"""Online file-localisation benchmark on SWE-bench.

Tasks for one repository are replayed in the order they were created. For
each task a system ranks files from the issue text alone, is scored against
the files the real fix changed, and only then (if it learns) is told the
answer. Nothing from a later task is visible to an earlier one.

Usage:  python -m trailmark.bench --name django-swe --repo django/django
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .hydra import Hydra
from .index import Workspace
from .learn import LearnConfig, Learner
from .retrieve import Config, Retriever

KS = (1, 3, 5, 10)
STRUCTURE = ("IMPORTS", "REFS", "COCHANGED")


@dataclass
class Task:
    key: str
    text: str
    ts: int
    gold: list[int]  # file ids present in the graph


def load_tasks(parquet: str, repo: str, ws: Workspace) -> tuple[list[Task], dict[str, int]]:
    import pyarrow.parquet as pq

    rows = [r for r in pq.read_table(parquet).to_pylist() if r["repo"] == repo]
    rows.sort(key=lambda r: r["created_at"])
    tasks: list[Task] = []
    info = {"instances": len(rows), "gold_files": 0, "gold_missing_from_snapshot": 0, "skipped_no_gold": 0}
    for r in rows:
        paths = sorted(set(re.findall(r"^diff --git a/(\S+) b/", r["patch"], flags=re.M)))
        gold = []
        for p in paths:
            info["gold_files"] += 1
            fid = ws.path_id.get(p)
            if fid is None or ws.file_by_id[fid]["is_test"]:
                info["gold_missing_from_snapshot"] += 1
            else:
                gold.append(fid)
        if not gold:
            info["skipped_no_gold"] += 1
            continue
        ts = int(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp())
        tasks.append(Task(r["instance_id"], r["problem_statement"], ts, gold))
    return tasks, info


_PR_SUFFIX = re.compile(r"\s*\(#\d+\)\s*$")
_TRAILER = re.compile(r"^(co-authored-by|signed-off-by|reviewed-by|change-id|bug|test|tested):", re.I)


def load_commit_tasks(repo: str, since: str, ws: Workspace, max_files: int = 5) -> tuple[list[Task], dict[str, int]]:
    """Tasks from a repository's own commits after ``since``: message in, changed files out.

    For repositories with no issue dataset. The graph must have been ingested
    from the ``since`` commit, so nothing a task changes is visible in advance.
    Only source files that already existed at ``since`` count as answers.
    """
    from .gitmine import read_commits

    commits = read_commits(repo, rev=f"{since}..HEAD")
    tasks: list[Task] = []
    info = {"instances": len(commits), "gold_files": 0, "skipped_no_gold": 0, "skipped_too_many_files": 0}
    for c in commits:
        gold = [ws.path_id[p] for p in dict.fromkeys(c.files) if p in ws.path_id and not ws.file_by_id[ws.path_id[p]]["is_test"]]
        if not gold:
            info["skipped_no_gold"] += 1
            continue
        if len(gold) > max_files:
            info["skipped_too_many_files"] += 1
            continue
        body = "\n".join(ln for ln in c.body.splitlines() if not _TRAILER.match(ln.strip())).strip()
        text = _PR_SUFFIX.sub("", c.subject).strip() + ("\n\n" + body if body else "")
        info["gold_files"] += len(gold)
        tasks.append(Task(c.sha[:12], text, c.ts, gold))
    return tasks, info


def get_tasks(args, ws: Workspace) -> tuple[list[Task], dict[str, int]]:
    if getattr(args, "commits", None):
        return load_commit_tasks(args.commits, args.since, ws)
    return load_tasks(args.parquet, args.repo, ws)


# name -> (retrieval config, learning config or None, description)
def systems() -> dict[str, tuple[Config, LearnConfig | None, str]]:
    plain = dict(use_title=False, use_sym=False, use_stems=False, use_graph=False, use_symgraph=False, use_uses=False)
    nograph = dict(use_dense=True, use_graph=False, use_symgraph=False, use_uses=False)
    full = dict(use_dense=True)
    return {
        # baselines without a graph
        "bm25": (Config(use_dense=False, use_mentions=False, **plain), None, "keyword search over files"),
        "dense": (Config(use_bm25=False, use_dense=True, use_mentions=False, **plain), None, "vector search over files"),
        "hybrid": (Config(use_dense=True, **plain), None, "keyword + vector + mention linking"),
        "text+": (Config(**nograph), None, "every text signal (title, file names, function-level), no graph"),
        # with HydraDB
        "graph": (Config(**full, kinds=STRUCTURE), None, "text signals + static code graph, fixed weights"),
        "trailmark": (Config(**full), LearnConfig(), "graph + usage + history + experience + trails + co-fix"),
        "trailmark-warm": (Config(**full), LearnConfig(warmup=400), "trailmark, policy pre-trained by replaying 400 past commits"),
        # ablations: remove one thing from trailmark
        "no-structure": (Config(**nograph, kinds=()), LearnConfig(use_trails=False), "trailmark without code-structure edges (experience only)"),
        "no-history": (Config(**full), LearnConfig(use_history=False), "trailmark without git history as experience"),
        "no-experience": (Config(**full), LearnConfig(use_history=False, use_experience=False, use_heat=False), "trailmark without any experience"),
        "no-uses": (Config(**full, use_uses=False), LearnConfig(), "trailmark without identifier-usage edges"),
        "with-policy": (Config(**full), LearnConfig(use_policy=True), "trailmark plus online re-weighting of evidence"),
        "no-trails": (Config(**full, kinds=STRUCTURE), LearnConfig(use_trails=False), "trailmark without trails"),
        "ungrounded-trails": (Config(**full), LearnConfig(ground_trails=False), "trailmark, trails laid without a structural path"),
    }


def run_system(name: str, ws: Workspace, db: Hydra, tasks: list[Task], qvecs: dict[str, np.ndarray], tokens: dict[int, int], no_dense: bool = False) -> dict:
    cfg, lcfg, desc = systems()[name]
    if no_dense:
        cfg.use_dense = False
    cfg.top_k = 50
    retriever = Retriever(ws, db if cfg.use_graph or cfg.use_symgraph or cfg.use_uses or lcfg else None, cfg)
    retriever.query_vectors = qvecs
    learner = None
    if lcfg is not None:
        lcfg.run = f"b{int(time.time()) % 10_000_000}"
        learner = Learner(retriever, db, lcfg)
    rows = []
    reads0, t0 = db.queries, time.perf_counter()
    for task in tasks:
        t = time.perf_counter()
        res = learner.locate(task.text) if learner else retriever.locate(task.text)
        ms = (time.perf_counter() - t) * 1000
        rank = {h.file_id: i for i, h in enumerate(res.hits, 1)}
        ranks = [rank.get(g) for g in task.gold]
        cover = None
        if all(r is not None for r in ranks):
            cover = sum(tokens[h.file_id] for h in res.hits[: max(ranks)])
        rows.append({"key": task.key, "ranks": ranks, "cover_tokens": cover, "ms": round(ms, 1)})
        if learner:
            learner.record(task.text, res, task.gold, key=task.key, ts=task.ts)
    out = {
        "system": name, "description": desc, "rows": rows,
        "seconds": round(time.perf_counter() - t0, 1), "db_requests": db.queries - reads0,
        "summary": summarize(rows),
    }
    if learner:
        out["final_weights"] = {k: round(v, 3) for k, v in retriever.config.weights.items()}
        out["trails"] = len(learner.trails)
        out["run"] = lcfg.run
    return out


def summarize(rows: list[dict]) -> dict:
    n = len(rows) or 1
    out: dict[str, float] = {"tasks": len(rows)}
    for k in KS:
        # acc@k: every changed file is in the top k (the strict, agent-relevant measure)
        out[f"acc@{k}"] = round(sum(all(r is not None and r <= k for r in row["ranks"]) for row in rows) / n, 4)
    for k in (5, 10):
        out[f"recall@{k}"] = round(sum(sum(r is not None and r <= k for r in row["ranks"]) / len(row["ranks"]) for row in rows) / n, 4)
    out["mrr"] = round(sum(max((1.0 / r for r in row["ranks"] if r), default=0.0) for row in rows) / n, 4)
    covered = [row["cover_tokens"] for row in rows if row["cover_tokens"] is not None]
    out["covered_in_top50"] = round(len(covered) / n, 4)
    out["median_cover_tokens"] = int(np.median(covered)) if covered else 0
    out["median_ms"] = round(float(np.median([row["ms"] for row in rows])), 1) if rows else 0
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="workspace name used at ingest")
    p.add_argument("--repo", help="SWE-bench repo, e.g. django/django")
    p.add_argument("--commits", help="instead of SWE-bench: path to a git repository whose commits are the tasks")
    p.add_argument("--since", help="with --commits: the revision the graph was ingested from")
    p.add_argument("--parquet", default="data/swebench/test.parquet")
    p.add_argument("--systems", default="bm25,dense,hybrid,text+,graph,trailmark")
    p.add_argument("--limit", type=int, default=0, help="only the first N tasks")
    p.add_argument("--no-dense", action="store_true", help="run every system without the embedding model")
    p.add_argument("--out", default="results")
    args = p.parse_args(argv)

    ws = Workspace(args.name)
    db = Hydra()
    tasks, info = get_tasks(args, ws)
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"{args.repo or args.commits}: {info} -> {len(tasks)} tasks")

    names = [s.strip() for s in args.systems.split(",") if s.strip()]
    no_dense = args.no_dense
    qvecs: dict[str, np.ndarray] = {}
    if not no_dense and any(systems()[n][0].use_dense for n in names):
        cache = ws.dir / "task_vectors.npz"
        if cache.exists():
            data = np.load(cache)
            stored = dict(zip(data["keys"].tolist(), data["vecs"]))
        else:
            stored = {}
        missing = [t for t in tasks if t.key not in stored]
        if missing:
            print(f"embedding {len(missing)} task texts ...")
            vecs = ws.embed([t.text[:2000] for t in missing])
            stored.update({t.key: v for t, v in zip(missing, vecs)})
            np.savez(cache, keys=np.asarray(list(stored)), vecs=np.stack(list(stored.values())))
        qvecs = {t.text: stored[t.key] for t in tasks}
        _ = ws.dense

    tokens = {}
    for f in ws.files:
        try:
            tokens[f["id"]] = (ws.root / f["path"]).stat().st_size // 4  # ~4 bytes per token
        except OSError:
            tokens[f["id"]] = f["loc"] * 10

    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)
    cols = ["acc@1", "acc@3", "acc@5", "acc@10", "recall@10", "mrr", "median_cover_tokens", "median_ms"]
    print(f"{'system':22s} " + " ".join(f"{c:>10s}" for c in cols))
    for name in names:
        res = run_system(name, ws, db, tasks, qvecs, tokens, no_dense)
        res["info"] = info
        tag = f"{args.name}{'-' + str(args.limit) if args.limit else ''}"
        (out_dir / f"{tag}.{name}.json").write_text(json.dumps(res), encoding="utf-8")
        s = res["summary"]
        print(f"{name:22s} " + " ".join(f"{s[c]:>10}" for c in cols) + f"   ({res['seconds']}s, {res['db_requests']} db requests)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
