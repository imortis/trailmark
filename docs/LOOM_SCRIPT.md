# Loom walkthrough (about 4 minutes)

Record screen and camera. Tabs to have open, in order:
1. The demo: https://imortis.github.io/trailmark/ (or the live version, if you run it locally with `python -m trailmark.web`).
2. The GitHub README, scrolled to "Results".
3. A terminal in the repository (optional, for one command).

---

## 0:00–0:25  The problem and the claim

**Say:** "Coding agents spend most of their tokens just finding where a change
belongs. On a codebase the model has never seen, Claude Haiku took ten turns
and 171,000 tokens per task just to name the right files. Trailmark is a
context layer on HydraDB's open-source engine: it answers that question by
walking a graph of the codebase, and the graph learns from every merged change."

## 0:25–1:30  Live demo

**Do:** in the demo, click a change in the left list (pick one with two or more
answer files; the sixth one, a dispatcher change touching three files, shows the
learning graph moving the answers up).

**Say:** "This is Reef, a repository created after the model's training cutoff.
These changes were merged later than the snapshot the graph was built from, so
nothing here has seen them. Three stores answer at once."

**Point at, column by column:**
- *Plain store*: text search only. "This is what you get without a graph."
- *Graph in HydraDB*: "the same question answered by walking imports, calls,
  inheritance and a node for every identifier. Under 'who uses the names in
  this task' you see USES edges: every file that touches a name, in one read.
  That's the agent's grep loop, replaced."
- *Graph after learning*: "the same graph after it was taught sixty merged
  changes. Under 'changed together' are COFIX edges with counts, learned from
  those merges. A plain store has nowhere to keep that."

**Point at:** the green rows (the files the real change touched) and the
"N of M answer files in the top 5" line in each column, and the HydraDB read
count and milliseconds.

## 1:30–2:30  The metrics that matter

**Show:** README, "Metrics" and the agent table.

**Say:** "We measured what a team pays for: tokens and turns an agent spends to
find where a change goes, at the same accuracy. And completeness: did it find
all the files a change needs, not just the first one."

"Same model, same tasks, only the tools differ. With Trailmark: 38 percent fewer
tokens, 45 percent fewer turns, and slightly more tasks fully right."

"Then the control judges asked for: the same tool with the graph switched off.
It keeps most of the token saving. That tells us honestly what the graph adds
on a single question: fewer turns and more complete answers on multi-file
changes. The graph's real job is the next part."

## 2:30–3:30  Continual learning against a baseline

**Show:** README, "Continual learning" table and the curve.

**Say:** "A fixed held-out set of changes, re-scored as the graph is taught more
merges. Two baselines on the same set: a plain store, and the graph before any
teaching. The learned graph climbs from 46 to 54 percent; the plain store stays
at 46. And an agent using the learning graph spends 13 percent fewer tokens in
the second half of the stream than one using the frozen graph."

"I'll be straight about the size: these are modest effects and each interval
touches zero on its own. On Django, learning from five thousand merges made no
difference at all. We report that too."

"We also caught a bug this way. An online re-weighting step made the curve dip
nearly 20 points before it recovered. We switched it off by default and kept it in the
repo as an ablation. Every number, including what didn't work, is in the
repository with per-task records."

## 3:30–4:00  Why HydraDB, and who pays

**Say:** "Everything learned is graph structure in HydraDB: task nodes, trails
grounded by HydraDB's shortest-path procedure, co-fix counts. You can query
why a file was suggested and delete a trail that's wrong. That's the
difference from a vector store."

"Who pays: teams running agents on tickets at volume, and agent products whose
margin is token cost. Priced per locate call, below the saving it produces.
Trailmark: structure instead of tokens."
