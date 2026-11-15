"""Lexical machinery: code-aware tokenising, BM25, and mention linking.

Mention linking is the cheapest strong signal in issue localisation: issue
text names files, modules, classes and functions outright (often inside a
traceback). We link those mentions straight to graph nodes.
"""

from __future__ import annotations

import keyword
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_PATH = re.compile(r"[\w./\\-]*\w\.py\b")
_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
_BACKTICK = re.compile(r"`([^`\n]{2,80})`")

STOP = set(keyword.kwlist) | {
    "the", "a", "an", "of", "to", "in", "is", "it", "and", "or", "for", "on", "with", "as", "be", "this", "that",
    "are", "was", "at", "by", "from", "not", "but", "if", "when", "which", "should", "would", "can", "will", "has",
    "have", "does", "do", "we", "you", "i", "so", "there", "then", "than", "also", "into", "any", "all", "use",
    "using", "used", "self", "cls", "none", "true", "false", "py", "get", "set",
}


def tokenize(text: str) -> list[str]:
    """Split text into lowercase terms; identifiers yield themselves and their parts."""
    out: list[str] = []
    for ident in _IDENT.findall(text):
        low = ident.lower()
        parts = [p.lower() for chunk in ident.split("_") if chunk for p in _CAMEL.findall(chunk)]
        if len(parts) > 1:
            if len(low) > 2 and low not in STOP:
                out.append(low)
            out.extend(p for p in parts if len(p) > 1 and p not in STOP and not p.isdigit())
        elif len(low) > 1 and low not in STOP:
            out.append(low)
    return out


