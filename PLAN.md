# Trailmark — a code graph that learns from every agent run

HydraDB Hackathon, Nov 14–16 2026 (48 h, solo, Python). Built on HydraDB OSS.

> This is the plan as written at the start. What was actually built and
> measured, including what did not work, is in `README.md`. Main departures:
> an agent-level evaluation on an unseen repository became the centrepiece,
> git history is loaded as experience at ingest, and the HTTP client was
> rewritten after finding a 44 ms per-request stall.

## The one-sentence pitch

Coding agents burn most of their tokens finding the right files. Trailmark is a
context layer that finds them by walking a graph of the codebase in HydraDB, and
every finished task wears the path in deeper, so the next lookup is cheaper and
more accurate. No LLM calls are needed to retrieve.

## Why this can win (mapped to the rules)

| Rule / criterion | How Trailmark answers it |
|---|---|
| Track 1: "a graph that improves itself, sharper every run" | Outcome feedback reinforces edges and adds experience nodes; we plot recall per run |
| Track 2: "context layer for agents, traversing structure instead of burning tokens" | MCP server whose tools are graph traversals; we report context tokens per task |
| "It has to earn its place over a vector store" | Same tasks, same seeds, with and without the graph; ablation table |
| Engineering | Real HydraDB usage: batched ingest, bounded multi-hop Cypher, `algo.SPpaths` weighted paths |
| Go-to-market | Buyer, price and savings computed from the measured token numbers |

## What the research says (and how we use it)

- HydraDB's own benchmark page says a graph only helps after vector search finds
  the entry node. So we are hybrid by design: lexical + dense seeds, graph expansion.
- HydraDB OSS is a Cypher graph store with no vector index, no `IN`, no
  expressions in `RETURN`, directed single-type edges, bounded paths only, and a
  1024-row batch cap. Seeds and scoring therefore live in Python; structure,
  weights and experience live in HydraDB.
- Prior art (LocAgent, RepoGraph, CoSIL, GraphLocator) shows code graphs help
  issue localisation, but they use an LLM to walk the graph and the graph is
  static. A 2025 "repository memory" paper got +4.9 points Acc@5 by adding
  history. Nobody we found reinforces the graph's own edges from task outcomes.
  That is the innovation slot.
- A 2026 survey warns that continuous edge updates cause drift. We answer with
  weight decay, capped weights and a held-out check.

## Verified against the live node (Nov 14, commit af63440)

Works:
- `UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Label, n.p = row.p` (node batch)
- `UNWIND $rows AS row CREATE (a {id: row.src})-[:T]->(b {id: row.dst})` (edge batch)
- `MATCH (a {id: $s})-[r:T]->(b {id: $d}) SET r.w = $w, r.hits = $h` (edge weights, with parameters)
- `MERGE (a {id: 1})-[:T {w: 1.0}]->(b {id: 2})` (single edge with properties)
- Explicit two-hop with both edge properties returned; incoming direction; `count(*)` grouping
- `[:T*1..2]` when the relationship is NOT bound to a variable
- `algo.SPpaths` with `weightProp`: returns nodes, edges, properties and summed `pathWeight`
- `STARTS WITH`, `DETACH DELETE` by label

Does not work (design around it):
- Edge properties inside an UNWIND batch -> load edges plain, then set weights
  one statement each, or encode coarse weight in the edge type (`COCHANGED_HI`)
- `UNWIND ... MATCH` batched reads -> one query per seed (cap seeds at ~20)
- Binding a variable-length relationship (`[r:T*1..2]`)

Run the server: `wsl -e bash -lc "~/start-hydradb.sh"`; health at
`http://127.0.0.1:8443/healthz`; token `local-development-token-32-bytes`.

## The innovation: desire paths

Three learning mechanisms, all stored as graph structure in HydraDB:

1. **Experience nodes.** Each finished task becomes a `Task` node with
   `TOUCHED` edges to the files that were actually changed. A new query finds
   similar past tasks and walks `Task -TOUCHED-> File`.
2. **Trail edges.** When a task's seed files were wrong but the right file was
   N hops away, we write/reinforce a direct `TRAIL` edge from seed to answer.
   Weights grow with success, decay with time, and are capped.
3. **Learned traversal policy.** A small online model learns how much each edge
   type (`IMPORTS`, `CALLS`, `COCHANGED`, `TRAIL`, `TOUCHED`) should count for
   this repository, updated after every task.

## Graph schema (HydraDB)

