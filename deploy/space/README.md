---
title: Trailmark
emoji: 🧭
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: A context layer for coding agents, built on HydraDB
---

# Trailmark: live demo

Describe a change to the [Reef](https://github.com/Human-Agent-Society/reef)
codebase and Trailmark walks a code graph in HydraDB to say which files it
belongs in. Three stores answer side by side: text indexes alone, the graph as
ingested, and the graph after it has learned from merged changes.

The container runs the open-source HydraDB engine and Trailmark together. On
start it loads Reef's code graph and git history into HydraDB and teaches the
learning graph the first 60 later commits; the rest are offered as unseen
changes to try. Startup takes about a minute.

Source, benchmarks and per-task records: https://github.com/imortis/trailmark

HydraDB (`hydradb/graph-node`) is distributed under the Elastic License 2.0;
see `hydradb/LICENSE`. Trailmark is MIT.
