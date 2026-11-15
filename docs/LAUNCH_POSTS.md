# Launch posts

Post from your own accounts. Tag HydraDB: **@hydra_db** on X (from their GitHub
profile). On LinkedIn, type "@HydraDB" and pick the company page from the list.
Attach the demo GIF or a screenshot of the three-column comparison. Replace
`<SPACE_URL>` with the live link.

The organisers also want to see "who wants it". Things that count, and are
honest: replies from people who say they'd use it, GitHub stars, someone
installing it on their own repository, a team agreeing to a trial. Screenshot
them for the Loom and the submission.

---

## X / Twitter (thread)

**1/**
Coding agents burn most of their tokens just finding where a change belongs.

I built Trailmark on @hydra_db's open-source graph engine: it walks a graph of
your codebase instead, and the graph learns from every merged change.

38% fewer tokens, 45% fewer turns, same accuracy. 🧵

**2/**
Test: Claude Haiku names the files a change needs, on a repo created after its
training cutoff (101 real commits).

Search tools only: 171k tokens, 10.3 turns per task
With Trailmark: 106k tokens, 5.7 turns, slightly more tasks fully right

**3/**
How: every file, symbol and identifier is a node in HydraDB. Imports, calls,
inheritance, "uses this name", "changed together" are edges.

One `locate` call returns the files, the functions to read, the code, and why,
plus every file that touches the names in the task.

**4/**
It keeps learning. Each merge adds task nodes, co-fix counts and trails (only
where HydraDB's shortest-path finds a real route).

On a repo the model never saw, a fixed held-out set climbs from 46% to 54% as
merges are absorbed. On Django it doesn't move. Both are in the README.

**5/**
Honest bits, all in the repo:
• the same tool on a plain store keeps most of the token saving; the graph adds
  fewer turns, more complete multi-file answers, and the learning
• an online re-weighting step made learning dip 17 pts; switched off

Try it: <SPACE_URL>
Code: github.com/imortis/trailmark

---

## LinkedIn

Coding agents spend most of their tokens before writing a line of code: searching
and reading files to work out where a change belongs. For teams running agents
at volume, that orientation step is a large part of the bill.

For the HydraDB hackathon I built Trailmark, a context layer that answers "where
does this change go?" by walking a graph of the codebase in @HydraDB's
open-source engine, and that learns from every merged change.

What I measured, on a repository created after the model's training cutoff
(101 real changes, Claude Haiku):
• 38% fewer tokens and 45% fewer turns per task, at the same accuracy
• a fixed held-out benchmark that climbs from 46% to 54% as the graph absorbs
  merges, and a plain store that stays at 46% (on Django, learning made no
  difference, and the write-up says so)
• a control with the graph switched off, so the graph's own contribution is
  measured rather than assumed

Everything learned is stored as inspectable graph structure: task nodes, co-fix
counts, and trails grounded by HydraDB's shortest-path procedure.

Who I think pays for this: teams running background agents on tickets, and
agent products whose margin is token cost. If that's you, I'd like to hear how
you'd use it.

Live demo: <SPACE_URL>
Code and every per-task result: github.com/imortis/trailmark

#AIagents #DeveloperTools #GraphDatabase #HydraDB