class BM25:
    """Okapi BM25 with numpy postings. Supports appending documents later."""

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_len: list[int] = []
        self.postings: dict[str, tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
        self._frozen: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def add(self, tokens: list[str]) -> int:
        doc = len(self.doc_len)
        self.doc_len.append(len(tokens))
        for term, tf in Counter(tokens).items():
            ids, tfs = self.postings[term]
            ids.append(doc)
            tfs.append(tf)
            self._frozen.pop(term, None)
        return doc

    def __len__(self) -> int:
        return len(self.doc_len)

    def max_score(self, query_tokens: list[str]) -> float:
        """Upper bound on any document's score for this query (every term saturated).

        Dividing a score by this gives an absolute similarity in [0, 1), so "the
        best of a bad lot" does not look like a strong match.
        """
        n = len(self.doc_len)
        total = 0.0
        for term, qtf in Counter(query_tokens).items():
            df = len(self.postings[term][0]) if term in self.postings else 0
            total += math.log(1.0 + (n - df + 0.5) / (df + 0.5)) * (self.k1 + 1.0) * min(qtf, 3)
        return total

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        n = len(self.doc_len)
        out = np.zeros(n, dtype=np.float32)
        if n == 0:
            return out
        lens = np.asarray(self.doc_len, dtype=np.float32)
        avg = float(lens.mean()) or 1.0
        for term, qtf in Counter(query_tokens).items():
            if term not in self.postings:
                continue
            if term not in self._frozen:
                ids, tfs = self.postings[term]
                self._frozen[term] = (np.asarray(ids, dtype=np.int64), np.asarray(tfs, dtype=np.float32))
            ids, tfs = self._frozen[term]
            df = len(ids)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            denom = tfs + self.k1 * (1.0 - self.b + self.b * lens[ids] / avg)
            out[ids] += idf * (tfs * (self.k1 + 1.0) / denom) * min(qtf, 3)
        return out


def code_identifiers(text: str) -> set[str]:
    """Identifiers a piece of prose names: backticked, called, snake_case, camelCase or dotted."""
    idents: set[str] = set()
    for chunk in _BACKTICK.findall(text):
        idents.update(_IDENT.findall(chunk))
    for ident in _IDENT.findall(text):
        if "_" in ident.strip("_") or re.search(r"[a-z][A-Z]", ident) or re.match(r"[A-Z][a-z]+[A-Z]", ident):
            idents.add(ident)
    idents.update(re.findall(r"\b([A-Za-z_]\w{2,})\(", text))
    for dotted in _DOTTED.findall(text):
        idents.update(dotted.split("."))
    return {i for i in idents if len(i) >= 4 and i.lower() not in STOP}


@dataclass
class Mentions:
    """Graph nodes named outright in a piece of text."""
    files: dict[int, float] = field(default_factory=dict)  # file id -> strength (any kind)
    # The two kinds are kept apart because they are not equally trustworthy: a
    # path usually comes from a traceback (where the error surfaced, not where
    # the bug is), a name is something the author chose to write.
    paths: dict[int, float] = field(default_factory=dict)
    names: dict[int, float] = field(default_factory=dict)
    symbols: dict[int, float] = field(default_factory=dict)  # symbol id -> strength
    evidence: dict[int, list[str]] = field(default_factory=dict)  # file id -> matched strings

    def _hit(self, file_id: int, strength: float, what: str, kind: str = "names") -> None:
        bucket = self.paths if kind == "paths" else self.names
        bucket[file_id] = max(bucket.get(file_id, 0.0), strength)
        self.files[file_id] = max(self.files.get(file_id, 0.0), strength)
        ev = self.evidence.setdefault(file_id, [])
        if what not in ev and len(ev) < 4:
            ev.append(what)


class MentionLinker:
    """Resolve file paths, dotted names and identifiers in text to graph nodes."""

    MAX_NAME_FILES = 8  # an identifier defined in more files than this is too ambiguous to use

    def __init__(self, files: list[dict], symbols: list[dict]):
        self.path_id = {f["path"]: f["id"] for f in files}
        self.module_id = {f["module"]: f["id"] for f in files if f["module"]}
        self.suffix: dict[str, list[int]] = defaultdict(list)
        for f in files:
            parts = f["path"].split("/")
            for i in range(len(parts)):
                self.suffix["/".join(parts[i:])].append(f["id"])
        self.by_name: dict[str, list[dict]] = defaultdict(list)
        self.by_qual: dict[str, list[dict]] = defaultdict(list)
        for s in symbols:
            self.by_name[s["name"]].append(s)
            if "." in s["qualname"]:
                self.by_qual[s["qualname"]].append(s)

    def link(self, text: str) -> Mentions:
        m = Mentions()
        # 1. file paths, matched by longest path suffix
        for raw in set(_PATH.findall(text)):
            p = raw.replace("\\", "/").lstrip("./")
            parts = p.split("/")
            for i in range(len(parts)):
                ids = self.suffix.get("/".join(parts[i:]))
                if ids and len(ids) <= 3:
                    depth = len(parts) - i
                    for fid in ids:
                        m._hit(fid, min(1.0, 0.5 + 0.25 * depth) / len(ids), raw, "paths")
                    break
        # 2. dotted names: module, module.symbol, Class.method
        for dotted in set(_DOTTED.findall(text)):
            parts = dotted.split(".")
            for i in range(len(parts), 0, -1):
                fid = self.module_id.get(".".join(parts[:i]))
                if fid is not None and i >= 2:
                    m._hit(fid, 1.0 if i == len(parts) else 0.9, dotted)
                    break
            for i in range(len(parts) - 1):
                cands = self.by_qual.get(".".join(parts[i : i + 2]), [])
                if 0 < len({c["file_id"] for c in cands}) <= self.MAX_NAME_FILES:
                    share = 1.0 / len({c["file_id"] for c in cands})
                    for c in cands:
                        m.symbols[c["id"]] = max(m.symbols.get(c["id"], 0.0), share)
                        m._hit(c["file_id"], 0.9 * share, dotted)
        # 3. identifiers that look like code (CamelCase, snake_case, backticked, or called)
        idents: set[str] = set()
        for chunk in _BACKTICK.findall(text):
            idents.update(_IDENT.findall(chunk))
        for ident in _IDENT.findall(text):
            if "_" in ident.strip("_") or re.search(r"[a-z][A-Z]", ident) or re.match(r"[A-Z][a-z]+[A-Z]", ident):
                idents.add(ident)
        idents.update(re.findall(r"\b([A-Za-z_]\w{2,})\(", text))
        for ident in idents:
            if len(ident) < 4 or ident.lower() in STOP:
                continue
            cands = self.by_name.get(ident, [])
            files = {c["file_id"] for c in cands}
            if not cands or len(files) > self.MAX_NAME_FILES:
                continue
            share = 1.0 / len(files)
            for c in cands:
                # classes and functions are stronger evidence than a like-named method
                strength = (0.8 if c["kind"] != "method" else 0.5) * share
                m.symbols[c["id"]] = max(m.symbols.get(c["id"], 0.0), strength)
                m._hit(c["file_id"], strength, ident)
        return m
