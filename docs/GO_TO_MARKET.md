# Trailmark: go-to-market

## The problem, in the buyer's terms

Teams running coding agents at volume pay for tokens, and a large share of
those tokens is spent before any code is written: the agent searches and reads
files to work out where a change belongs. In our evaluation an agent spent
about 171,000 tokens and 10 turns per task just to name the files, on a
codebase it had not seen before.

## What we sell

A context layer the agent calls first. One `locate` call returns the ranked
files, the functions to read, their code, and the reasons. Measured on 101
tasks with Claude Haiku: **38% fewer tokens, 45% fewer turns, 31% lower cost,
same accuracy.** With the graph learning from each merge the figures rise to
43% fewer tokens and 50% fewer turns, and in the second half of the task
stream the learning graph found all files on 6 points more tasks.

It runs on HydraDB, so the customer's code graph, history and learned trails
sit in one store they control.

## Who buys first

| Segment | Why they feel it | How they buy |
|---|---|---|
| Teams running background agents on tickets (triage, small fixes, dependency bumps) | Thousands of runs a month on smaller models; orientation cost dominates | Engineering-platform budget, usage-based |
| Agent product companies (coding assistants, review bots) | Token cost is their gross margin | Embed as a component; volume licence |
| Large private monorepos | Models have no prior on the code; search is slow and noisy | Self-hosted with HydraDB on their own object storage |

Not the first buyer: a developer using a top-tier model interactively on a
well-known open-source project. Our own data shows no benefit there (the model
already knows Django), and with Claude Sonnet Trailmark is at parity on
accuracy and cost and only saves time (27% fewer turns). We say so in the README.

## Pricing

Anchor on measured savings, and take a minority of it.

- A team running 50,000 agent tasks a month on a Haiku-class model spends about
  $380 on the locate phase alone at our measured $0.0076 per task; Trailmark
  removes about $120 of that. On larger models, or when the same saving in
  turns applies to the whole task, the figure is 10–20x higher.
- **Team:** $0.001 per `locate` call, first 5,000 a month free. That is about 40% of
  the measured $0.0024 saving per call on a Haiku-class model, so the customer
  keeps the larger share from the first call.
- **Self-hosted:** annual licence per repository, with HydraDB on the
  customer's object storage.

The honest caveat for the pitch: the per-task dollar saving on a small model is
small in absolute terms. The business case rests on volume and on the turn
reduction (45–50%), which is also latency and rate-limit headroom.

## Why HydraDB, and why not a vector store

- A vector store of code chunks was the weakest system we measured: 27% top-1
  on Django against 50% for Trailmark.
- What a vector store cannot hold is what made the difference on multi-file
  changes: which files use a name, which files change together, and which
  paths past tasks actually took. Those are edges.
- The learned state is inspectable. A team can query why a file was suggested
  and delete a trail that is wrong. Embedding drift cannot be audited that way.
- For HydraDB: Trailmark is a reference workload (batched ingest, weighted
  traversal, `algo.SPpaths`, continuous small writes) with a public benchmark
  and a list of six concrete OSS findings.

## Distribution

1. Open-source MCP server; one command to add to Claude Code or any MCP client.
2. `trailmark ingest` on a repository gives a working graph in under a minute,
   with git history as day-one experience.
3. The benchmark harness is in the repository, so a prospect can run the
   agent-with-and-without comparison on their own code before paying.

## What has to be true next

| Risk | Test |
|---|---|
| Savings hold on full fix tasks, not only on finding files | Run SWE-bench-style resolution with and without Trailmark on an unseen repository |
| Larger models save time but not tokens | Trim what `locate` returns for strong models; target a token saving at parity on Sonnet over 100 tasks |
| Python only | Add tree-sitter parsers for TypeScript and Go |
| Learning gains are small on single-file repositories | Measure the learning curve over thousands of real agent runs, not replayed history |
