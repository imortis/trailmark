"""Hybrid retrieval: text finds entry points, HydraDB traversal does the rest.

Pipeline for one query:
  1. seeds      text signals pick entry files and entry symbols
  2. expansion  HydraDB one-hop traversals from each seed, per edge type and direction:
                file level (imports, references, co-change, learned trails) and
                symbol level (calls, inheritance)
  3. features   every candidate file gets a feature vector (text + graph evidence)
  4. scoring    linear model over the features; the weights are what learning tunes
  5. reasons    each result carries the evidence that put it there

All graph evidence is read from HydraDB at query time, so anything that
changes in the graph (new trails, reinforced weights, recorded tasks) changes
the next answer without rebuilding an index.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .hydra import Hydra
from .index import Workspace
from .text import code_identifiers, tokenize

# File-level expansions: (feature, edge type, direction). "out" follows seed -> candidate.
EXPANSIONS: tuple[tuple[str, str, str], ...] = (
    ("imports_out", "IMPORTS", "out"),  # seed imports candidate
    ("imports_in", "IMPORTS", "in"),    # candidate imports seed
    ("refs_out", "REFS", "out"),        # seed calls/inherits from candidate
    ("refs_in", "REFS", "in"),          # candidate calls/inherits from seed
    ("cochange", "COCHANGED", "out"),   # changed together in git history
    ("trail", "TRAIL", "out"),          # learned shortcut from past outcomes
)
# Symbol-level expansions from the symbols a task names or matches best.
SYMBOL_EXPANSIONS: tuple[tuple[str, str, str], ...] = (
    ("sym_callee", "CALLS", "out"),     # candidate defines something a seed symbol calls
    ("sym_caller", "CALLS", "in"),      # candidate calls a seed symbol
    ("sym_base", "INHERITS", "out"),    # candidate defines a base class of a seed class
    ("sym_sub", "INHERITS", "in"),      # candidate subclasses a seed class
)
STATIC_KINDS = {"IMPORTS", "REFS", "COCHANGED", "CALLS", "INHERITS", "USES"}  # fixed after ingest: safe to cache
TEXT_FEATURES = ("bm25", "title", "sym", "dense", "stem_title", "stem_text", "mention_path", "mention_name")
GRAPH_FEATURES = tuple(n for n, _, _ in EXPANSIONS) + tuple(n for n, _, _ in SYMBOL_EXPANSIONS)
LINK_FEATURES = ("uses",)  # read from HydraDB before seeding: files that touch a named identifier
LEARNED_FEATURES = ("experience", "hot")
FEATURES = TEXT_FEATURES + LINK_FEATURES + GRAPH_FEATURES + LEARNED_FEATURES

DEFAULT_WEIGHTS: dict[str, float] = {
    "bm25": 1.0, "title": 0.6, "sym": 0.8, "dense": 0.8,
    "stem_title": 1.2, "stem_text": 0.3, "mention_path": 0.4, "mention_name": 1.0,
    "imports_out": 0.20, "imports_in": 0.10, "refs_out": 0.30, "refs_in": 0.15,
    "cochange": 0.40, "trail": 0.8,
    "sym_callee": 0.30, "sym_caller": 0.15, "sym_base": 0.20, "sym_sub": 0.10,
    "experience": 1.0, "hot": 0.3, "uses": 0.8,
}


@dataclass
class Config:
    use_bm25: bool = True
    use_title: bool = True      # the first line of a task, scored on its own
    use_sym: bool = True        # function-level keyword match
    use_dense: bool = False
    use_stems: bool = True      # file names that appear in the task
    use_mentions: bool = True
    use_graph: bool = True      # file-level traversal
    use_symgraph: bool = True   # symbol-level traversal
    use_uses: bool = True       # files that use an identifier the task names
    max_identifiers: int = 16
    kinds: tuple[str, ...] = ("IMPORTS", "REFS", "COCHANGED", "TRAIL")
    include_tests: bool = False
    n_seeds: int = 12
    n_symbol_seeds: int = 10
    pool: int = 60  # text candidates kept per signal
    top_k: int = 20
    trail_kind: str = "TRAIL"  # edge type holding learned trails (benchmark runs use their own)
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))


@dataclass
class Hit:
    file_id: int
    path: str
    score: float
    features: dict[str, float]
    reasons: list[str]


@dataclass
class Result:
    hits: list[Hit]
    seeds: list[tuple[int, float]]  # (file id, seed strength)
    candidates: dict[int, dict[str, float]]  # file id -> features, for learning
    db_queries: int
    mention_symbols: dict[int, float]
    symbol_seeds: list[tuple[int, float]] = field(default_factory=list)
    # identifier -> files that use it, rarest identifiers first (the evidence a grep would give)
    usage: list[tuple[str, list[int]]] = field(default_factory=list)


def _neighbors_query(kind: str, direction: str) -> str:
    arrow = f"-[r:{kind}]->" if direction == "out" else f"<-[r:{kind}]-"
    return f"MATCH (a {{id: $id}}){arrow}(b) RETURN b.id AS id, r.w AS w, b.imp_in AS imp_in, b.ref_in AS ref_in"


def _symbol_neighbors_query(kind: str, direction: str) -> str:
    arrow = f"-[:{kind}]->" if direction == "out" else f"<-[:{kind}]-"
    return f"MATCH (a {{id: $id}}){arrow}(b) RETURN b.id AS id, b.path AS path"


_USES_QUERY = "MATCH (n {id: $id})<-[:USES]-(f) RETURN f.id AS id"


def split_title(query: str) -> str:
    for line in query.splitlines():
        if line.strip():
            return line.strip()
    return ""


class Retriever:
    def __init__(self, ws: Workspace, db: Hydra | None, config: Config | None = None):
        self.ws = ws
        self.db = db
        self.config = config or Config()
        self._cache: dict[tuple[str, str, int], list[dict]] = {}
        self.query_vectors: dict[str, np.ndarray] = {}  # optional precomputed query embeddings
        self.last_trails: dict[int, list[dict]] = {}
        self._sym_ids = np.asarray([s["id"] for s in ws.symbols], dtype=np.int64)
        self._sym_by_id = {s["id"]: s for s in ws.symbols}

    # -- stage 1: text signals -------------------------------------------------
    def _text_scores(self, query: str):
        cfg, ws = self.config, self.ws
        signals: dict[str, dict[int, float]] = {}
        allowed = np.ones(len(ws.files), dtype=bool) if cfg.include_tests else ~ws.is_test
        tokens = tokenize(query)
        title_tokens = tokenize(split_title(query))

        def top(scores: np.ndarray, keep: int | None = None) -> dict[int, float]:
            scores = np.where(allowed, scores, 0.0)
            hi = float(scores.max()) if scores.size else 0.0
            if hi <= 0:
                return {}
            order = np.argsort(-scores)[: keep or cfg.pool]
            return {int(ws.row_file[i]): float(scores[i] / hi) for i in order if scores[i] > 0}

        if cfg.use_bm25:
            signals["bm25"] = top(ws.bm25.scores(tokens))
        if cfg.use_title and title_tokens:
            signals["title"] = top(ws.bm25.scores(title_tokens))

        symbol_seeds: dict[int, float] = {}
        if cfg.use_sym:
            sym_scores = ws.sym_bm25.scores(tokens)
            per_file = np.zeros(len(ws.files), dtype=np.float32)
            np.maximum.at(per_file, ws.sym_file_row, sym_scores)
            signals["sym"] = top(per_file)
            hi = float(sym_scores.max()) if sym_scores.size else 0.0
            if hi > 0:
                for i in np.argsort(-sym_scores)[: cfg.n_symbol_seeds]:
                    if sym_scores[i] > 0 and allowed[ws.sym_file_row[i]]:
                        symbol_seeds[int(self._sym_ids[i])] = float(sym_scores[i] / hi) * 0.7

        if cfg.use_dense:
            q = self.query_vectors.get(query)
            if q is None:
                q = ws.embed([query[:2000]])[0]
            cos = ws.dense @ q
            signals["dense"] = top(np.maximum(cos - float(np.median(cos)), 0.0))

        if cfg.use_stems:
            title_set, text_set = set(title_tokens), set(tokens)
            st_title: dict[int, float] = {}
            st_text: dict[int, float] = {}
            for row, (stem, distinct) in enumerate(ws.file_stems()):
                if not stem or not allowed[row] or not stem <= text_set:
                    continue
                fid = int(ws.row_file[row])
                st_text[fid] = distinct
                if stem <= title_set:
                    st_title[fid] = distinct
            signals["stem_text"], signals["stem_title"] = st_text, st_title

        evidence: dict[int, list[str]] = {}
        mention_symbols: dict[int, float] = {}
        if cfg.use_mentions:
            m = ws.linker.link(query)
            ok = lambda fid: cfg.include_tests or not ws.file_by_id[fid]["is_test"]  # noqa: E731
            signals["mention_path"] = {f: s for f, s in m.paths.items() if ok(f)}
            signals["mention_name"] = {f: s for f, s in m.names.items() if ok(f)}
            evidence, mention_symbols = m.evidence, m.symbols
            for sid, s in sorted(m.symbols.items(), key=lambda kv: -kv[1])[: cfg.n_symbol_seeds]:
                symbol_seeds[sid] = max(symbol_seeds.get(sid, 0.0), s)
        seeds = sorted(symbol_seeds.items(), key=lambda kv: -kv[1])[: cfg.n_symbol_seeds]
        return signals, evidence, mention_symbols, seeds

    # -- identifier usage (HydraDB) ------------------------------------------------
    def _usage(self, query: str) -> tuple[dict[int, float], list[tuple[str, list[int]]], int]:
        """Files that use the identifiers a task names, weighted by how rare each identifier is.

        One read per identifier: ``(:Name)<-[:USES]-(:File)``. This is what an
        agent's grep loop finds, in a single call, and it is what makes a
        multi-file change complete: every site that touches a renamed or
        removed name shows up.
        """
        ws, cfg = self.ws, self.config
        if self.db is None or not ws.names:
            return {}, [], 0
        found = [(ident, *ws.names[ident]) for ident in code_identifiers(query) if ident in ws.names]
        found.sort(key=lambda x: (x[2], x[0]))  # rarest first
        found = found[: cfg.max_identifiers]
        reads = self._fetch([("USES", "in", nid) for _, nid, _ in found], symbol=False, query=_USES_QUERY)
        scores: dict[int, float] = {}
        usage: list[tuple[str, list[int]]] = []
        for ident, nid, _ in found:
            files = [r["id"] for r in self._cache.get(("USES", "in", nid), [])]
            files = [f for f in files if f in ws.file_by_id and (cfg.include_tests or not ws.file_by_id[f]["is_test"])]
            if not files:
                continue
            usage.append((ident, files))
            weight = 1.0 / math.sqrt(len(files))
            for f in files:
                scores[f] = scores.get(f, 0.0) + weight
        hi = max(scores.values(), default=0.0)
        return ({f: s / hi for f, s in scores.items()} if hi > 0 else {}), usage, reads

    # -- stage 2: expansion in HydraDB -------------------------------------------
    def _fetch(self, jobs: list[tuple[str, str, int]], symbol: bool, query: str | None = None) -> int:
        """Make sure every (edge type, direction, node) neighbourhood is in the cache. Returns DB reads."""
        if self.db is None:
            return 0
        missing: dict[tuple[str, str], list[int]] = {}
        for kind, direction, nid in jobs:
            if (kind, direction, nid) not in self._cache or kind not in STATIC_KINDS:
                missing.setdefault((kind, direction), []).append(nid)
        reads = 0
        for (kind, direction), ids in missing.items():
            real = self.config.trail_kind if kind == "TRAIL" else kind
            q = query or (_symbol_neighbors_query(real, direction) if symbol else _neighbors_query(real, direction))
            for nid, rows in zip(ids, self.db.query_many(q, [{"id": i} for i in ids])):
                self._cache[(kind, direction, nid)] = rows
            reads += len(ids)
        return reads

    def _expand(self, seeds: list[tuple[int, float]]):
        """File-level traversal. Returns per-candidate features, reasons and the number of DB reads."""
        cfg = self.config
        jobs = [(name, kind, d, fid, s) for fid, s in seeds for name, kind, d in EXPANSIONS if kind in cfg.kinds]
        reads = self._fetch([(kind, d, fid) for _, kind, d, fid, _ in jobs], symbol=False)
        feats: dict[int, dict[str, float]] = {}
        reasons: dict[int, list[tuple[float, str]]] = {}
        for name, kind, direction, fid, strength in jobs:
            rows = self._cache.get((kind, direction, fid), [])
            if not rows:
                continue
            spread = 1.0 / math.sqrt(len(rows))  # a seed with many neighbours spreads its evidence thin
            seed_path = self.ws.file_by_id[fid]["path"]
            for r in rows:
                cand = r["id"]
                if cand not in self.ws.file_by_id:
                    continue
                w = r["w"] if r["w"] is not None else 1.0
                if kind == "REFS":
                    w = math.log1p(w)
                # Hub damping: being imported by everything is weak evidence.
                if name == "imports_out":
                    w /= math.sqrt(1.0 + (r["imp_in"] or 0))
                elif name == "refs_out":
                    w /= math.sqrt(1.0 + (r["ref_in"] or 0))
                value = strength * w * spread
                f = feats.setdefault(cand, {})
                f[name] = f.get(name, 0.0) + value
                reasons.setdefault(cand, []).append((value * cfg.weights.get(name, 0.0), _reason(name, seed_path)))
        return feats, reasons, reads

    def _expand_symbols(self, seeds: list[tuple[int, float]]):
        """Symbol-level traversal: what the named symbols call, are called by, extend or are extended by."""
        ws, cfg = self.ws, self.config
        sym = self._sym_by_id
        jobs = [(name, kind, d, sid, s) for sid, s in seeds for name, kind, d in SYMBOL_EXPANSIONS]
        reads = self._fetch([(kind, d, sid) for _, kind, d, sid, _ in jobs], symbol=True)
        feats: dict[int, dict[str, float]] = {}
        reasons: dict[int, list[tuple[float, str]]] = {}
        for name, kind, direction, sid, strength in jobs:
            rows = self._cache.get((kind, direction, sid), [])
            if not rows:
                continue
            home = sym[sid]["file_id"] if sid in sym else None
            spread = 1.0 / math.sqrt(len(rows))
            label = sym[sid]["qualname"] if sid in sym else str(sid)
            per_file: dict[int, float] = {}
            for r in rows:
                fid = ws.path_id.get(r["path"])
                if fid is None or fid == home:
                    continue
                per_file[fid] = max(per_file.get(fid, 0.0), strength * spread)
            for fid, value in per_file.items():
                f = feats.setdefault(fid, {})
                f[name] = f.get(name, 0.0) + value
                reasons.setdefault(fid, []).append((value * cfg.weights.get(name, 0.0), _symbol_reason(name, label)))
        return feats, reasons, reads

    # -- full pipeline ---------------------------------------------------------
    def locate(self, query: str, extra: dict[int, dict[str, float]] | None = None, annotate=None) -> Result:
        """Rank files for ``query``.

        The learning layer plugs in two ways: ``extra`` adds candidates with
        features before seeding (experience from similar past tasks), and
        ``annotate(file_id) -> features`` decorates every final candidate
        without adding new ones (a prior such as how often a file gets fixed).
        """
        cfg, ws = self.config, self.ws
        signals, evidence, mention_symbols, symbol_seeds = self._text_scores(query)
        keep = lambda fid: fid in ws.file_by_id and (cfg.include_tests or not ws.file_by_id[fid]["is_test"])  # noqa: E731

        cands: dict[int, dict[str, float]] = {}
        for name, scores in signals.items():
            for fid, s in scores.items():
                cands.setdefault(fid, {})[name] = s
        for fid, f in (extra or {}).items():
            if keep(fid):
                cands.setdefault(fid, {}).update(f)
        reads = 0
        usage: list[tuple[str, list[int]]] = []
        if cfg.use_uses:
            use_scores, usage, reads = self._usage(query)
            for fid, s in use_scores.items():
                cands.setdefault(fid, {})["uses"] = s

        strength = {
            fid: sum(cfg.weights.get(k, 0.0) * v for k, v in f.items() if k not in GRAPH_FEATURES)
            for fid, f in cands.items()
        }
        top = sorted(strength.items(), key=lambda kv: -kv[1])[: cfg.n_seeds]
        hi = top[0][1] if top and top[0][1] > 0 else 1.0
        seeds = [(fid, s / hi) for fid, s in top if s > 0]

        reasons: dict[int, list[tuple[float, str]]] = {}
        if cfg.use_graph and seeds and cfg.kinds:
            gfeats, greasons, n = self._expand(seeds)
            reads += n
            for fid, f in gfeats.items():
                if keep(fid):
                    cands.setdefault(fid, {}).update(f)
                    reasons.setdefault(fid, []).extend(greasons.get(fid, []))
        if cfg.use_symgraph and symbol_seeds:
            sfeats, sreasons, n = self._expand_symbols(symbol_seeds)
            reads += n
            for fid, f in sfeats.items():
                if keep(fid):
                    cands.setdefault(fid, {}).update(f)
                    reasons.setdefault(fid, []).extend(sreasons.get(fid, []))

        if annotate is not None:
            for fid, f in cands.items():
                f.update(annotate(fid))

        hits = [
            Hit(fid, ws.file_by_id[fid]["path"], sum(cfg.weights.get(k, 0.0) * v for k, v in f.items()), f, [])
            for fid, f in cands.items()
        ]
        hits.sort(key=lambda h: (-h.score, h.path))
        hits = hits[: cfg.top_k]
        for h in hits:
            h.reasons = self._explain(h, evidence.get(h.file_id, []), reasons.get(h.file_id, []))
        self.last_trails = (
            {fid: self._cache.get(("TRAIL", "out", fid), []) for fid, _ in seeds} if "TRAIL" in cfg.kinds else {}
        )
        return Result(hits, seeds, cands, reads, mention_symbols, symbol_seeds, usage)

    def _explain(self, hit: Hit, mentioned: list[str], graph_reasons: list[tuple[float, str]]) -> list[str]:
        out: list[str] = []
        f = hit.features
        if f.get("stem_title", 0) > 0:
            out.append("file name appears in the task title")
        if mentioned:
            out.append("named in the task: " + ", ".join(mentioned))
        if f.get("sym", 0) >= 0.6:
            out.append("contains a function that matches the task closely")
        elif f.get("bm25", 0) >= 0.5:
            out.append("strong keyword match")
        if f.get("dense", 0) >= 0.5:
            out.append("semantically similar")
        if f.get("uses", 0) >= 0.4:
            out.append("uses identifiers the task names")
        if f.get("experience", 0) > 0:
            out.append("changed by similar past tasks")
        seen: set[str] = set()
        for _, text in sorted(graph_reasons, reverse=True):
            if text not in seen:
                seen.add(text)
                out.append(text)
            if len(seen) == 3:
                break
        return out

    # -- graph-native explanation ------------------------------------------------
    def _sp(self, a: int, b: int, max_len: int, kinds: list[str]) -> dict | None:
        if self.db is None or not kinds:
            return None
        rels = ", ".join(f"'{k}'" for k in kinds)
        rows = self.db.query(
            f"CALL algo.SPpaths({{sourceNode: {int(a)}, targetNode: {int(b)}, relTypes: [{rels}], "
            f"maxLen: {int(max_len)}, relDirection: 'both', pathCount: 1}}) "
            "YIELD path, pathWeight, pathCost RETURN path, pathWeight, pathCost"
        )
        return rows[0]["path"] if rows else None

    def why(self, src_path: str, dst_path: str, max_len: int = 3) -> list[dict] | None:
        """Shortest structural path between two files, computed by HydraDB (algo.SPpaths)."""
        a, b = self.ws.path_id.get(src_path), self.ws.path_id.get(dst_path)
        if a is None or b is None:
            return None
        kinds = [self.config.trail_kind if k == "TRAIL" else k for k in self.config.kinds]
        path = self._sp(a, b, max_len, kinds)
        if path is None:
            return None
        nodes, rels = path["nodes"], path["rels"]
        name = lambda n: n.get("path", str(n["id"]))  # noqa: E731
        # Nodes come in walk order; an edge may be traversed against its direction.
        return [
            {"from": name(nodes[i]), "edge": r["type"], "to": name(nodes[i + 1]),
             "forward": r["src"] == nodes[i]["id"], "w": r.get("w")}
            for i, r in enumerate(rels)
        ]

    def path_len(self, a: int, b: int, max_len: int = 3) -> int | None:
        """Hops on the shortest structural path between two file ids, or None if there is none."""
        path = self._sp(a, b, max_len, [k for k in self.config.kinds if k in STATIC_KINDS])
        return len(path["rels"]) if path else None


def _reason(feature: str, seed_path: str) -> str:
    return {
        "imports_out": f"imported by {seed_path}",
        "imports_in": f"imports {seed_path}",
        "refs_out": f"called or subclassed by {seed_path}",
        "refs_in": f"calls or subclasses {seed_path}",
        "cochange": f"historically changes with {seed_path}",
        "trail": f"learned trail from {seed_path}",
    }[feature]


def _symbol_reason(feature: str, symbol: str) -> str:
    return {
        "sym_callee": f"defines code that {symbol} calls",
        "sym_caller": f"calls {symbol}",
        "sym_base": f"defines a base class of {symbol}",
        "sym_sub": f"subclasses {symbol}",
    }[feature]
