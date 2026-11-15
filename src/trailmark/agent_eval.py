"""Agent-level evaluation: the same coding agent, with and without Trailmark.

For each task the agent (Claude Code, headless) is asked to name the files a
change must touch. Conditions differ only in the tools available:

  baseline          Read, Grep, Glob
  trailmark         those + Trailmark's MCP tools, graph as ingested (no learning)
  trailmark-flat    the same tool answering from text indexes only: no graph
                    traversal, usage edges, history or learning. The control
                    for "would this work on a plain store?"
  trailmark-learn   the same, and after every task Trailmark is told which files
                    the real change touched, as it would be when a pull request
                    merges. Tasks run in date order, so each task sees only what
                    earlier ones taught.

Recorded per run: the files named, tokens processed, cost, turns and wall
time. Runs are appended to a JSONL file and skipped if already present, so
the evaluation can be stopped and resumed. On resume, a learning condition
replays the outcomes of its finished tasks into a fresh run namespace, so the
graph is in the same state as if it had never stopped.

Usage:  python -m trailmark.agent_eval --name reef --commits <repo> --since <rev> --task change
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .bench import get_tasks
from .hydra import Hydra
from .index import Workspace
from .learn import LearnConfig, Learner
from .retrieve import Config, Retriever

BUILTIN_TOOLS = "Read,Grep,Glob"
TRAILMARK_TOOLS = ["mcp__trailmark__locate", "mcp__trailmark__neighbors", "mcp__trailmark__impact", "mcp__trailmark__why"]
MAX_FILES = 5

_ENDING = """End your reply with exactly one line of JSON in this form, listing at most {max_files} repository-relative paths, most likely first:
{{"files": ["path/to/file.py"]}}"""

PROMPTS = {
    # a bug report or feature request, as in SWE-bench
    "issue": """You are triaging an issue filed against the repository in the current directory.

Find the existing source files (not tests, not docs) that must be modified to resolve it. Do not write the fix. Use the tools available to you, and stop as soon as you are confident.

""" + _ENDING + """

<issue>
{issue}
</issue>""",
    # a description of a change to make, as taken from a repository's own history
    "change": """You are planning a change to the repository in the current directory. The change is described below; it has not been made yet.

Find the existing source files (not tests, not docs) that must be modified to make it. Do not write the change. Use the tools available to you, and stop as soon as you are confident.

""" + _ENDING + """

