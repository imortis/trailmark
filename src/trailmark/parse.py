"""Static analysis of a Python repository into a code graph.

Two passes. The first parses every file and records symbols, raw imports,
raw call targets and raw base classes as dotted names. The second resolves
those names to files and symbols, following ``__init__`` re-exports.

The output is plain data (``CodeGraph``); loading it into HydraDB is
``ingest.py``'s job.
"""

from __future__ import annotations

import ast
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".tox", ".nox", "build", "dist", ".eggs", ".mypy_cache", ".pytest_cache", "site-packages",
}
SOURCE_ROOTS = ("src", "lib")
FILE_ID_BASE = 1
SYMBOL_ID_BASE = 1_000_000
NAME_ID_BASE = 5_000_000
NAME_MIN_LEN = 4
NAME_MAX_FILES = 40  # an identifier used in more files than this says little about where to look
MAX_REEXPORT_HOPS = 6


@dataclass
class FileRec:
    id: int
    path: str  # posix, relative to repo root
    module: str
    is_pkg: bool
    is_test: bool
    loc: int
    doc: str = ""


@dataclass
class SymbolRec:
    id: int
    file_id: int
    path: str
    name: str
    qualname: str  # "Class.method" or "func"
    kind: str  # "class" | "function" | "method"
    line: int
    end_line: int
    doc: str = ""


@dataclass
class CodeGraph:
    root: str
    files: list[FileRec] = field(default_factory=list)
    symbols: list[SymbolRec] = field(default_factory=list)
    # edge type -> {(src_id, dst_id): weight}
    edges: dict[str, dict[tuple[int, int], float]] = field(default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)
    # identifier text -> (node id, number of files that use it)
    names: dict[str, tuple[int, int]] = field(default_factory=dict)

    def add_edge(self, kind: str, src: int, dst: int, weight: float = 1.0) -> None:
        if src == dst:
            return
        bucket = self.edges.setdefault(kind, {})
        bucket[(src, dst)] = bucket.get((src, dst), 0.0) + weight

    def stats(self) -> dict[str, int]:
        out = {"files": len(self.files), "symbols": len(self.symbols), "names": len(self.names)}
        for kind, bucket in sorted(self.edges.items()):
            out[kind] = len(bucket)
        return out


# --------------------------------------------------------------------------
# pass 1: per-file extraction
# --------------------------------------------------------------------------

@dataclass
class _RawFile:
    rec: FileRec
    # local name -> ("module", dotted) or ("symbol", module_dotted, name)
    aliases: dict[str, tuple] = field(default_factory=dict)
    import_modules: set[str] = field(default_factory=set)  # dotted candidates for IMPORTS
    star_imports: list[str] = field(default_factory=list)
    # symbol qualname -> list of dotted call targets
    calls: dict[str, list[str]] = field(default_factory=dict)
    # class qualname -> list of dotted base names
    bases: dict[str, list[str]] = field(default_factory=dict)
    top_names: set[str] = field(default_factory=set)


