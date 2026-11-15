"""Command line entry point: ``trailmark <command>``."""

from __future__ import annotations

import argparse
import sys
import time

from .hydra import Hydra, HydraError


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that HydraDB is reachable and supports every query form Trailmark uses."""
    db = Hydra()
    print(f"HydraDB at {db.config.url} (graph={db.config.graph}, cell={db.config.cell})")
    if not db.health():
        print("  FAIL  not reachable. Start it with:  wsl -e bash -lc \"~/start-hydradb.sh\"")
        return 1
    print("  ok    health")
    a, b, c = 990_000_001, 990_000_002, 990_000_003
    checks: list[tuple[str, object]] = []

    def step(name: str, fn) -> None:
        try:
            ok = fn()
            checks.append((name, bool(ok) if ok is not None else True))
        except HydraError as e:
            checks.append((name, f"{e.code}: {e.message}"))

    step("node batch", lambda: db.batch(
        "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:TmProbe, n.name = row.name",
        [{"id": a, "name": "a"}, {"id": b, "name": "b"}, {"id": c, "name": "c"}]))
    step("edge batch", lambda: db.batch(
        "UNWIND $rows AS row CREATE (a {id: row.src})-[:TM_PROBE]->(b {id: row.dst})",
        [{"src": a, "dst": b}, {"src": b, "dst": c}]))
    step("edge weight SET", lambda: db.run("MATCH (a {id: $s})-[r:TM_PROBE]->(b {id: $d}) SET r.w = $w", s=a, d=b, w=2.5))
    step("read edge weight", lambda: db.query(
        "MATCH (a:TmProbe {id: $s})-[r:TM_PROBE]->(b) RETURN b.id AS id, r.w AS w", s=a) == [{"id": b, "w": 2.5}])
    step("two-hop traversal", lambda: [r["id"] for r in db.query(
        "MATCH (a:TmProbe {id: $s})-[:TM_PROBE]->(m)-[:TM_PROBE]->(b) RETURN b.id AS id", s=a)] == [c])
    step("bounded variable-length", lambda: sorted(r["id"] for r in db.query(
        "MATCH (a:TmProbe {id: $s})-[:TM_PROBE*1..2]->(b) RETURN b.id AS id", s=a)) == [b, c])
    step("incoming traversal", lambda: [r["id"] for r in db.query(
        "MATCH (b:TmProbe {id: $s})<-[:TM_PROBE]-(a) RETURN a.id AS id", s=c)] == [b])
    step("algo.SPpaths", lambda: len(db.query(
        f"CALL algo.SPpaths({{sourceNode: {a}, targetNode: {c}, relTypes: ['TM_PROBE'], maxLen: 3, "
        "relDirection: 'outgoing', pathCount: 1}) YIELD path, pathWeight, pathCost RETURN path, pathWeight, pathCost"
    )[0]["path"]["nodes"]) == 3)
    step("cleanup", lambda: db.run("MATCH (n:TmProbe) DETACH DELETE n"))

    failed = 0
    for name, result in checks:
        if result is True:
            print(f"  ok    {name}")
        else:
            failed += 1
            print(f"  FAIL  {name}: {result}")
    print("all checks passed" if not failed else f"{failed} check(s) failed")
    return 1 if failed else 0


def cmd_ingest(args: argparse.Namespace) -> int:
    import json

    from .ingest import build_graph, history_tasks, load, load_history, save_sidecar, verify

    t0 = time.perf_counter()
    print(f"parsing {args.repo} ...")
    graph, commits = build_graph(args.repo, before=args.before, slot=args.slot)
    print(f"  parsed in {time.perf_counter() - t0:.1f}s: {graph.stats()}")
    if graph.parse_errors:
        print(f"  {len(graph.parse_errors)} file(s) could not be parsed (skipped)")
    out = save_sidecar(args.name, graph, commits, {"name": args.name, "before": args.before, "slot": args.slot})
    history = history_tasks(graph, commits, slot=args.slot)
    (out / "history.json").write_text(json.dumps(history), encoding="utf-8")
    print(f"  sidecar: {out} ({len(history)} past commits usable as experience)")
    if args.dry_run:
        return 0
    db = Hydra()
    if not db.health():
        print("HydraDB is not reachable; run `trailmark doctor`.")
        return 1
    if not args.verify_only:
        print("loading into HydraDB ...")
        load(db, graph)
        load_history(db, history)
    print("verifying ...")
    bad = 0
    for name, (expected, actual) in verify(db, graph).items():
        mark = "ok  " if expected == actual else "DIFF"
        bad += expected != actual
        print(f"  {mark}  {name}: expected {expected}, HydraDB has {actual}")
    print(f"done in {time.perf_counter() - t0:.1f}s, {db.queries} requests")
    return 1 if bad else 0


def cmd_locate(args: argparse.Namespace) -> int:
    from .index import Workspace
    from .retrieve import Config, Retriever

    query = args.query if args.query != "-" else sys.stdin.read()
    ws = Workspace(args.name)
    db = None if args.no_graph else Hydra()
    cfg = Config(use_dense=args.dense, use_graph=not args.no_graph, include_tests=args.tests, top_k=args.k)
    t0 = time.perf_counter()
    res = Retriever(ws, db, cfg).locate(query)
    ms = (time.perf_counter() - t0) * 1000
    for i, h in enumerate(res.hits, 1):
        print(f"{i:2d}. {h.score:6.3f}  {h.path}")
        for r in h.reasons:
            print(f"            - {r}")
    print(f"\n{len(res.candidates)} candidates from {len(res.seeds)} seeds, {res.db_queries} HydraDB reads, {ms:.0f} ms")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trailmark", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check the HydraDB connection and required query forms").set_defaults(fn=cmd_doctor)

    pi = sub.add_parser("ingest", help="parse a repository and load its code graph into HydraDB")
    pi.add_argument("repo", help="path to a git repository")
    pi.add_argument("--name", required=True, help="short name for this repository")
    pi.add_argument("--before", help="only mine git history before this date (YYYY-MM-DD)")
    pi.add_argument("--slot", type=int, default=0, help="id range for this repository; use a different slot per repository")
    pi.add_argument("--dry-run", action="store_true", help="parse and write the sidecar, skip HydraDB")
    pi.add_argument("--verify-only", action="store_true", help="skip loading; only check HydraDB against the parse")
    pi.set_defaults(fn=cmd_ingest)

    pl = sub.add_parser("locate", help="rank the files most relevant to a task description")
    pl.add_argument("query", help="task or issue text ('-' reads stdin)")
    pl.add_argument("--name", required=True, help="repository name used at ingest")
    pl.add_argument("-k", type=int, default=10, help="number of results")
    pl.add_argument("--dense", action="store_true", help="add embedding search to the seeds")
    pl.add_argument("--no-graph", action="store_true", help="text search only (baseline)")
    pl.add_argument("--tests", action="store_true", help="include test files")
    pl.set_defaults(fn=cmd_locate)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
