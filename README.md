# Trailmark

**A context layer for coding agents, built on the open-source HydraDB engine.**
Trailmark keeps a graph of your codebase in HydraDB and answers "where does this
change belong?" by walking it. Every merged change teaches it something.

On a repository the model had never seen, an agent using Trailmark found the
files a change needs with **38% fewer tokens and 45% fewer turns**, at the same
accuracy (Claude Haiku, 101 real changes).

**Live demo:** <SPACE_URL> · **Walkthrough:** <LOOM_URL>

Built solo in 48 hours for the HydraDB Hackathon (Nov 14–16, 2026).

---

## Contents

1. [What it does](#what-it-does)
2. [Metrics: what we measured and why](#metrics-what-we-measured-and-why)
3. [Result 1: an agent with and without Trailmark](#result-1-an-agent-with-and-without-trailmark)
4. [Result 2: does the graph do the work?](#result-2-does-the-graph-do-the-work)
5. [Result 3: continual learning against a baseline](#result-3-continual-learning-against-a-baseline)
6. [How HydraDB is used](#how-hydradb-is-used)
7. [Run it](#run-it)
8. [Limits](#limits)

---

## What it does

```
task text ──► entry points (keywords, names, file names, function-level match)
                 │
                 ▼
          HydraDB traversal ──► ranked files · functions to read · their code · reasons
          imports · calls · inheritance · identifier usage        + every file that uses the
          co-change · trails · past tasks                           names in the task
                 ▲                                                + files changed together
                 │  merge hook: which files each merged change touched
          the graph learns: Task nodes, TRAIL and COFIX edges
```

- **One call replaces the agent's search loop.** `locate` returns the ranked
  files, the functions to read with line ranges, the best-matching code inline,
  why each file is there, and every file that uses each identifier the task
  names (`(:File)-[:USES]->(:Name)` edges, one read per name).
- **It learns from merges.** Each merged change becomes a `Task` node with
  `TOUCHED` edges. Files changed together get a `COFIX {n}` edge with an exact
  count. Files that kept leading to the answer get `TRAIL` edges, laid only where
  HydraDB's `algo.SPpaths` finds a real structural path between them, and
  decayed when they mislead. Git history is loaded the same way at ingest, so a
  repository has experience on day one. All of it can be inspected and edited
  with Cypher.
- **Hybrid by design.** HydraDB OSS has no text or vector index, and HydraDB's
  own benchmarks say a graph only helps once something has found the entry
  node. Text search picks entry points; every relationship is read from HydraDB
  at query time.

Agents use it through MCP (`locate`, `neighbors`, `impact`, `why`,
`record_outcome`). The live demo shows the same answers in a browser.

---

## Metrics: what we measured and why

| Metric | Why this one |
|---|---|
| **Tokens and turns per task, at equal accuracy** | What a team running agents pays for. A saving that costs accuracy is not a saving, so accuracy is always reported alongside. |
| **All files found** (completeness) | A change that touches three files is not done when one is found. Most real changes in our unseen repository touched several files (64%). Top-1 accuracy would hide this. |
| **Strict top-5 recall** for retrieval without an agent | Every changed file must be in the top 5, which is roughly what an agent will read. |
| **Paired 95% bootstrap intervals** | Every comparison is on the same tasks; intervals resample tasks. Differences inside the interval are reported as "no difference". |

Where we test matters as much as what. Models memorise popular repositories, so
an agent "finding" files in Django mostly recalls them. Our agent tests use
[Reef](https://github.com/Human-Agent-Society/reef) (Apache-2.0), created on
2026-08-31, after the models' training cutoff. The graph is built from Reef as
it stood 200 commits ago; the 101 tasks are the later commits (message in,
changed source files out), replayed in date order.

---

## Result 1: an agent with and without Trailmark

Claude Code, headless, is given a change description and asked to name the
source files that must be modified. Conditions differ only in the tools.

**Claude Haiku, all 101 tasks**

| | Search tools only | With Trailmark | Change (95% interval) |
|---|---|---|---|
| All files found | 68.3% | 71.3% | +3.0 points (7 tasks gained, 4 lost) |
| First file correct | 85.1% | 82.2% | −2.9 points |
| Tokens per task | 171,400 | 105,500 | **−38% (−47% to −29%)** |
| Turns per task | 10.3 | 5.7 | **−45% (−51% to −38%)** |
| Cost per task | $0.0076 | $0.0052 | −31% (−37% to −25%) |
| Wall time per task | 18.2 s | 13.4 s | −26% |

**Claude Sonnet, 39 tasks** (learning condition, with the re-weighting step that was later switched off)

| | Search tools only | With Trailmark | Change (95% interval) |
|---|---|---|---|
| All files found | 82.1% | 79.5% | −2.6 points (2 gained, 3 lost) |
| Tokens per task | 128,100 | 118,200 | −8% (−30% to +19%), not significant |
| Turns per task | 8.5 | 6.2 | −27% (−43% to −9%) |
| Wall time per task | 17.2 s | 14.4 s | −17% |

With the smaller model, the saving is large and accuracy holds. With the larger
model, Trailmark is at parity on accuracy and cost and only saves time. The
token saving is where high-volume agents run.

On Django, Trailmark gives an agent nothing: the models know Django and name the
file in 2–4 turns. A context layer pays off on code the model has not memorised,
which is the normal case for private codebases.

**How `locate` got here** (each version's records are kept):

| Version | Sonnet: all files found (search only: 80–82%) | Sonnet tokens |
|---|---|---|
| v1: ranked files + inline code (`results/agent/`) | 65% | −35% |
| v2: + a list of companion files (`results/agent-v2/`) | 72% | −13% |
| v3: + usage edges + learned co-fix counts (`results/agent-v3/`) | 79.5% | −8% |

v1 made the agent fast but careless: it stopped early on multi-file changes. v2
did not help: only 1 of 91 answer files was surfaced by its list, so we removed
it. v3 closed the gap by giving the agent what its own search would have found
(usage sites) and what merges had taught (files changed together).

---

## Result 2: does the graph do the work?

The organisers ask whether a build would fall apart on a plain store. We
measured it instead of arguing it.

**The same agent tool with the graph switched off.** Same output format; the
answer comes from text indexes only (no traversal, usage edges, history or
learning).

| Haiku, 101 tasks | Search only | Tool, graph off | Tool, graph on |
|---|---|---|---|
| All files found | 68.3% | 69.3% | 71.3% |
| All files found, multi-file changes (65) | 53.8% | 55.4% | 58.5% |
| Tokens per task | 171,400 | 115,300 | 105,500 |
| Turns per task | 10.3 | 6.3 | 5.7 |

The honest reading: **most of the token saving comes from the tool's design**
(one call that returns ranked files with code). On top of that, the graph cuts
turns by a further 10% (95% interval 2% to 18%) and tokens by 8% (not
significant), and finds all files on 3 more multi-file changes. The graph's
distinctive job is learning (Result 3): merged changes become edges, and a plain
store has nowhere to keep them.

**Retrieval ablations** (no agent; strict top 5; Reef, 101 tasks):

| System | Top 1 | Top 3 | Top 5 | Top 10 |
|---|---|---|---|---|
| Keyword search (BM25) | 18.8% | 37.6% | 43.6% | 53.5% |
| All text signals, no graph | 15.8% | 38.6% | 48.5% | 62.4% |
| + static graph in HydraDB | 15.8% | 43.6% | 54.5% | 61.4% |
| **Trailmark** (graph + history + learning) | 18.8% | 44.6% | **60.4%** | **67.3%** |
| Trailmark without usage edges | 17.8% | 44.6% | 57.4% | 67.3% |
| Trailmark without trails | 14.9% | 46.5% | 55.4% | 62.4% |
| Trailmark without code-structure edges | 20.8% | 46.5% | 55.4% | 64.4% |

On Django (848 SWE-bench issues), a plain vector store reaches 49.5% at top 5
and Trailmark 81.1%; but text signals alone already reach 80.8%, so the graph
adds nothing measurable there. The graph earns its place on multi-file changes,
which Reef has and Django mostly does not (79% of Django fixes touch one file).

---

## Result 3: continual learning against a baseline

Two baselines throughout: a **plain store** (text indexes only) and the **frozen
graph** (structure plus git history up to ingest, never taught). The learning
graph starts identical to the frozen one.

**A. Learning from every merge.** How Trailmark is meant to run: a merge hook
reports each merged change. Each task is scored at its own date; the learning
graph has absorbed every commit merged before it.

| | Plain store | Frozen graph | Learning graph | Learning vs frozen (95% interval) |
|---|---|---|---|---|
| Reef: 101 tasks, 129 merges absorbed | 48.5% | 55.4% | **59.4%** | +4.0 points (−1.0 to +8.9) |
| Django: 848 issues 2015–2023, 5,632 merges absorbed | 80.8% | 80.7% | 80.9% | +0.2 points (−0.9 to +1.4) |

**B. A fixed held-out set, re-scored as the graph learns.** Reef's newest 41
tasks are held out; the graph is taught the older 60 one merge at a time.

| Merges taught | 0 | 10 | 20 | 30 | 40 | 50 | 60 |
|---|---|---|---|---|---|---|---|
| Learning graph | 46.3% | 43.9% | 46.3% | 51.2% | 51.2% | 51.2% | **53.7%** |
| Plain store | 46.3% | 46.3% | 46.3% | 46.3% | 46.3% | 46.3% | 46.3% |

After 60 merges: +7.3 points over both baselines (95% interval 0 to +17). Django
with 200 held-out issues and 648 merges taught: no change (82.0% → 82.0%).

**C. The agent, learning as it goes** (Haiku, 101 tasks in date order; after each
task the graph is told which files the real change touched):

| | Search only | Trailmark, frozen | Trailmark, learning |
|---|---|---|---|
| First 50 tasks: all files found | 78.0% | 82.0% | 76.0% |
| Last 51 tasks: all files found | 58.8% | 60.8% | 60.8% |
| Last 51 tasks: tokens per task | 191,600 | 123,200 | 107,500 |
| Last 51 tasks: turns per task | 11.3 | 6.5 | 5.7 |

Learning does not make the agent more complete. It makes it cheaper as it goes:
in the second half, 13% fewer tokens (95% interval −26% to +4%) and 11% fewer
turns (−22% to +1%) than the frozen graph. An earlier run with the re-weighting
step switched on found all files on 66.7% of the last 51 tasks; that did not
replicate once re-weighting was removed, so we do not claim it (both runs are in
`results/agent-v3/`).

**What this adds up to.** The learning effect is real in direction and modest in
size. On new code where changes span several files, the learning graph ranks
the right files higher than the frozen graph in both retrieval designs (+4 and
+7 points), and an agent using it spends fewer tokens late in the stream; every
interval, taken alone, reaches zero. On Django learning does nothing: the graph
starts with 8,368 commits of history, and single-file fixes leave little for
co-change to teach. We report both rather than pick one.

**A bug the curve caught.** An online re-weighting step (nudging how much each
kind of evidence counts after every task) made the held-out curve drop 17
points after 15–30 merges before recovering, with no final gain. It is off by
default and kept as an ablation (`--variant with-policy`, `results/reef.curve.*`).

---

## How HydraDB is used

| What | How |
|---|---|
| Nodes | `File`, `Symbol`, `Name`, `Task`, loaded with `UNWIND … MERGE … SET` batches of 1,000 |
| Structure | `DEFINES`, `IMPORTS`, `CALLS`, `INHERITS`, `REFS` (file-level projection of calls and inheritance, weighted) |
| Usage | `(:File)-[:USES]->(:Name)` for identifiers used in at most 40 files |
| History | `COCHANGED {w}` from git; past commits as `(:Task)-[:TOUCHED]->(:File)` |
| Learning | `TRAIL {w, hits, hops}` laid, reinforced and decayed; `COFIX {n}` counted per merge |
| Retrieval | one-hop reads per seed, per edge type and direction, fanned out concurrently |
| Grounding and "why" | `algo.SPpaths` between two files across all edge types |
| Impact analysis | breadth-first walk over incoming `CALLS` / `INHERITS` |

Reef loads as 36,000 nodes and 110,000 edges in about 20 seconds; Django as
86,000 nodes and 200,000 edges in about 90 seconds. A query makes about 50
graph reads and takes 15–70 ms.

### What we learned about HydraDB OSS (commit `af63440`)

1. **Send each HTTP request in one write.** Python HTTP clients send headers and
   body separately; against a node in WSL from Windows that cost 44 ms per
   request. A raw socket with one `sendall` takes 0.9 ms (`hydra.py`).
2. **The local filesystem store cannot take writes after a restart**
   (`PutMode::Update not yet implemented by LocalFileSystem`). Start from an
   empty store and reload; `scripts/start-hydradb.sh` does.
3. **Edge properties cannot be set inside an `UNWIND` batch.** Load edges plain,
   then `MATCH … SET r.w = $w` one statement per edge.
4. **Variable-length patterns only run away from a fixed source, with the
   relationship unbound.** Reverse walks need repeated one-hop reads.
5. **Whole-graph scans exceed the 30 s deadline** on an unoptimised build; anchor
   every query on a node id, and build with `--release`.
6. `UNWIND … MATCH` (batched reads) and `IN` are not supported.

---

## Run it

```bash
# 1. HydraDB (Linux or WSL): build github.com/hydra-db/hydradb with --release, then
scripts/start-hydradb.sh

# 2. Trailmark
uv venv && uv pip install -e ".[mcp,dense,bench,dev]"
trailmark doctor                                   # checks every query form Trailmark uses
trailmark ingest /path/to/repo --name myrepo       # parse, mine history, load into HydraDB
trailmark locate "describe the bug or change" --name myrepo

# 3. Give it to an agent (Claude Code)
claude mcp add trailmark -e TRAILMARK_WORKSPACE=myrepo -e TRAILMARK_HOME=$PWD \
  -- python -m trailmark.mcp_server

# 4. The demo page
python -m trailmark.web --name myrepo --commits /path/to/repo --since <rev>
```

`deploy/` builds the hosted demo: one container running HydraDB and Trailmark.

### Reproduce every number

```bash
python -m trailmark.bench  --name reef --commits data/repos/reef --since <rev> --no-dense   # retrieval tables
python -m trailmark.curve  --name reef --commits data/repos/reef --since <rev> --holdout 41 --step 5 --no-dense
python -m trailmark.stream --name reef --commits data/repos/reef --since <rev> --no-dense
python -m trailmark.stream --name django-swe --repo django/django --after 2015-08-19
python -m trailmark.agent_eval --name reef --commits data/repos/reef --since <rev> --task change \
  --model haiku --n 101 --conditions baseline,trailmark,trailmark-flat,trailmark-learn
python -m trailmark.summary                                                                   # results/summary.json
```

`<rev>` for Reef is `93bf204bdcc797a91991a6786faec827daad8fc7`; the newest task
is `0d723262abb67fe5f7747a4187f9865e7a358af3`. Per-task records for every table
are in `results/`.

### Known simplifications

- Django's graph is one snapshot (the newest task's base commit), not rebuilt
  per task; every system sees the same snapshot. Its stream excludes the 38
  commits that already contained an issue's fix (SWE-bench dates are issue
  dates, and some fixes were committed first).
- Commit messages reveal location more than bug reports do. That is the same for
  every system and every agent condition.
- The Trailmark agent conditions add three sentences to the system prompt (in
  `agent_eval.py`), the kind a team would put in `CLAUDE.md`.
- The agent evaluation measures finding files, not completing fixes.
- Cost is not compared for the learning agent condition: it ran right after
  another condition with the same prompt prefix, so prompt caching made it look
  cheaper than it is.

---

## Limits

- Python repositories only (the parser uses `ast`).
- Learning helps on new, multi-file code and not on Django; it needs testing on
  more repositories and on real agent traffic.
- For larger models the benefit today is speed, not tokens.
- Persistence needs an S3-compatible store behind HydraDB (finding 2).

Go-to-market: `docs/GO_TO_MARKET.md`. Submission checklist: `docs/SUBMISSION.md`.

## Licence

MIT. Trailmark talks to HydraDB over its query API and contains none of its
code. HydraDB is under the Elastic License 2.0; the hosted demo bundles an
unmodified build with that licence.
