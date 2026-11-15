"""Learning from every merge: a graph that keeps absorbing commits vs one frozen at ingest.

This is how Trailmark is meant to run: a merge hook tells it which files every
merged change touched. We replay a repository's real history. Each task is
scored at its own date by three stores:

  plain     text indexes only (what a plain, non-graph store could answer)
  frozen    the graph as ingested: code structure plus git history up to the cutoff
  learning  the same graph, having absorbed every merged commit dated before the task

Nothing dated on or after a task is visible to it. For SWE-bench, commits that
already contain an issue's fix (the dataset's dates are issue dates, and a few
fixes were committed earlier) are excluded from the stream entirely, so no
answer can leak (see `find_leaks`).

Usage:
  python -m trailmark.stream --name django-swe --repo django/django --after 2015-08-19
  python -m trailmark.stream --name reef --commits data/repos/reef --since <rev> --no-dense
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .bench import get_tasks
from .gitmine import Commit, read_commits
from .hydra import Hydra
from .index import Workspace
from .ingest import HISTORY_MAX_FILES, _TICKET_PREFIX, edge_batch
from .learn import NODE_TASK, LearnConfig, Learner
from .retrieve import Config, Retriever

K = 5
LEAK_OVERLAP = 0.6  # share of a fix's added lines that, found in an earlier commit, marks that commit as the fix


def source_files(ws: Workspace, c: Commit) -> list[int]:
    return [ws.path_id[p] for p in dict.fromkeys(c.files) if p in ws.path_id and not ws.file_by_id[ws.path_id[p]]["is_test"]]


def find_leaks(ws: Workspace, commits: list[Commit], tasks, parquet: str, repo: str) -> set[str]:
    """Commits dated before an issue that already contain most of its fix."""
    import pyarrow.parquet as pq

    patches = {r["instance_id"]: r["patch"] for r in pq.read_table(parquet).to_pylist() if r["repo"] == repo}
    added = lambda text: {ln[1:].strip() for ln in text.splitlines()  # noqa: E731
                          if ln.startswith("+") and not ln.startswith("+++") and len(ln.strip()) > 3}
    files = {c.sha: set(source_files(ws, c)) for c in commits}
    ts = [c.ts for c in commits]
    diffs: dict[str, set[str]] = {}
    leaks: set[str] = set()
    for t in tasks:
        want = added(patches.get(t.key, ""))
        if not want:
            continue
        for c in commits[: bisect.bisect_left(ts, t.ts)]:
            if not files[c.sha] & set(t.gold):
                continue
            if c.sha not in diffs:
                out = subprocess.run(["git", "-C", str(ws.root), "show", "--format=", "--no-color", c.sha], capture_output=True)
                diffs[c.sha] = added(out.stdout.decode("utf-8", "replace"))
            if len(want & diffs[c.sha]) / len(want) >= LEAK_OVERLAP:
                leaks.add(c.sha)
    return leaks


def ci(a: list[bool], b: list[bool], rounds: int = 4000, seed: int = 0) -> tuple[float, float, float]:
    rng = random.Random(seed)
    d = [int(y) - int(x) for x, y in zip(a, b)]
    n = len(d)
    means = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(rounds))
    return sum(d) / n, means[int(0.025 * rounds)], means[int(0.975 * rounds)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True)
    p.add_argument("--repo")
    p.add_argument("--commits")
    p.add_argument("--since")
    p.add_argument("--after", help="SWE-bench mode: the ingest cutoff date; commits after it form the stream")
    p.add_argument("--parquet", default="data/swebench/test.parquet")
    p.add_argument("--no-dense", action="store_true")
    p.add_argument("--out", default="results")
    args = p.parse_args(argv)

    ws = Workspace(args.name)
    db = Hydra()
    tasks, _ = get_tasks(args, ws)
    if args.commits:
        stream = read_commits(args.commits, rev=f"{args.since}..HEAD")
    else:
        stream = read_commits(str(ws.root), after=args.after)
    stream = [c for c in stream if 0 < len(source_files(ws, c)) <= HISTORY_MAX_FILES]
    leaks: set[str] = set()
    if args.repo and not args.commits:
        cache = Path(args.out) / f"{args.name}.leaks.json"
        if cache.exists():
            leaks = set(json.loads(cache.read_text(encoding="utf-8")))
        else:
            leaks = find_leaks(ws, stream, tasks, args.parquet, args.repo)
            cache.write_text(json.dumps(sorted(leaks)), encoding="utf-8")
        stream = [c for c in stream if c.sha not in leaks]
    print(f"{args.name}: {len(tasks)} tasks, {len(stream)} merged commits in the stream, {len(leaks)} excluded as containing a fix", flush=True)

    dense = not args.no_dense and (ws.dir / "dense.npy").exists()
    qvecs: dict = {}
    if dense:
        data = np.load(ws.dir / "task_vectors.npz")
        stored = dict(zip(data["keys"].tolist(), data["vecs"]))
        qvecs = {t.text: stored[t.key] for t in tasks if t.key in stored}

    stamp = int(time.time()) % 10_000_000
    off = dict(use_history=False, use_experience=False, use_trails=False, use_policy=False, use_heat=False)
    flat = Config(use_dense=dense, use_graph=False, use_symgraph=False, use_uses=False, kinds=(), top_k=30)
    systems = {
        "plain": Learner(Retriever(ws, None, flat), db, LearnConfig(run=f"sp{stamp}", **off)),
        "frozen": Learner(Retriever(ws, db, Config(use_dense=dense, top_k=30)), db, LearnConfig(run=f"sf{stamp}")),
        "learning": Learner(Retriever(ws, db, Config(use_dense=dense, top_k=30)), db, LearnConfig(run=f"sl{stamp}")),
    }
    for learner in systems.values():
        learner.r.query_vectors = qvecs
    learner = systems["learning"]

    hits: dict[str, list[bool]] = {name: [] for name in systems}
    absorbed = 0
    t0 = time.perf_counter()
    for i, task in enumerate(tasks, 1):
        # merge hook: everything merged before this task was filed
        batch = []
        while absorbed < len(stream) and stream[absorbed].ts < task.ts:
            batch.append(stream[absorbed])
            absorbed += 1
        if batch:
            rows, edges = [], []
            for c in batch:
                learner._recorded += 1
                tid = learner._id_base + learner._recorded
                files = source_files(ws, c)
                rows.append({"id": tid, "key": c.sha[:12], "ts": c.ts})
                edges += [{"src": tid, "dst": f} for f in files]
                learner._register(tid, learner.touched_kind, _TICKET_PREFIX.sub("", c.subject).strip(), files)
            db.batch(NODE_TASK, rows)
            db.batch(edge_batch(learner.touched_kind), edges)
        for name, sys_ in systems.items():
            ranked = [h.file_id for h in sys_.locate(task.text).hits[:K]]
            hits[name].append(all(g in ranked for g in task.gold))
        if i % 100 == 0 or i == len(tasks):
            print(f"  {i:4d} tasks, {absorbed:5d} commits absorbed   " +
                  "  ".join(f"{n} {np.mean(h):.3f}" for n, h in hits.items()) + f"   [{time.perf_counter() - t0:.0f}s]", flush=True)

    years = [datetime.fromtimestamp(t.ts, timezone.utc).year for t in tasks]
    by_year = {}
    for y in sorted(set(years)):
        idx = [i for i, yy in enumerate(years) if yy == y]
        by_year[str(y)] = {"tasks": len(idx), **{n: float(np.mean([hits[n][i] for i in idx])) for n in systems}}
    thirds = [(round(j * len(tasks) / 3), round((j + 1) * len(tasks) / 3)) for j in range(3)]
    out = {
        "name": args.name, "k": K, "tasks": len(tasks), "stream_commits": len(stream), "excluded_leaks": len(leaks),
        "overall": {n: float(np.mean(h)) for n, h in hits.items()},
        "by_year": by_year,
        "by_third": [{n: float(np.mean(hits[n][a:b])) for n in systems} for a, b in thirds],
        "learning_vs_frozen": dict(zip(("diff", "lo", "hi"), ci(hits["frozen"], hits["learning"]))),
        "learning_vs_plain": dict(zip(("diff", "lo", "hi"), ci(hits["plain"], hits["learning"]))),
        "frozen_vs_plain": dict(zip(("diff", "lo", "hi"), ci(hits["plain"], hits["frozen"]))),
        "per_task": [{"key": t.key, **{n: hits[n][i] for n in systems}} for i, t in enumerate(tasks)],
    }
    Path(args.out, f"{args.name}.stream.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("\nall changed files in the top 5")
    print(f"{'':8s}" + "".join(f"{n:>10s}" for n in systems) + "   tasks")
    for y, r in by_year.items():
        print(f"{y:8s}" + "".join(f"{r[n]:>10.1%}" for n in systems) + f"   {r['tasks']}")
    print(f"{'all':8s}" + "".join(f"{out['overall'][n]:>10.1%}" for n in systems) + f"   {len(tasks)}")
    for key in ("learning_vs_frozen", "learning_vs_plain", "frozen_vs_plain"):
        d = out[key]
        print(f"{key:20s} {d['diff']:+.3f} (95% CI {d['lo']:+.3f} to {d['hi']:+.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
