# Trailmark: go-to-market

## The problem, in the buyer's terms

Teams running coding agents at volume pay for tokens, and much of that spend
happens before any code is written: the agent searches and reads files to work
out where a change belongs. In our evaluation, Claude Haiku spent about 171,000
tokens and 10 turns per task just to name the files, on a codebase it had not
seen before.

## What we sell

A context layer the agent calls first. One `locate` call returns the ranked
files, the functions to read with their code, why each file is there, every
file that uses the names in the task, and files that past merges changed
together.

Measured on 101 real changes with Claude Haiku: **38% fewer tokens, 45% fewer
turns, 31% lower cost, at the same accuracy.**

## What the buyer gets from the graph, specifically

We measured this rather than assuming it, and the pitch follows the data:

- **The call design carries most of the saving.** The same tool with the graph
  switched off still cut tokens by a third. Anyone can copy that; it is the
  entry point, not the moat.
- **The graph adds completeness and speed.** On changes that span several
  files, the graph version found every file more often (58.5% against 55.4%)
  and used 10% fewer turns than the graph-off version.
- **The moat is what the graph learns.** Every merged change becomes edges:
  which files change together, how often, and which paths led to the answer. On
  a repository the models had never seen, learning from merges added 4–7 points
  over the frozen graph across three experiment designs. That knowledge is
  specific to the customer's codebase, accumulates over time, and lives in a
  graph they own and can inspect. On Django it added nothing; the pitch says so.

## Who buys first

| Segment | Why they feel it | How they buy |
|---|---|---|
| Teams running background agents on tickets (triage, small fixes, dependency bumps) | Thousands of runs a month on smaller models; orientation cost dominates | Engineering-platform budget, usage-based |
| Agent product companies (coding assistants, review bots) | Token cost is their gross margin | Embed as a component; volume licence |
| Large private codebases | Models have no prior on the code; it changes daily, so a learning index stays current | Self-hosted with HydraDB on their own object storage |

Not the first buyer: a developer using a top-tier model interactively on a
well-known open-source project. Our data shows no benefit on Django (the models
know it), and with Claude Sonnet Trailmark is at parity on accuracy and cost and
only saves time (27% fewer turns).

## Pricing

Anchor on measured savings, and take a minority of them.

- **Team:** $0.001 per `locate` call, first 5,000 a month free. That is about
  40% of the measured $0.0024 saving per task on a Haiku-class model, so the
  customer keeps the larger share from the first call.
- **Self-hosted:** annual licence per repository, with HydraDB on the
  customer's object storage.

Worked example: 50,000 agent tasks a month on a Haiku-class model costs about
$380 for the locate phase at our measured $0.0076 per task; Trailmark removes
about $120, and we charge about $45. The absolute figure is small on a small
model. The business case rests on volume and on the 45% turn reduction, which
is also latency and rate-limit headroom, and it grows with model price.

## Who wants it: how we will show demand

The organisers ask to see who wants it. What we will collect, and only report
if real:

1. Replies and DMs to the launch posts (tagged @hydra_db on X, HydraDB on
   LinkedIn) from people running agents on their own code.
2. GitHub stars, forks and issues on `imortis/trailmark`.
3. Anyone who runs `trailmark ingest` on their own repository; the benchmark
   harness lets them measure the saving on their own code before paying.
4. One design-partner trial: a team running background agents, comparing
   tokens per task for two weeks with and without Trailmark.

## Why HydraDB, and why not a vector store

- A vector store of code was the weakest system we measured: 49.5% at top 5 on
  Django against 81.1% for Trailmark.
- What a vector store cannot hold is what made the difference on multi-file
  changes: which files use a name, which change together, and which paths past
  tasks took. Those are edges, and they are updated with every merge.
- The learned state is inspectable. A team can query why a file was suggested
  and delete a trail that is wrong. Embedding drift cannot be audited that way.
- For HydraDB: Trailmark is a reference workload (batched ingest, weighted
  traversal, `algo.SPpaths`, continuous small writes) with a public benchmark
  and six concrete findings about the OSS engine.

## Distribution

1. Open-source MCP server; one command to add to Claude Code or any MCP client.
2. `trailmark ingest` gives a working graph of a repository in under a minute,
   with git history as day-one experience.
3. The benchmark harness ships in the repository, so a prospect can run the
   with-and-without comparison on their own code.

## What has to be true next

| Risk | Test |
|---|---|
| Savings hold on full fixes, not only on finding files | Run fix-and-test tasks with and without Trailmark on an unseen repository |
| Larger models save time but not tokens | Trim what `locate` returns for strong models; target a token saving at parity on Sonnet over 100 tasks |
| Learning gains are small and repository-dependent | Measure on several private-style repositories and on real agent traffic over weeks |
| Python only | Add tree-sitter parsers for TypeScript and Go |
