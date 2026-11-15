# Submission

Deadline: **9 AM IST, Nov 16, 2026**. The three required parts:

| Requirement | Link |
|---|---|
| Live app | https://imortis.github.io/trailmark/ |
| Open-source GitHub repo | https://github.com/imortis/trailmark |
| Video walkthrough (6 min) | https://imortis.github.io/trailmark/video.html |

Built on the open-source engine: HydraDB OSS built from source at
`af6344037bffb2a354bf32c2b312ba4921a1583f`, run locally (WSL) and packaged in
the container in `deploy/`.

---

## Ready-to-paste answers

**Project name**
Trailmark

**One-line description**
A context layer for coding agents on HydraDB: walk a code graph instead of burning tokens, and learn from every merge.

**Track**
Track 2, the context layer for agents (primary). It also addresses Track 1: the graph learns from every merged change.

**What it does (short)**
Coding agents spend most of their tokens finding where a change belongs. Trailmark loads a codebase into HydraDB (files, functions and identifiers as nodes; imports, calls, inheritance, identifier usage and co-change as edges) and answers "which files does this change need?" with one call that returns ranked files, the functions to read, their code and the reasons. Every merged change is written back as graph structure (task nodes, co-fix counts, trails grounded by HydraDB's shortest-path procedure), so the graph keeps learning.

**Results (short)**
On a repository created after the model's training cutoff (101 real changes), Claude Haiku with Trailmark found the files with 38% fewer tokens and 45% fewer turns at the same accuracy. A control with the graph switched off shows the graph's own contribution: 10% fewer turns and more complete answers on multi-file changes. Learning from merges improved held-out ranking from 46% to 54% against a plain store and a frozen graph on that repository, and made no difference on Django; both are reported.

**How HydraDB is used**
HydraDB OSS (built from source) is the only store for structure, history and learning: batched UNWIND/MERGE ingest of about 36,000 nodes and 110,000 edges, one-hop traversals per seed across eight edge types, `algo.SPpaths` to ground every learned trail and explain connections, and continuous small writes (MERGE/SET of TRAIL and COFIX edges) as merges arrive. Six findings about the OSS engine are written up in the README.

**Tech stack**
Python 3.11+, HydraDB OSS (Rust, OpenCypher over HTTP), MCP server for Claude Code and other agents, standard-library HTTP transport, NumPy, BM25 and optional bge-small embeddings, GitHub Pages for the demo.

**Team**
Solo: imortis.

**Go-to-market**
Buyers: teams running background coding agents at volume and agent products whose margin is token cost. Pricing: $0.001 per locate call, below the measured $0.0024 saving per task. Full plan: `docs/GO_TO_MARKET.md`. Launch posts tagging @hydra_db: `docs/LAUNCH_POSTS.md`.

---

## Notes for the organisers, if a field asks

- The always-on demo serves answers precomputed by the live system with HydraDB running. Hugging Face now requires a paid plan for Docker apps, so the fully live version is the tested container in `deploy/` (one command on any Docker host), or run locally with `python -m trailmark.web`.
- The video is narrated with a neural text-to-speech voice (Indian English).