<change>
{issue}
</change>""",
}

# The lines a team would put in CLAUDE.md after installing Trailmark.
TRAILMARK_HINT = (
    "This repository is indexed by Trailmark. Call mcp__trailmark__locate with the full task text "
    "before searching, and read the line ranges it suggests rather than whole files. Its result also "
    "lists every file that uses the identifiers the task names, and files that past changes touched "
    "together; a change is complete only when each of those has been considered."
)


def uses_trailmark(condition: str) -> bool:
    return condition.startswith("trailmark")


def build_command(condition: str, model: str, mcp_config: Path, max_turns: int) -> list[str]:
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError("the `claude` CLI is not on PATH")
    allowed = BUILTIN_TOOLS.split(",") + (TRAILMARK_TOOLS if uses_trailmark(condition) else [])
    cmd = [
        exe, "-p", "--output-format", "json", "--model", model,
        "--tools", BUILTIN_TOOLS,
        "--allowedTools", ",".join(allowed),
        # Only the MCP servers named here, so the user's own servers cannot add tools or tokens.
        "--strict-mcp-config", "--mcp-config", str(mcp_config),
        "--max-turns", str(max_turns),
    ]
    if uses_trailmark(condition):
        cmd += ["--append-system-prompt", TRAILMARK_HINT]
    return cmd


def parse_answer(text: str) -> list[str]:
    for m in reversed(re.findall(r"\{[^{}]*\"files\"[^{}]*\}", text or "", flags=re.S)):
        try:
            files = json.loads(m).get("files", [])
        except ValueError:
            continue
        out = []
        for f in files:
            if isinstance(f, str):
                f = f.strip().replace("\\", "/").lstrip("./")
                if f and f not in out:
                    out.append(f)
        return out[:MAX_FILES]
    return []


def run_one(cmd: list[str], prompt: str, cwd: Path, timeout: int) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(cwd), env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "wall_s": round(time.perf_counter() - t0, 1)}
    wall = round(time.perf_counter() - t0, 1)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"error": "unparseable output", "stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:], "wall_s": wall}
    if isinstance(data, list):  # some versions emit an event array; the result event is last
        data = next((e for e in reversed(data) if isinstance(e, dict) and e.get("type") == "result"), {})
    usage = data.get("usage") or {}
    return {
        "wall_s": wall,
        "is_error": bool(data.get("is_error")),
        "subtype": data.get("subtype"),
        "result": data.get("result") or "",
        "turns": data.get("num_turns"),
        "cost_usd": data.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


def write_mcp_config(path: Path, name: str, home: Path, run: str = "", flat: bool = False) -> None:
    env = {"TRAILMARK_WORKSPACE": name, "TRAILMARK_HOME": str(home)}
    if flat:
        env["TRAILMARK_FLAT"] = "1"
    if run:
        env["TRAILMARK_RUN"] = run
    for k in ("TRAILMARK_MODEL_DIR", "HYDRADB_URL", "HYDRADB_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    path.write_text(json.dumps({"mcpServers": {"trailmark": {
        "command": sys.executable, "args": ["-m", "trailmark.mcp_server"], "env": env}}}, indent=1), encoding="utf-8")


class Teacher:
    """Feeds real outcomes to Trailmark between tasks, as a merge hook would."""

    def __init__(self, ws: Workspace, run: str):
        self.ws = ws
        self.state_file = ws.dir / f"learn.{run}.json"
        self.state_file.unlink(missing_ok=True)
        dense = (ws.dir / "dense.npy").exists()
        self.learner = Learner(Retriever(ws, Hydra(), Config(use_dense=dense, top_k=30)), Hydra(), LearnConfig(run=run))

    def teach(self, task) -> None:
        res = self.learner.locate(task.text)
        self.learner.record(task.text, res, task.gold, key=task.key, ts=task.ts)
        self.learner.save(self.state_file)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True)
    p.add_argument("--repo")
    p.add_argument("--commits", help="path to a git repository whose commits are the tasks")
    p.add_argument("--since", help="with --commits: the revision the graph was ingested from")
    p.add_argument("--task", choices=("issue", "change"), default="issue", help="how the task text is framed to the agent")
    p.add_argument("--parquet", default="data/swebench/test.parquet")
    p.add_argument("--n", type=int, default=60, help="number of tasks, evenly spaced through the task stream")
    p.add_argument("--offset", type=int, default=0, help="shift the evenly spaced sample")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--conditions", default="baseline,trailmark")
    p.add_argument("--max-turns", type=int, default=30)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--out", default="results/agent")
    args = p.parse_args(argv)

    home = Path.cwd().resolve()
    ws = Workspace(args.name)
    tasks, _ = get_tasks(args, ws)
    step = max(1, len(tasks) // args.n)
    picked = tasks[args.offset :: step][: args.n]
    picked_keys = {t.key for t in picked}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / f"{args.name}.{args.model}.jsonl"
    done: set[tuple[str, str]] = set()
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("error"):
                done.add((r["key"], r["condition"]))

    conditions = [c.strip() for c in args.conditions.split(",")]
    configs: dict[str, Path] = {}
    teachers: dict[str, Teacher] = {}
    empty = out_dir / "mcp.empty.json"
    empty.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    for cond in conditions:
        if not uses_trailmark(cond):
            configs[cond] = empty.resolve()
            continue
        run = ""
        if cond.endswith("-learn"):
            # A fresh namespace every start: learned edges from an earlier,
            # interrupted start can never leak into this one.
            run = f"e{int(time.time()) % 100_000_000}"
            teachers[cond] = Teacher(ws, run)
        cfg = out_dir / f"mcp.{cond}.json"
        write_mcp_config(cfg, args.name, home, run, flat=cond.endswith("-flat"))
        configs[cond] = cfg.resolve()

    print(f"{len(picked)} tasks x {conditions}, model={args.model}, log={log}", flush=True)
    # Tasks are in date order. A learning condition must see every picked task
    # in order, including finished ones (their outcomes are replayed).
    # The graph learns from every task in the stream, whether or not the agent
    # is sampled on it: merges teach Trailmark even when no agent was involved.
    i = 0
    for task in tasks:
        sampled = task.key in picked_keys
        i += sampled
        gold = [ws.file_by_id[g]["path"] for g in task.gold]
        prompt = PROMPTS[args.task].format(issue=task.text.strip(), max_files=MAX_FILES)
        for cond in conditions:
            if sampled and (task.key, cond) not in done:
                cmd = build_command(cond, args.model, configs[cond], args.max_turns)
                r = run_one(cmd, prompt, ws.root, args.timeout)
                files = parse_answer(r.get("result", ""))
                r.update(key=task.key, condition=cond, order=i, gold=gold, files=files,
                         top1=bool(files) and files[0] in gold, all_found=bool(gold) and all(g in files for g in gold),
                         any_found=any(g in files for g in gold))
                r["tokens"] = sum(r.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_tokens", "cache_read_tokens", "output_tokens"))
                with log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(r) + "\n")
                print(f"[{i}/{len(picked)}] {task.key} {cond:16s} top1={int(r['top1'])} all={int(r['all_found'])} "
                      f"turns={r.get('turns')} tokens={r['tokens']} cost={r.get('cost_usd')} {r.get('wall_s')}s {r.get('error') or ''}", flush=True)
                if r.get("error"):
                    # Stop here rather than teach past a task that has no result:
                    # the next start resumes from this task with the graph replayed.
                    if cond in teachers:
                        print("stopping: a learning-condition run failed; rerun to resume", flush=True)
                        return 1
            if cond in teachers:
                teachers[cond].teach(task)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