def _dotted(node: ast.AST) -> str | None:
    """Render ``a.b.c`` for Name/Attribute chains; ``?.attr`` when the base is opaque."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    if parts:
        return "?." + parts[0]
    return None


def _first_line(doc: str | None) -> str:
    if not doc:
        return ""
    return doc.strip().splitlines()[0][:200] if doc.strip() else ""


def _is_test_path(path: str) -> bool:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    return (
        "/tests/" in f"/{p}" or "/test/" in f"/{p}" or name.startswith("test_")
        or name.endswith("_test.py") or name in ("tests.py", "conftest.py")
    )


class _Extractor(ast.NodeVisitor):
    def __init__(self, raw: _RawFile, symbols: list[SymbolRec], next_id: list[int], module: str, is_pkg: bool):
        self.raw = raw
        self.symbols = symbols
        self.next_id = next_id
        self.module = module
        self.is_pkg = is_pkg
        self.scope: list[tuple[str, str]] = []  # (kind, name)

    # -- imports -----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.raw.import_modules.add(a.name)
            if a.asname:
                self.raw.aliases[a.asname] = ("module", a.name)
            else:
                head = a.name.split(".")[0]
                self.raw.aliases[head] = ("module", head)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self._resolve_relative(node.module or "", node.level)
        if base is None:
            return
        if base:
            self.raw.import_modules.add(base)
        for a in node.names:
            if a.name == "*":
                self.raw.star_imports.append(base)
                continue
            local = a.asname or a.name
            # Could be a submodule or a symbol; pass 2 decides.
            self.raw.aliases[local] = ("symbol", base, a.name)
            self.raw.import_modules.add(f"{base}.{a.name}" if base else a.name)

    def _resolve_relative(self, module: str, level: int) -> str | None:
        if level == 0:
            return module
        parts = self.module.split(".") if self.module else []
        if not self.is_pkg:
            parts = parts[:-1]
        up = level - 1
        if up > len(parts):
            return None
        if up:
            parts = parts[:-up]
        if module:
            parts = parts + module.split(".")
        return ".".join(parts)

    # -- definitions -------------------------------------------------------
    def _add_symbol(self, node: ast.AST, name: str, kind: str) -> str:
        qual = ".".join([n for _, n in self.scope] + [name])
        self.symbols.append(
            SymbolRec(
                id=self.next_id[0],
                file_id=self.raw.rec.id,
                path=self.raw.rec.path,
                name=name,
                qualname=qual,
                kind=kind,
                line=getattr(node, "lineno", 0),
                end_line=getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0,
                doc=_first_line(ast.get_docstring(node, clean=False)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else "",
            )
        )
        self.next_id[0] += 1
        return qual

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(k == "function" for k, _ in self.scope):
            return  # classes nested in functions are not addressable
        qual = self._add_symbol(node, node.name, "class")
        if not self.scope:
            self.raw.top_names.add(node.name)
        self.raw.bases[qual] = [d for d in (_dotted(b) for b in node.bases) if d and not d.startswith("?.")]
        for dec in node.decorator_list:
            self._collect_calls(qual, dec)
        self.scope.append(("class", node.name))
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if any(k == "function" for k, _ in self.scope):
            return  # nested function: its calls were collected by the outer walk
        in_class = bool(self.scope) and self.scope[-1][0] == "class"
        qual = self._add_symbol(node, node.name, "method" if in_class else "function")
        if not self.scope:
            self.raw.top_names.add(node.name)
        self._collect_calls(qual, node)

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def _collect_calls(self, qual: str, node: ast.AST) -> None:
        bucket = self.raw.calls.setdefault(qual, [])
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                d = _dotted(sub.func)
                if d:
                    bucket.append(d)

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.scope:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.raw.top_names.add(t.id)


def discover(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                out.append(Path(dirpath) / fn)
    return out


def _module_name(rel: str, roots: list[str]) -> tuple[str, bool]:
    for r in roots:  # deepest source root first
        if r and rel.startswith(r + "/"):
            rel = rel[len(r) + 1 :]
            break
    parts = rel[:-3].split("/")
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


# --------------------------------------------------------------------------
# pass 2: resolution
# --------------------------------------------------------------------------

class _Resolver:
    def __init__(self, raws: list[_RawFile], symbols: list[SymbolRec]):
        self.raws = raws
        self.by_module: dict[str, _RawFile] = {}
        for r in raws:
            self.by_module.setdefault(r.rec.module, r)
        self.sym: dict[tuple[str, str], SymbolRec] = {}
        self.by_name: dict[str, list[SymbolRec]] = defaultdict(list)
        module_of_file = {r.rec.id: r.rec.module for r in raws}
        for s in symbols:
            self.sym.setdefault((module_of_file[s.file_id], s.qualname), s)
            if s.kind != "class":
                self.by_name[s.name].append(s)
        self.class_bases: dict[int, list[int]] = defaultdict(list)

    def find_module(self, dotted: str) -> tuple[_RawFile | None, list[str]]:
        """Longest module prefix of ``dotted`` plus the remaining attribute names."""
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            raw = self.by_module.get(".".join(parts[:i]))
            if raw is not None:
                return raw, parts[i:]
        return None, parts

    def symbol_in(self, module: str, names: list[str], hops: int = 0) -> SymbolRec | None:
        """Resolve ``names`` (e.g. ["Q"] or ["Model", "save"]) inside ``module``."""
        if not names or hops > MAX_REEXPORT_HOPS:
            return None
        for take in (len(names), 1):
            s = self.sym.get((module, ".".join(names[:take])))
            if s is not None:
                if take < len(names) and s.kind == "class":
                    m = self.method_of(s, names[take])
                    return m or s
                return s
        raw = self.by_module.get(module)
        if raw is None:
            return None
        alias = raw.aliases.get(names[0])
        if alias is not None:
            return self.resolve_alias(alias, names[1:], hops + 1)
        for star in raw.star_imports:
            s = self.symbol_in(star, names, hops + 1)
            if s is not None:
                return s
        return None

    def resolve_alias(self, alias: tuple, tail: list[str], hops: int = 0) -> SymbolRec | None:
        if alias[0] == "module":
            raw, rest = self.find_module(".".join([alias[1], *tail]))
            if raw is None or not rest:
                return None
            return self.symbol_in(raw.rec.module, rest, hops)
        _, base, name = alias
        sub = f"{base}.{name}" if base else name
        if sub in self.by_module:  # ``from pkg import submodule``
            return self.symbol_in(sub, tail, hops) if tail else None
        return self.symbol_in(base, [name, *tail], hops)

    def method_of(self, cls: SymbolRec, name: str, depth: int = 0) -> SymbolRec | None:
        """Find ``name`` on ``cls`` or, failing that, on its resolved base classes."""
        module = self._module_of_file[cls.file_id]
        s = self.sym.get((module, f"{cls.qualname}.{name}"))
        if s is not None or depth > 5:
            return s
        for base_id in self.class_bases.get(cls.id, []):
            base = self._by_id.get(base_id)
            if base is not None:
                s = self.method_of(base, name, depth + 1)
                if s is not None:
                    return s
        return None

    def prepare(self, symbols: list[SymbolRec]) -> None:
        self._by_id = {s.id: s for s in symbols}
        self._module_of_file = {r.rec.id: r.rec.module for r in self.raws}

    def resolve_name(self, raw: _RawFile, dotted: str, cls_qual: str | None) -> SymbolRec | None:
        parts = dotted.split(".")
        head, tail = parts[0], parts[1:]
        module = raw.rec.module
        if head in ("self", "cls") and cls_qual and len(tail) == 1:
            cls = self.sym.get((module, cls_qual))
            return self.method_of(cls, tail[0]) if cls else None
        if head == "?":
            name = tail[0]
            if len(name) > 3 and not name.startswith("__"):
                cands = self.by_name.get(name, [])
                if len(cands) == 1:
                    return cands[0]
            return None
        if head in raw.top_names:
            local = self.sym.get((module, head))
            if local is None:
                return None
            if tail and local.kind == "class":
                return self.method_of(local, tail[0]) or local
            return local
        alias = raw.aliases.get(head)
        if alias is not None:
            return self.resolve_alias(alias, tail)
        return None


def parse_repo(root: str | Path, id_offset: int = 0) -> CodeGraph:
    """Parse a repository. ``id_offset`` gives each repository its own id range in one database."""
    root = Path(root).resolve()
    graph = CodeGraph(root=str(root))
    roots = [r for r in SOURCE_ROOTS if (root / r).is_dir()]
    raws: list[_RawFile] = []
    next_sym = [id_offset + SYMBOL_ID_BASE]
    used_in: dict[str, set[int]] = {}  # identifier -> files that use or define it

    for i, path in enumerate(discover(root)):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError) as e:
            graph.parse_errors.append(f"{rel}: {type(e).__name__}")
            continue
        module, is_pkg = _module_name(rel, roots)
        rec = FileRec(
            id=id_offset + FILE_ID_BASE + i, path=rel, module=module, is_pkg=is_pkg,
            is_test=_is_test_path(rel), loc=text.count("\n") + 1,
            doc=_first_line(ast.get_docstring(tree, clean=False)),
        )
        raw = _RawFile(rec=rec)
        ex = _Extractor(raw, graph.symbols, next_sym, module, is_pkg)
        for node in tree.body:
            ex.visit(node)
        raws.append(raw)
        graph.files.append(rec)
        for node in ast.walk(tree):
            ident = None
            if isinstance(node, ast.Name):
                ident = node.id
            elif isinstance(node, ast.Attribute):
                ident = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                ident = node.name
            elif isinstance(node, ast.keyword):
                ident = node.arg
            elif isinstance(node, ast.arg):
                ident = node.arg
            if ident and len(ident) >= NAME_MIN_LEN and not ident.startswith("__"):
                used_in.setdefault(ident, set()).add(rec.id)

    # USES: file -> identifier. This is the graph form of "find references": it
    # answers "which files touch this name" for names the resolver cannot bind
    # (attribute access on untyped values), which is most of a dynamic codebase.
    next_name = id_offset + NAME_ID_BASE
    for ident in sorted(used_in):
        files = used_in[ident]
        if len(files) > NAME_MAX_FILES:
            continue
        graph.names[ident] = (next_name, len(files))
        for fid in files:
            graph.add_edge("USES", fid, next_name)
        next_name += 1

    res = _Resolver(raws, graph.symbols)
    res.prepare(graph.symbols)

    # DEFINES
    for s in graph.symbols:
        graph.add_edge("DEFINES", s.file_id, s.id)

    # IMPORTS (file -> file)
    for raw in raws:
        for dotted in raw.import_modules:
            target, _ = res.find_module(dotted)
            if target is not None:
                graph.add_edge("IMPORTS", raw.rec.id, target.rec.id)
        # ``from pkg import name`` re-exported from elsewhere: link to the real home too
        for alias in raw.aliases.values():
            if alias[0] == "symbol":
                s = res.resolve_alias(alias, [])
                if s is not None:
                    graph.add_edge("IMPORTS", raw.rec.id, s.file_id)

    # INHERITS first, so method lookup can climb base classes
    for raw in raws:
        for cls_qual, bases in raw.bases.items():
            cls = res.sym.get((raw.rec.module, cls_qual))
            if cls is None:
                continue
            for b in bases:
                target = res.resolve_name(raw, b, None)
                if target is not None and target.kind == "class":
                    graph.add_edge("INHERITS", cls.id, target.id)
                    res.class_bases[cls.id].append(target.id)

    # CALLS
    for raw in raws:
        for qual, targets in raw.calls.items():
            src = res.sym.get((raw.rec.module, qual))
            if src is None:
                continue
            cls_qual = qual.rsplit(".", 1)[0] if src.kind == "method" else (qual if src.kind == "class" else None)
            for dotted, n in Counter(targets).items():
                dst = res.resolve_name(raw, dotted, cls_qual)
                if dst is not None:
                    graph.add_edge("CALLS", src.id, dst.id, float(n))

    # IMPORTS should be unweighted: collapse repeat counts
    if "IMPORTS" in graph.edges:
        graph.edges["IMPORTS"] = {k: 1.0 for k in graph.edges["IMPORTS"]}

    # REFS: file-level projection of CALLS + INHERITS, weighted by reference count
    sym_file = {s.id: s.file_id for s in graph.symbols}
    for kind in ("CALLS", "INHERITS"):
        for (a, b), w in graph.edges.get(kind, {}).items():
            graph.add_edge("REFS", sym_file[a], sym_file[b], w)

    return graph