Nodes (integer `id`, one label each):
- `File {path, lang, loc}`
- `Symbol {name, qualname, kind, path, line}`
- `Task {key, text, ts}`

Edges (directed, single type):
- `File -DEFINES-> Symbol`
- `File -IMPORTS-> File`
- `Symbol -CALLS-> Symbol`
- `File -COCHANGED-> File {w}` (mined from git history, both directions stored)
- `Task -TOUCHED-> File`
- `File -TRAIL-> File {w, hits, last}`

## Retrieval pipeline

1. Seeds: BM25 over file paths + symbols + docstrings, fused with a small local
   embedding model. No API key needed.
2. Experience: nearest past `Task` nodes, then `TOUCHED` in HydraDB.
3. Expansion: bounded 1–2 hop Cypher over each edge type from the seeds.
4. Scoring: seed score + sum over paths of (edge-type weight x edge weight x hop decay).
5. Explanation: `algo.SPpaths` from seed to result, returned as the "why".
6. Output: ranked files and symbols with a token budget.

## Benchmark (the centrepiece of the demo)

- Data: SWE-bench instances for one large repo (Django first, then a second
  repo), ordered by creation date. Gold = files changed by the real fix.
- Protocol: strictly online. Predict, score, then learn from that task. Nothing
  from the future is visible. Co-change edges are mined only from commits
  before the first evaluated task.
- Systems compared: BM25, dense vectors, hybrid (BM25+dense), hybrid + static
  graph, hybrid + graph + learning (Trailmark).
- Metrics: file recall@1/5/10, MRR, context tokens needed to cover all gold
  files, latency.
- Charts: recall over task number (the "sharper every run" curve), ablation
  bars, tokens per task.
- Known simplification to state openly: the code graph is built from one
  snapshot of the repo, not rebuilt at each task's commit.

## Phases

### Phase 0 — Foundations (hours 0–4)
- 0.1 Build and run HydraDB OSS in WSL (Rust build, auth token, env vars).
- 0.2 Python project (`uv`), HydraDB client over HTTP with batching and typed-cell decoding.
- 0.3 Smoke test: write, read, multi-hop, `algo.SPpaths`, edge-property `SET`.
- Exit: `trailmark doctor` passes against a live node.

### Phase 1 — Ingest (hours 4–10)
- 1.1 Python AST parser: files, symbols, imports resolved to files, call edges.
- 1.2 Git miner: co-change edges with a date cutoff.
- 1.3 Batched loader into HydraDB (1000-row UNWIND chunks, idempotent).
- Exit: Django fully loaded; counts verified by Cypher.

### Phase 2 — Retrieval (hours 10–18)
- 2.1 BM25 and dense seed indexes.
- 2.2 Graph expansion queries and scorer.
- 2.3 Path explanations.
- Exit: `trailmark locate "<issue text>"` returns ranked files with reasons.

### Phase 3 — Learning loop (hours 18–26)
- 3.1 `record_outcome`: Task nodes, TOUCHED edges.
- 3.2 Trail reinforcement, decay, caps.
- 3.3 Online edge-type weights.
- Exit: repeated tasks measurably improve; unit tests for update rules.

### Phase 4 — Benchmark (hours 26–34)
- 4.1 SWE-bench loader and chronological runner.
- 4.2 Baselines and ablations.
- 4.3 Results JSON and charts.
- Exit: one command reproduces every number in the README.

### Phase 5 — Product surface (hours 34–40)
- 5.1 MCP server: `locate`, `neighbors`, `impact`, `why`, `record_outcome`.
- 5.2 CLI polish and a Claude Code hook that records outcomes automatically.
- 5.3 Results page (HTML) with the charts.

### Phase 6 — Submission (hours 40–48)
- 6.1 README: setup, how HydraDB is used, results, limitations.
- 6.2 Go-to-market one-pager with pricing from measured token savings.
- 6.3 Demo video script (3 minutes) and recording.
- 6.4 Licence, public repo, submit with buffer.

## Cut lines (if time runs short)

1. Drop the second repo.
2. Drop the Claude Code hook; keep the MCP server.
3. Drop learned edge-type weights; keep experience nodes and trails.
Never cut: the online benchmark and the learning curve.

## Risks

- HydraDB fails to build in WSL -> try the `ghcr.io/hydra-db/hydradb:0.3.0`
  image or ask in the Discord for hosted access. The client is behind one
  interface so the rest of the work is not blocked meanwhile.
- Query budgets truncate large traversals -> keep hops at 2 and fan-out capped.
- Learning shows no gain -> report honestly, lean on the static-graph ablation.
