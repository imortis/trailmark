"""Continual-learning curve: a fixed held-out test set, re-scored as the graph learns.

The task stream is split by date into a training stream and a held-out test
set (the newest tasks). The graph is taught the training stream in order, one
merged change at a time; at each checkpoint the held-out set is scored without
learning from it. Two baselines are scored on the same held-out set: text
indexes alone (a plain store) and the graph as ingested (never taught).

Usage:
  python -m trailmark.curve --name reef --commits data/repos/reef --since <rev> --holdout 41 --step 10 --no-dense
  python -m trailmark.curve --name django-swe --repo django/django --holdout 200 --step 100
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import numpy as np

from .bench import get_tasks
from .hydra import Hydra
from .index import Workspace
from .learn import LearnConfig, Learner
from .retrieve import Config, Retriever

K = 5


def score(learner: Learner, tasks, qvecs) -> list[bool]:
    """For each task: are all of its changed files in the top K?"""
    learner.r.query_vectors = qvecs
    out = []
    for t in tasks:
        ranked = [h.file_id for h in learner.locate(t.text).hits[:K]]
        out.append(all(g in ranked for g in t.gold))
    return out


def paired_ci(a: list[bool], b: list[bool], rounds: int = 4000, seed: int = 0) -> tuple[float, float, float]:
    """Mean of b - a with a 95% bootstrap interval, resampling tasks."""
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
    p.add_argument("--parquet", default="data/swebench/test.parquet")
    p.add_argument("--holdout", type=int, required=True, help="newest N tasks form the fixed test set")
    p.add_argument("--step", type=int, default=10, help="training tasks between checkpoints")
    p.add_argument("--no-dense", action="store_true")
    p.add_argument("--variant", default="full", help="full | with-policy | no-trails | experience-only")
    p.add_argument("--out", default="results")
    args = p.parse_args(argv)

    ws = Workspace(args.name)
    db = Hydra()
    tasks, _ = get_tasks(args, ws)
    train, test = tasks[: -args.holdout], tasks[-args.holdout :]
    dense = not args.no_dense and (ws.dir / "dense.npy").exists()
    qvecs: dict = {}
    if dense:
        data = np.load(ws.dir / "task_vectors.npz")
        stored = dict(zip(data["keys"].tolist(), data["vecs"]))
        qvecs = {t.text: stored[t.key] for t in tasks if t.key in stored}
    print(f"{args.name}: {len(train)} training tasks, {len(test)} held-out tasks, top-{K}, variant={args.variant}", flush=True)

    stamp = int(time.time()) % 10_000_000
    off = dict(use_history=False, use_experience=False, use_trails=False, use_policy=False, use_heat=False)
    flat_cfg = Config(use_dense=dense, use_graph=False, use_symgraph=False, use_uses=False, kinds=(), top_k=30)
    plain = Learner(Retriever(ws, None, flat_cfg), db, LearnConfig(run=f"cp{stamp}", **off))
    variants = {
        "full": {},
        "with-policy": {"use_policy": True},
        "no-trails": {"use_trails": False},
        "experience-only": {"use_trails": False},
    }
    learner = Learner(Retriever(ws, db, Config(use_dense=dense, top_k=30)), db, LearnConfig(run=f"cl{stamp}", **variants[args.variant]))

    plain_scores = score(plain, test, qvecs)
    static_scores = score(learner, test, qvecs)  # the graph as ingested, before any teaching
    print(f"  plain store              {np.mean(plain_scores):.3f}", flush=True)
    print(f"  graph, taught 0          {np.mean(static_scores):.3f}", flush=True)

    checkpoints = [{"taught": 0, "acc": float(np.mean(static_scores))}]
    t0 = time.perf_counter()
    for i, task in enumerate(train, 1):
        learner.r.query_vectors = qvecs
        res = learner.locate(task.text)
        learner.record(task.text, res, task.gold, key=task.key, ts=task.ts)
        if i % args.step == 0 or i == len(train):
            s = score(learner, test, qvecs)
            diff, lo, hi = paired_ci(static_scores, s)
            checkpoints.append({"taught": i, "acc": float(np.mean(s)), "vs_static": diff, "ci95": [lo, hi]})
            print(f"  graph, taught {i:<9d} {np.mean(s):.3f}   vs untaught {diff:+.3f} (95% CI {lo:+.3f} to {hi:+.3f})   "
                  f"[{time.perf_counter() - t0:.0f}s]", flush=True)
            final = s
    diff, lo, hi = paired_ci(plain_scores, final)
    print(f"  final vs plain store     {diff:+.3f} (95% CI {lo:+.3f} to {hi:+.3f})", flush=True)

    out = {
        "name": args.name, "variant": args.variant, "k": K, "train": len(train), "holdout": len(test),
        "plain": float(np.mean(plain_scores)), "static": float(np.mean(static_scores)),
        "checkpoints": checkpoints, "final_vs_plain": {"diff": diff, "ci95": [lo, hi]},
        "final_weights": {k: round(v, 3) for k, v in learner.r.config.weights.items()},
    }
    Path(args.out, f"{args.name}.curve.{args.variant}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
