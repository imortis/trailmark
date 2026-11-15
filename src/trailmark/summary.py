"""Build results/summary.json from the raw per-task records.

One place that turns logs into the numbers shown in the README and the live
demo, so nothing is typed by hand.

Usage:  python -m trailmark.summary [results_dir]
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

from .agent_report import bootstrap_ratio

CONDITIONS = {"baseline": "baseline", "flat": "trailmark-flat", "graph": "trailmark", "learning": "trailmark-learn"}
BUCKETS = 3


def agent_summary(log: Path, model: str) -> dict | None:
    if not log.exists():
        return None
    by: dict[str, dict[str, dict]] = {}
    order: dict[str, int] = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("error") or r.get("is_error") or not r.get("tokens"):
            continue
        by.setdefault(r["key"], {})[r["condition"]] = r
        if r.get("order"):
            order[r["key"]] = r["order"]
    present = [name for name, cond in CONDITIONS.items() if any(cond in v for v in by.values())]
    keys = [k for k, v in by.items() if all(CONDITIONS[n] in v for n in present)]
    keys.sort(key=lambda k: order.get(k, 0))
    if not keys:
        return None
    n = len(keys)
    out: dict = {"model": model, "tasks": n, "conditions": present}
    for name in present:
        rs = [by[k][CONDITIONS[name]] for k in keys]
        out[name] = {
            "all_found": sum(r["all_found"] for r in rs) / n,
            "first_correct": sum(r["top1"] for r in rs) / n,
            "tokens": st.mean(r["tokens"] for r in rs),
            "turns": st.mean(r["turns"] or 0 for r in rs),
            "cost_usd": st.mean(r["cost_usd"] or 0 for r in rs),
            "wall_s": st.mean(r["wall_s"] for r in rs),
        }
        if name != "baseline":
            for metric in ("tokens", "turns"):
                base = [float(by[k]["baseline"][metric] or 0) for k in keys]
                treat = [float(r[metric] or 0) for r in rs]
                lo, hi = bootstrap_ratio(base, treat)
                out[name][f"{metric}_change"] = sum(treat) / sum(base) - 1
                out[name][f"{metric}_ci95"] = [lo - 1, hi - 1]
            b = [by[k]["baseline"]["all_found"] for k in keys]
            t = [r["all_found"] for r in rs]
            out[name]["only_this"] = sum(y and not x for x, y in zip(b, t))
            out[name]["only_baseline"] = sum(x and not y for x, y in zip(b, t))
    # learning curve: the task stream cut into equal parts, in date order
    size = n / BUCKETS
    cuts = [(round(i * size), round((i + 1) * size)) for i in range(BUCKETS)]
    out["curve"] = {
        "buckets": [f"tasks {a + 1}–{b}" for a, b in cuts],
        "rows": [
            {"name": name, "all_found": [sum(by[k][CONDITIONS[name]]["all_found"] for k in keys[a:b]) / (b - a) for a, b in cuts],
             "tokens": [st.mean(by[k][CONDITIONS[name]]["tokens"] for k in keys[a:b]) for a, b in cuts]}
            for name in present
        ],
    }
    return out


def retrieval_summary(results: Path, name: str, systems: list[str]) -> dict:
    out: dict = {}
    for system in systems:
        f = results / f"{name}.{system}.json"
        if not f.exists():
            continue
        data = json.loads(f.read_text(encoding="utf-8"))
        rows = data["rows"]
        size = len(rows) / BUCKETS
        cuts = [(round(i * size), round((i + 1) * size)) for i in range(BUCKETS)]
        top5 = lambda part: sum(all(r is not None and r <= 5 for r in row["ranks"]) for row in part) / len(part)  # noqa: E731
        out[system] = {**{k: data["summary"][k] for k in ("tasks", "acc@1", "acc@3", "acc@5", "acc@10", "recall@10", "median_ms")},
                       "acc@5_by_third": [top5(rows[a:b]) for a, b in cuts]}
    return out


def main(argv: list[str] | None = None) -> int:
    results = Path((argv or sys.argv[1:] or ["results"])[0])
    haiku = agent_summary(results / "agent-v3" / "reef.haiku.jsonl", "Claude Haiku")
    sonnet = agent_summary(results / "agent-v3" / "reef.sonnet.jsonl", "Claude Sonnet")
    systems = ["bm25", "dense", "hybrid", "text+", "graph", "trailmark", "no-uses", "no-trails", "no-structure", "no-history", "no-policy"]
    summary: dict = {
        "retrieval": {"reef": retrieval_summary(results, "reef", systems), "django": retrieval_summary(results, "django-swe", systems)},
        "agent_haiku": haiku,
        "agent_sonnet": sonnet,
    }
    if haiku:
        summary["agent"] = {"model": haiku["model"], "tasks": haiku["tasks"],
                            **{name: {k: haiku[name][k] for k in ("all_found", "tokens", "turns")} for name in haiku["conditions"]}}
        labels = {"baseline": "Search tools only", "flat": "Trailmark tool on a plain store",
                  "graph": "Trailmark on HydraDB", "learning": "Trailmark on HydraDB, learning"}
        summary["learning_curve"] = {
            "buckets": haiku["curve"]["buckets"],
            "rows": [{"name": labels[r["name"]], "values": r["all_found"]} for r in haiku["curve"]["rows"]],
        }
        summary["note"] = ("Reef was created after the model's training cutoff. The graph was built from the code 200 commits "
                           "back; tasks are later commits in date order. In the learning condition the graph is told each "
                           "task's real outcome before the next task.")
    # learning from every merge (stream.py) and the held-out curves (curve.py)
    for repo, name in (("django", "django-swe"), ("reef", "reef")):
        f = results / f"{name}.stream.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            data.pop("per_task", None)
            summary.setdefault("stream", {})[repo] = data
        for variant in ("full", "with-policy", "no-trails", "experience-only"):
            f = results / f"{name}.curve.{variant}.json"
            if f.exists():
                summary.setdefault("curves", {}).setdefault(repo, {})[variant] = json.loads(f.read_text(encoding="utf-8"))
    django_stream = summary.get("stream", {}).get("django")
    if django_stream:
        years = list(django_stream["by_year"])
        names = {"plain": "Plain store (text only)", "frozen": "Graph frozen at 2015", "learning": "Graph learning from every merge"}
        summary["learning_curve"] = {
            "title": f"Django, {django_stream['tasks']} SWE-bench issues, 2015-2023: all changed files in the top 5",
            "buckets": years,
            "rows": [{"name": names[n], "values": [django_stream["by_year"][y][n] for y in years]} for n in names],
        }
    (results / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    for label, s in (("Haiku", haiku), ("Sonnet", sonnet)):
        if not s:
            continue
        print(f"\nAgent, {label}, {s['tasks']} tasks")
        print(f"{'':10s}{'all found':>10s}{'first ok':>10s}{'tokens':>10s}{'turns':>7s}{'tok chg':>9s}{'only this / only base':>24s}")
        for name in s["conditions"]:
            c = s[name]
            chg = f"{c['tokens_change']:+.1%}" if "tokens_change" in c else ""
            disc = f"{c['only_this']} / {c['only_baseline']}" if "only_this" in c else ""
            print(f"{name:10s}{c['all_found']:>10.1%}{c['first_correct']:>10.1%}{c['tokens']:>10.0f}{c['turns']:>7.1f}{chg:>9s}{disc:>24s}")
        print("  all files found by thirds of the stream:")
        for r in s["curve"]["rows"]:
            print(f"    {r['name']:10s}" + "  ".join(f"{v:6.1%}" for v in r["all_found"]) + "   tokens " + "  ".join(f"{v:8.0f}" for v in r["tokens"]))
    for repo, table in summary["retrieval"].items():
        print(f"\nRetrieval, {repo}")
        for system, r in table.items():
            print(f"  {system:14s} n={r['tasks']:4d}  top1 {r['acc@1']:.3f}  top3 {r['acc@3']:.3f}  top5 {r['acc@5']:.3f}  top10 {r['acc@10']:.3f}"
                  f"   top5 by third: " + " ".join(f"{v:.3f}" for v in r["acc@5_by_third"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
