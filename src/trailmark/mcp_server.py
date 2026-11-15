"""MCP server: Trailmark as a context layer for coding agents.

Tools are graph traversals in HydraDB, so an agent finds where to work
without spending tokens reading files to orient itself. ``record_outcome``
closes the loop: tell Trailmark which files a task really changed and the
graph is sharper for the next task.

Run:  TRAILMARK_WORKSPACE=<name> python -m trailmark.mcp_server
Env:  TRAILMARK_HOME (directory holding .trailmark, default cwd), HYDRADB_URL, HYDRADB_TOKEN
"""

from __future__ import annotations

import os
from collections import OrderedDict

try:  # mcp 2.x renamed FastMCP to MCPServer; the decorator API is the same
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from .hydra import Hydra
from .index import Workspace
from .learn import Learner, LearnConfig
from .retrieve import Config, Result, Retriever
from .present import locate_payload

mcp = FastMCP("trailmark")
_state: dict = {}
_recent: "OrderedDict[str, Result]" = OrderedDict()  # task text -> last ranking, for record_outcome


def _ctx() -> tuple[Workspace, Hydra, Retriever, Learner]:
    if not _state:
        name = os.environ.get("TRAILMARK_WORKSPACE")
        if not name:
            raise RuntimeError("set TRAILMARK_WORKSPACE to the name used at `trailmark ingest`")
        ws = Workspace(name, os.environ.get("TRAILMARK_HOME", "."))
        db = Hydra()
        dense = (ws.dir / "dense.npy").exists()
        # TRAILMARK_FLAT=1 is the control for "would this work on a plain store?":
        # the same tool and output format, answering from text indexes alone.
        flat = os.environ.get("TRAILMARK_FLAT") == "1"
        config = Config(use_dense=dense, include_tests=os.environ.get("TRAILMARK_INCLUDE_TESTS") == "1", top_k=30)
        if flat:
            config.use_graph = config.use_symgraph = config.use_uses = False
            config.kinds = ()
        retriever = Retriever(ws, None if flat else db, config)
        # TRAILMARK_RUN scopes the learned edges and state file, so separate
        # experiments (or branches) do not share what they have learned.
        run = os.environ.get("TRAILMARK_RUN", "")
        learn_config = LearnConfig(run=run)
        if flat:
            learn_config = LearnConfig(run=run, use_history=False, use_experience=False, use_trails=False,
                                       use_policy=False, use_heat=False)
        learner = Learner(retriever, db, learn_config)
        state_file = ws.dir / f"learn{('.' + run) if run else ''}.json"
        learner.load(state_file)
        _state.update(ws=ws, db=db, retriever=retriever, learner=learner, state_file=state_file, flat=flat)
    return _state["ws"], _state["db"], _state["retriever"], _state["learner"]


def _file_id(ws: Workspace, path: str) -> int:
    path = path.replace("\\", "/").lstrip("./")
    if path in ws.path_id:
        return ws.path_id[path]
    ids = ws.linker.suffix.get(path, [])
    if len(ids) == 1:
        return ids[0]
    raise ValueError(f"unknown or ambiguous file: {path}")


@mcp.tool()
def locate(task: str, k: int = 8) -> dict:
    """Find the files (and the symbols inside them) most relevant to a task or bug report.

    Call this first, before searching or reading files. Each result says why it
    was chosen, and ``read_first`` gives the functions and line ranges to read
    instead of the whole file; the leading files include that code inline.
    Results are ranked best first.

    ``identifiers`` lists every file that uses each identifier the task names
    (what a search for that name would find). ``changed_together`` lists
    files that past tasks changed in the same commit as a leading result,
    with how many times; a change to one often needs the other.
    """
    ws, _, _, learner = _ctx()
    res = learner.locate(task)
    _recent[task] = res
    while len(_recent) > 32:
        _recent.popitem(last=False)
    out = locate_payload(ws, learner, res, task, k, with_cofix=not _state.get("flat"))
    return {
        **out,
        "graph_reads": res.db_queries,
        "past_tasks_known": len(learner.task_ids),
    }


