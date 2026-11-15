"""The local side of a workspace: sidecar records plus seed-search indexes.

HydraDB OSS has no text or vector index, so entry-point search lives here.
Everything relational (structure, weights, experience) stays in HydraDB.
"""

from __future__ import annotations

import json
import os
import pickle
from pathlib import Path

import numpy as np

from .ingest import sidecar_path
from .text import BM25, MentionLinker, tokenize

PATH_BOOST = 3  # path terms are repeated so a path match outweighs a body match
DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DENSE_DOC_CHARS = 1800


class Workspace:
    def __init__(self, name: str, base: str | Path = "."):
        self.name = name
        self.dir = sidecar_path(name, base)
        if not (self.dir / "files.json").exists():
            raise FileNotFoundError(f"no sidecar at {self.dir}; run `trailmark ingest` first")
        self.meta = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        self.files: list[dict] = json.loads((self.dir / "files.json").read_text(encoding="utf-8"))
        self.symbols: list[dict] = json.loads((self.dir / "symbols.json").read_text(encoding="utf-8"))
        self.root = Path(self.meta["root"])
        names_path = self.dir / "names.json"
        # identifier text -> [node id, number of files that use it]
        self.names: dict[str, list[int]] = json.loads(names_path.read_text(encoding="utf-8")) if names_path.exists() else {}
        self.file_by_id = {f["id"]: f for f in self.files}
        self.path_id = {f["path"]: f["id"] for f in self.files}
        self.symbols_by_file: dict[int, list[dict]] = {}
        for s in self.symbols:
            self.symbols_by_file.setdefault(s["file_id"], []).append(s)
        self.linker = MentionLinker(self.files, self.symbols)
        self._bm25: BM25 | None = None
        self._sym_bm25: BM25 | None = None
        self._stems: list[tuple[frozenset[str], float]] | None = None
        self.row_of_file = {f["id"]: i for i, f in enumerate(self.files)}
        # row in the symbol index -> row of its file, for folding symbol scores into files
        self.sym_file_row = np.asarray([self.row_of_file[s["file_id"]] for s in self.symbols], dtype=np.int64)
        self._dense: np.ndarray | None = None
        self._embedder = None
        # row index in the search indexes -> file id (same order as self.files)
        self.row_file = np.asarray([f["id"] for f in self.files], dtype=np.int64)
        self.is_test = np.asarray([bool(f["is_test"]) for f in self.files])

    # -- lexical -----------------------------------------------------------
    def _file_tokens(self, f: dict) -> list[str]:
        try:
            body = (self.root / f["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
        return tokenize(f["path"]) * PATH_BOOST + tokenize(body)

    @property
    def bm25(self) -> BM25:
        if self._bm25 is None:
            cache = self.dir / "bm25.pkl"
            if cache.exists():
                self._bm25 = pickle.loads(cache.read_bytes())
            else:
                index = BM25()
                for f in self.files:
                    index.add(self._file_tokens(f))
                index.postings = dict(index.postings)  # drop the defaultdict lambda so it pickles
                cache.write_bytes(pickle.dumps(index))
                self._bm25 = index
        return self._bm25

    # -- symbol-level lexical ------------------------------------------------
    @property
    def sym_bm25(self) -> BM25:
        """BM25 over function and method bodies (classes: header and docstring).

        File-level scores dilute in large files; a single function that matches
        the task well is sharper evidence, and it gives line ranges to read.
        """
        if self._sym_bm25 is None:
            cache = self.dir / "sym_bm25.pkl"
            if cache.exists():
                self._sym_bm25 = pickle.loads(cache.read_bytes())
            else:
                index = BM25()
                lines_of: dict[int, list[str]] = {}
                for s in self.symbols:
                    lines = lines_of.get(s["file_id"])
                    if lines is None:
                        try:
                            text = (self.root / s["path"]).read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            text = ""
                        lines = lines_of[s["file_id"]] = text.splitlines()
                    end = s["end_line"] if s["kind"] != "class" else min(s["end_line"], s["line"] + 12)
                    body = "\n".join(lines[max(0, s["line"] - 1) : end])
                    index.add(tokenize(s["qualname"]) * 2 + tokenize(body))
                index.postings = dict(index.postings)
                cache.write_bytes(pickle.dumps(index))
                self._sym_bm25 = index
        return self._sym_bm25

    def file_stems(self) -> list[tuple[frozenset[str], float]]:
        """Per file (row order): the terms of its name, and how distinctive that name is.

        ``makemigrations.py`` named in a task title is close to proof; ``base.py``
        or ``utils.py`` is not, so a stem shared by many files counts for less.
        ``__init__.py`` takes its package's name.
        """
        if self._stems is None:
            stems = []
            for f in self.files:
                parts = f["path"][:-3].split("/")
                stem = parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]
                stems.append(frozenset(t for t in tokenize(stem) if len(t) > 2))
            count: dict[frozenset[str], int] = {}
            for st in stems:
                count[st] = count.get(st, 0) + 1
            self._stems = [(st, 1.0 / (count[st] ** 0.5) if st else 0.0) for st in stems]
        return self._stems

    # -- dense -------------------------------------------------------------
    def dense_doc(self, f: dict) -> str:
        """A compact natural-language-ish summary of a file for the embedding model."""
        parts = [f["path"].replace("/", " ").replace("_", " ").removesuffix(".py"), f.get("doc", "")]
        for s in self.symbols_by_file.get(f["id"], []):
            if s["kind"] != "method":
                parts.append(f"{s['name']}: {s['doc']}" if s["doc"] else s["name"])
        text = ". ".join(p for p in parts if p)
        if len(text) < DENSE_DOC_CHARS:
            methods = " ".join(s["name"] for s in self.symbols_by_file.get(f["id"], []) if s["kind"] == "method")
            text = f"{text}. {methods}"
        return text[:DENSE_DOC_CHARS]

    def embedder(self):
        if self._embedder is None:
            from fastembed import TextEmbedding  # optional dependency

            # TRAILMARK_MODEL_DIR keeps the model download off the system drive.
            self._embedder = TextEmbedding(DENSE_MODEL, cache_dir=os.environ.get("TRAILMARK_MODEL_DIR"))
        return self._embedder

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = np.asarray(list(self.embedder().embed(texts, batch_size=16)), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        return vecs / np.maximum(norms, 1e-9)

    @property
    def dense(self) -> np.ndarray:
        if self._dense is None:
            cache = self.dir / "dense.npy"
            if cache.exists():
                self._dense = np.load(cache)
            else:
                self._dense = self.embed([self.dense_doc(f) for f in self.files])
                np.save(cache, self._dense)
        return self._dense
