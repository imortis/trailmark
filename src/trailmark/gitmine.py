"""Mine git history: commits (as past tasks) and file co-change edges.

Everything takes a ``before`` cutoff so a benchmark can guarantee that
nothing from the evaluation period leaks into the graph.
"""

from __future__ import annotations

import math
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass

MAX_FILES_PER_COMMIT = 30  # bigger commits are sweeping refactors, not signal
MIN_COCHANGE = 2
TOP_PARTNERS = 12


@dataclass
class Commit:
    sha: str
    ts: int
    subject: str
    files: list[str]
    body: str = ""


def read_commits(repo: str, before: str | None = None, after: str | None = None, rev: str = "HEAD") -> list[Commit]:
    """Non-merge commits reachable from ``rev`` (a revision or a range), oldest first."""
    cmd = ["git", "-C", repo, "log", rev, "--no-merges", "--name-only", "--pretty=format:%x01%H%x02%ct%x02%s%x02%b%x03"]
    if before:
        cmd.append(f"--before={before}")
    if after:
        cmd.append(f"--after={after}")
    out = subprocess.run(cmd, capture_output=True, check=True).stdout.decode("utf-8", errors="replace")
    commits: list[Commit] = []
    for block in out.split("\x01")[1:]:
        header, _, rest = block.partition("\x03")
        sha, ts, subject, body = (header.split("\x02") + ["", "", ""])[:4]
        files = [ln.strip() for ln in rest.splitlines() if ln.strip()]
        commits.append(Commit(sha=sha, ts=int(ts or 0), subject=subject, files=files, body=body.strip()))
    commits.reverse()
    return commits


def cochange_edges(commits: list[Commit], known_paths: set[str]) -> dict[tuple[str, str], float]:
    """Directed, normalised co-change weights between files that still exist.

    weight(a -> b) = cochange(a, b) / sqrt(changes(a) * changes(b)), in (0, 1].
    Each file keeps only its strongest ``TOP_PARTNERS`` partners.
    """
    changes: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    for c in commits:
        files = sorted({f for f in c.files if f in known_paths})
        if not files or len(files) > MAX_FILES_PER_COMMIT:
            continue
        changes.update(files)
        for i, a in enumerate(files):
            for b in files[i + 1 :]:
                pairs[(a, b)] += 1

    partners: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for (a, b), n in pairs.items():
        if n < MIN_COCHANGE:
            continue
        w = n / math.sqrt(changes[a] * changes[b])
        partners[a].append((w, b))
        partners[b].append((w, a))

    out: dict[tuple[str, str], float] = {}
    for a, lst in partners.items():
        lst.sort(reverse=True)
        for w, b in lst[:TOP_PARTNERS]:
            out[(a, b)] = round(w, 4)
    return out