@mcp.tool()
def neighbors(path: str) -> dict:
    """Structural neighbourhood of a file: what it imports, what depends on it, and what changes with it."""
    ws, db, _, learner = _ctx()
    fid = _file_id(ws, path)
    out: dict[str, list] = {}
    for label, kind, direction in (
        ("imports", "IMPORTS", "out"), ("imported_by", "IMPORTS", "in"),
        ("uses", "REFS", "out"), ("used_by", "REFS", "in"),
        ("changes_with", "COCHANGED", "out"), ("learned_trails_to", learner.trail_kind, "out"),
    ):
        arrow = f"-[r:{kind}]->" if direction == "out" else f"<-[r:{kind}]-"
        rows = db.query(f"MATCH (a {{id: $id}}){arrow}(b) RETURN b.path AS path, r.w AS w", id=fid)
        rows.sort(key=lambda r: -(r["w"] or 0))
        out[label] = [r["path"] if r["w"] is None else {"path": r["path"], "weight": r["w"]} for r in rows[:25]]
    return out


def _dependents(db: Hydra, symbol_id: int, kind: str, depth: int, cap: int = 400) -> list[dict]:
    """Breadth-first walk against edge direction: who reaches this symbol within ``depth`` hops.

    HydraDB only runs variable-length patterns away from a fixed source, so the
    reverse walk is one incoming-edge read per frontier node, fanned out concurrently.
    """
    query = f"MATCH (s {{id: $id}})<-[:{kind}]-(c) RETURN c.id AS id, c.qualname AS symbol, c.path AS path, c.line AS line"
    seen: dict[int, dict] = {}
    frontier = [symbol_id]
    for hop in range(1, depth + 1):
        nxt: list[int] = []
        for rows in db.query_many(query, [{"id": i} for i in frontier], workers=16):
            for r in rows:
                if r["id"] != symbol_id and r["id"] not in seen:
                    seen[r["id"]] = {"symbol": r["symbol"], "path": r["path"], "line": r["line"], "hops": hop}
                    nxt.append(r["id"])
        frontier = nxt[: max(0, cap - len(seen))] if len(seen) < cap else []
        if not frontier:
            break
    return list(seen.values())


@mcp.tool()
def impact(symbol: str, depth: int = 2) -> dict:
    """What could break if this function, method or class changes: its transitive callers and subclasses.

    ``symbol`` is a name or qualified name such as ``QuerySet.bulk_update``.
    """
    ws, db, _, _ = _ctx()
    depth = max(1, min(3, int(depth)))
    matches = [s for s in ws.symbols if s["qualname"] == symbol or s["name"] == symbol][:5]
    if not matches:
        return {"error": f"no symbol named {symbol}"}
    out = []
    for s in matches:
        callers = _dependents(db, s["id"], "CALLS", depth)
        subclasses = _dependents(db, s["id"], "INHERITS", depth)
        tests = sorted({c["path"] for c in callers if ws.file_by_id.get(ws.path_id.get(c["path"], -1), {}).get("is_test")})
        out.append({
            "symbol": s["qualname"], "defined_in": f"{s['path']}:{s['line']}",
            "callers": callers[:60], "caller_count": len(callers),
            "subclasses": subclasses[:40], "tests_that_reach_it": tests[:30],
        })
    return {"matches": out}


@mcp.tool()
def why(from_path: str, to_path: str) -> dict:
    """Explain how two files are connected: the shortest path between them in the graph."""
    ws, _, retriever, _ = _ctx()
    steps = retriever.why(ws.file_by_id[_file_id(ws, from_path)]["path"], ws.file_by_id[_file_id(ws, to_path)]["path"])
    return {"connected": bool(steps), "path": steps or []}


@mcp.tool()
def record_outcome(task: str, changed_files: list[str]) -> dict:
    """Report which files a finished task actually changed, so the graph learns from it.

    Call once when the work is done, with the same task text given to ``locate``.
    """
    ws, _, _, learner = _ctx()
    res = _recent.get(task) or learner.locate(task)
    ids, unknown = [], []
    for p in changed_files:
        try:
            ids.append(_file_id(ws, p))
        except ValueError:
            unknown.append(p)
    rank = {h.file_id: i for i, h in enumerate(res.hits, 1)}
    outcome = learner.record(task, res, ids)
    learner.save(_state["state_file"])
    _recent.pop(task, None)
    return {
        "recorded_task": outcome.task_id,
        "ranks_before_learning": {ws.file_by_id[i]["path"]: rank.get(i) for i in ids},
        "trails_laid": outcome.trails_laid,
        "trails_reinforced": outcome.trails_reinforced,
        "trails_decayed": outcome.trails_decayed,
        "ranking_updates": outcome.policy_pairs,
        "unknown_files": unknown,
        "notes": outcome.notes,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
