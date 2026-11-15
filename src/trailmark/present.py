"""Turning a ranking into what an agent (or a person) reads.

Shared by the MCP server and the web demo, so both show exactly the same thing.
"""

from __future__ import annotations

from .index import Workspace
from .learn import Learner
from .retrieve import Result
from .text import tokenize

SNIPPET_FILES = 4       # leading results that get their best function inlined
SNIPPET_LINES = 30
USAGE_IDENTIFIERS = 8   # identifiers reported, rarest first
USAGE_FILES = 10        # files listed per identifier
COFIX_SOURCES = 4       # leading results whose co-changed files are reported


def top_symbols(ws: Workspace, file_ids: list[int], task: str, mentioned: dict[int, float], n: int = 3) -> dict[int, list[dict]]:
    """The functions inside each file that match the task best, with line ranges to read."""
    scores = ws.sym_bm25.scores(tokenize(task))
    wanted = set(file_ids)
    best: dict[int, list[tuple[float, dict]]] = {}
    for row, s in enumerate(ws.symbols):
        if s["file_id"] in wanted:
            score = float(scores[row]) * (2.0 if s["id"] in mentioned else 1.0)
            if score > 0:
                best.setdefault(s["file_id"], []).append((score, s))
    out: dict[int, list[dict]] = {}
    for fid, items in best.items():
        items.sort(key=lambda x: -x[0])
        top = items[0][0]
        out[fid] = [
            {"symbol": s["qualname"], "lines": f"{s['line']}-{s['end_line']}"}
            for score, s in items[:n] if score >= 0.4 * top
        ]
    return out


def snippet(ws: Workspace, path: str, lines: str) -> str:
    start, end = (int(x) for x in lines.split("-"))
    try:
        text = (ws.root / path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    last = min(end, start + SNIPPET_LINES - 1)
    body = "\n".join(text[start - 1 : last])
    return body + (f"\n... ({end - last} more lines)" if end > last else "")


def changed_together(ws: Workspace, learner: Learner, sources: list[int]) -> list[dict]:
    """Files that past tasks changed alongside each source file: ``(:File)-[:COFIX {n}]->(:File)``."""
    if not sources:
        return []
    results = learner.db.query_many(
        f"MATCH (a {{id: $id}})-[r:{learner.cofix_kind}]->(b) RETURN b.id AS id, r.n AS n", [{"id": s} for s in sources]
    )
    out: list[dict] = []
    for src, rows in zip(sources, results):
        rows = [r for r in rows if r["id"] in ws.file_by_id]
        rows.sort(key=lambda r: -(r["n"] or 0))
        if rows:
            out.append({
                "file": ws.file_by_id[src]["path"],
                "with": [{"path": ws.file_by_id[r["id"]]["path"], "times": r["n"]} for r in rows[:6]],
            })
    return out


def usage(ws: Workspace, res: Result) -> list[dict]:
    """Every file that uses each identifier the task names (from USES edges), rarest identifier first."""
    return [
        {"name": ident, "used_in": [ws.file_by_id[f]["path"] for f in fids[:USAGE_FILES]],
         **({"and_more": len(fids) - USAGE_FILES} if len(fids) > USAGE_FILES else {})}
        for ident, fids in res.usage[:USAGE_IDENTIFIERS]
    ]


def locate_payload(ws: Workspace, learner: Learner, res: Result, task: str, k: int, with_cofix: bool = True) -> dict:
    """The full ``locate`` answer: ranked files with reasons, functions to read and inline code,
    usage sites, and files changed together."""
    hits = res.hits[:k]
    symbols = top_symbols(ws, [h.file_id for h in hits], task, res.mention_symbols)
    files = [{"path": h.path, "why": h.reasons[:3], "read_first": symbols.get(h.file_id, [])} for h in hits]
    # Inline the best-matching function of the leading files, so the agent can
    # often judge relevance without a separate read.
    for entry in files[:SNIPPET_FILES]:
        if entry["read_first"]:
            entry["code"] = snippet(ws, entry["path"], entry["read_first"][0]["lines"])
    out: dict = {"files": files}
    ids = usage(ws, res)
    if ids:
        out["identifiers"] = ids
    if with_cofix:
        together = changed_together(ws, learner, [h.file_id for h in hits[:COFIX_SOURCES]])
        if together:
            out["changed_together"] = together
    return out
