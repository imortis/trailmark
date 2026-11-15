"""Summarise an agent evaluation log: baseline against one Trailmark condition, paired by task.

Only tasks with a successful run in both conditions are compared.

Usage:  python -m trailmark.agent_report results/agent-v3/reef.haiku.jsonl [trailmark|trailmark-learn]
"""

from __future__ import annotations

import json
import random
import statistics as st
import sys
from pathlib import Path

METRICS = (("tokens", "tokens processed"), ("turns", "turns"), ("cost_usd", "cost (USD)"), ("wall_s", "wall time (s)"))


def load_pairs(path: str | Path, treat: str = "trailmark") -> list[tuple[dict, dict]]:
    """(baseline run, treatment run) for every task that has both."""
    by: dict[str, dict[str, dict]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("error") or r.get("is_error") or not r.get("tokens"):
            continue
        by.setdefault(r["key"], {})[r["condition"]] = r  # a later successful retry replaces an earlier one
    return [(v["baseline"], v[treat]) for v in by.values() if "baseline" in v and treat in v]


def bootstrap_ratio(base: list[float], treat: list[float], rounds: int = 4000, seed: int = 0) -> tuple[float, float]:
    """95% interval for sum(treat)/sum(base), resampling tasks."""
    rng = random.Random(seed)
    n = len(base)
    out = []
    for _ in range(rounds):
        idx = [rng.randrange(n) for _ in range(n)]
        b = sum(base[i] for i in idx)
        out.append(sum(treat[i] for i in idx) / b if b else 1.0)
    out.sort()
    return out[int(0.025 * rounds)], out[int(0.975 * rounds)]


def summarise(pairs: list[tuple[dict, dict]]) -> dict:
    n = len(pairs)
    out: dict = {"tasks": n, "conditions": {}, "change": {}}
    if not n:
        return out
    for name, rs in (("baseline", [b for b, _ in pairs]), ("treatment", [t for _, t in pairs])):
        out["conditions"][name] = {
            "first_file_correct": sum(r["top1"] for r in rs) / n,
            "all_files_found": sum(r["all_found"] for r in rs) / n,
            "any_file_found": sum(r["any_found"] for r in rs) / n,
            **{m: {"mean": st.mean(float(r.get(m) or 0) for r in rs), "median": st.median(float(r.get(m) or 0) for r in rs)} for m, _ in METRICS},
        }
    for m, _ in METRICS:
        base = [float(b.get(m) or 0) for b, _ in pairs]
        treat = [float(t.get(m) or 0) for _, t in pairs]
        lo, hi = bootstrap_ratio(base, treat)
        out["change"][m] = {"ratio": sum(treat) / sum(base), "ci95": [lo, hi],
                            "tasks_lower": sum(t < b for b, t in zip(base, treat)) / n}
    out["all_found_table"] = {
        "both": sum(b["all_found"] and t["all_found"] for b, t in pairs),
        "baseline_only": sum(b["all_found"] and not t["all_found"] for b, t in pairs),
        "treatment_only": sum(t["all_found"] and not b["all_found"] for b, t in pairs),
        "neither": sum(not b["all_found"] and not t["all_found"] for b, t in pairs),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    path, treat = args[0], (args[1] if len(args) > 1 else "trailmark")
    s = summarise(load_pairs(path, treat))
    print(f"{s['tasks']} tasks with both conditions complete\n")
    if not s["tasks"]:
        return 1
    print(f"{'':22s}{'baseline':>12s}{treat:>17s}")
    for key, label in (("first_file_correct", "first file correct"), ("all_files_found", "all files found"), ("any_file_found", "any file found")):
        print(f"{label:22s}{s['conditions']['baseline'][key]:>12.1%}{s['conditions']['treatment'][key]:>17.1%}")
    for m, label in METRICS:
        b, t = s["conditions"]["baseline"][m]["mean"], s["conditions"]["treatment"][m]["mean"]
        c = s["change"][m]
        print(f"{'mean ' + label:22s}{b:>12.4g}{t:>17.4g}   {c['ratio'] - 1:+.1%} (95% CI {c['ci95'][0] - 1:+.1%} to {c['ci95'][1] - 1:+.1%}), lower on {c['tasks_lower']:.0%} of tasks")
    print(f"\nall files found: {s['all_found_table']}")
    Path(path).with_suffix(f".{treat}.summary.json").write_text(json.dumps(s, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
