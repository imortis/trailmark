"""Load a parsed code graph into HydraDB and keep a local sidecar.

HydraDB holds structure, weights and (later) experience. The sidecar holds
what HydraDB OSS cannot serve: text for lexical/dense seed search and the
path <-> id map.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .gitmine import Commit, cochange_edges, read_commits
from .hydra import Hydra
from .parse import CodeGraph, parse_repo

WEIGHTED = ("REFS", "COCHANGED")  # edge types whose ``w`` property is stored in HydraDB
SIDECAR_DIR = ".trailmark"

NODE_FILE = "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:File, n.path = row.path, n.loc = row.loc, n.is_test = row.is_test, n.imp_in = row.imp_in, n.ref_in = row.ref_in"
NODE_SYMBOL = (
    "UNWIND $rows AS row MERGE (n {id: row.id}) "
    "SET n:Symbol, n.name = row.name, n.qualname = row.qualname, n.kind = row.kind, n.path = row.path, n.line = row.line"
)


def edge_batch(kind: str) -> str:
    return f"UNWIND $rows AS row CREATE (a {{id: row.src}})-[:{kind}]->(b {{id: row.dst}})"


def edge_set_weight(kind: str) -> str:
    return f"MATCH (a {{id: $src}})-[r:{kind}]->(b {{id: $dst}}) SET r.w = $w"


def sidecar_path(name: str, base: str | Path = ".") -> Path:
    return Path(base) / SIDECAR_DIR / name


SLOT_SIZE = 100_000_000  # ids per repository slot: files, symbols and tasks all live inside it


def build_graph(repo: str, before: str | None = None, rev: str = "HEAD", slot: int = 0) -> tuple[CodeGraph, list[Commit]]:
    """Parse the working tree and add co-change edges from history before the cutoff."""
    graph = parse_repo(repo, id_offset=slot * SLOT_SIZE)
    path_id = {f.path: f.id for f in graph.files}
    commits = read_commits(repo, before=before, rev=rev)
    for (a, b), w in cochange_edges(commits, set(path_id)).items():
        graph.add_edge("COCHANGED", path_id[a], path_id[b], w)
    return graph, commits


HISTORY_ID_BASE = 89_000_000  # within a slot: above every run's task range
HISTORY_MAX_FILES = 10        # a commit touching more source files than this is a sweep, not a task
HISTORY_KIND = "TOUCHED"
_TICKET_PREFIX = re.compile(r"^(\[[^\]]+\]\s*)?((fixed|refs|closes|fixes)\s+#\d+(,\s*#\d+)*\s*(--|:|-)?\s*)", re.I)


def history_tasks(graph: CodeGraph, commits: list[Commit], slot: int = 0) -> list[dict]:
    """Past commits as tasks: the subject is the task text, the changed source files are the answer.

    This is the experience a repository already has on day one. Only commits
    handed in are used, so a ``--before`` cutoff at ingest keeps later history out.
    """
    by_path = {f.path: f for f in graph.files}
    out: list[dict] = []
    for c in commits:
        files = [by_path[p].id for p in dict.fromkeys(c.files) if p in by_path and not by_path[p].is_test]
        if not files or len(files) > HISTORY_MAX_FILES:
            continue
        text = _TICKET_PREFIX.sub("", c.subject).strip()
        if len(text) < 12:
            continue
        out.append({"id": slot * SLOT_SIZE + HISTORY_ID_BASE + len(out), "key": c.sha[:12], "ts": c.ts, "text": text, "files": files})
    return out


def load_history(db: Hydra, tasks: list[dict], log: Callable[[str], None] = print) -> None:
    """Write history as ``(:Task)-[:TOUCHED]->(:File)``."""
    t = time.perf_counter()
    db.batch(
        "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Task, n.key = row.key, n.ts = row.ts",
        [{"id": x["id"], "key": x["key"], "ts": x["ts"]} for x in tasks],
    )
    edges = [{"src": x["id"], "dst": f} for x in tasks for f in x["files"]]
    db.batch(edge_batch(HISTORY_KIND), edges)
    log(f"  history: {len(tasks)} past commits as Task nodes, {len(edges)} TOUCHED edges in {time.perf_counter() - t:.1f}s")


def load(db: Hydra, graph: CodeGraph, log: Callable[[str], None] = print) -> dict[str, float]:
    """Write nodes, edges and weights. Idempotent: MERGE nodes, deduped edge batches."""
    timings: dict[str, float] = {}

    t = time.perf_counter()
    # In-degrees are stored on the node so a traversal can damp hub files
    # (a utility module imported by 400 files is weak evidence) in one read.
    imp_in: dict[int, int] = {}
    ref_in: dict[int, int] = {}
    for (_, b) in graph.edges.get("IMPORTS", {}):
        imp_in[b] = imp_in.get(b, 0) + 1
    for (_, b) in graph.edges.get("REFS", {}):
        ref_in[b] = ref_in.get(b, 0) + 1
    db.batch(
        NODE_FILE,
        [
            {"id": f.id, "path": f.path, "loc": f.loc, "is_test": f.is_test,
             "imp_in": imp_in.get(f.id, 0), "ref_in": ref_in.get(f.id, 0)}
            for f in graph.files
        ],
    )
    db.batch(
        NODE_SYMBOL,
        [
            {"id": s.id, "name": s.name, "qualname": s.qualname, "kind": s.kind, "path": s.path, "line": s.line}
            for s in graph.symbols
        ],
    )
    db.batch(
        "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Name, n.text = row.text, n.files = row.files",
        [{"id": nid, "text": text, "files": n} for text, (nid, n) in graph.names.items()],
    )
    timings["nodes"] = time.perf_counter() - t
    log(f"  nodes: {len(graph.files)} files, {len(graph.symbols)} symbols in {timings['nodes']:.1f}s")

    for kind, bucket in sorted(graph.edges.items()):
        t = time.perf_counter()
        db.batch(edge_batch(kind), [{"src": a, "dst": b} for (a, b) in bucket])
        timings[kind] = time.perf_counter() - t
        log(f"  {kind}: {len(bucket)} edges in {timings[kind]:.1f}s")

    for kind in WEIGHTED:
        bucket = graph.edges.get(kind, {})
        if not bucket:
            continue
        t = time.perf_counter()
        db.run_many(edge_set_weight(kind), ({"src": a, "dst": b, "w": float(w)} for (a, b), w in bucket.items()), workers=16)
        timings[f"{kind}.w"] = time.perf_counter() - t
        log(f"  {kind} weights: {len(bucket)} in {timings[f'{kind}.w']:.1f}s")
    return timings


def save_sidecar(name: str, graph: CodeGraph, commits: list[Commit], meta: dict, base: str | Path = ".") -> Path:
    out = sidecar_path(name, base)
    out.mkdir(parents=True, exist_ok=True)
    (out / "files.json").write_text(json.dumps([asdict(f) for f in graph.files]), encoding="utf-8")
    (out / "symbols.json").write_text(json.dumps([asdict(s) for s in graph.symbols]), encoding="utf-8")
    (out / "names.json").write_text(json.dumps(graph.names), encoding="utf-8")
    (out / "commits.json").write_text(json.dumps([asdict(c) for c in commits]), encoding="utf-8")
    (out / "meta.json").write_text(
        json.dumps({**meta, "root": graph.root, "stats": graph.stats(), "parse_errors": graph.parse_errors}, indent=2),
        encoding="utf-8",
    )
    return out


def verify(db: Hydra, graph: CodeGraph, sample: int = 150, seed: int = 0) -> dict[str, tuple[int, int]]:
    """Check HydraDB against the parsed graph. Returns {name: (expected, actual)}.

    Everything is sampled, because whole-graph scans exceed HydraDB's 30 s
    query deadline: a random set of nodes per label is looked up by id, and a
    random set of source nodes per edge type has its full out-neighbourhood
    (and weights, where stored) compared.
    """
    out: dict[str, tuple[int, int]] = {}
    rng = random.Random(seed)
    for label, recs, prop in (("File", graph.files, "path"), ("Symbol", graph.symbols, "qualname")):
        picked_nodes = rng.sample(recs, min(sample, len(recs)))
        res = db.query_many(f"MATCH (n:{label} {{id: $id}}) RETURN n.{prop} AS v", [{"id": r.id} for r in picked_nodes])
        hits = sum(1 for r, rows in zip(picked_nodes, res) if rows and rows[0]["v"] == getattr(r, prop))
        out[f"{label} nodes (sample)"] = (len(picked_nodes), hits)
    for kind, bucket in sorted(graph.edges.items()):
        by_src: dict[int, dict[int, float]] = {}
        for (a, b), w in bucket.items():
            by_src.setdefault(a, {})[b] = w
        picked = rng.sample(sorted(by_src), min(sample, len(by_src)))
        results = db.query_many(
            f"MATCH (a {{id: $src}})-[r:{kind}]->(b) RETURN b.id AS id, r.w AS w", [{"src": s} for s in picked]
        )
        expected = actual = 0
        for src, rows in zip(picked, results):
            want = by_src[src]
            got = {r["id"]: r["w"] for r in rows}
            expected += len(want)
            for dst, w in want.items():
                if dst not in got:
                    continue
                if kind in WEIGHTED and (got[dst] is None or abs(got[dst] - w) > 1e-6):
                    continue
                actual += 1
            actual -= len(set(got) - set(want))  # unexpected extra edges count against the match
        out[f"{kind} edges (sample of {len(picked)} sources)"] = (expected, actual)
    return out
