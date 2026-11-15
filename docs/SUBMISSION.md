# Submission checklist

Deadline: **9 AM IST, Nov 16, 2026**.

| Requirement (from the organisers' FAQ) | Where it is | Status |
|---|---|---|
| Built on the open-source engine, run yourself | HydraDB OSS built from source at `af63440`, run locally and in the hosted container (`deploy/space/`) | done |
| Live link | Hugging Face Space (see README header) | needs HF login, then `deploy/build_space.sh` + upload |
| Open-source GitHub repo | https://github.com/imortis/trailmark (MIT) | push |
| Loom walkthrough | script in `docs/LOOM_SCRIPT.md` | record |
| Go-to-market: posts tagging HydraDB | drafts in `docs/LAUNCH_POSTS.md`; X handle `@hydra_db` | post |

## How the build answers the technical rubric

| Judges look for | Our answer | Evidence |
|---|---|---|
| **Right metrics** | Tokens and turns an agent spends to locate a change, *at equal accuracy*; completeness (all files a change needs), not top-1 | README "Metrics", `results/agent-v3/` |
| **Continual learning + a baseline** | Fixed held-out test sets re-scored after every batch of merged changes; baselines are a plain store and the untaught graph | README "Continual learning", `results/*.curve.*.json` |
| **Impact of HydraDB and the graph** | Same agent tool with the graph switched off (plain-store control); retrieval ablations removing one edge type at a time | README "Does the graph do the work?", `results/agent-v3/`, `results/reef.*.json` |

## Before recording the Loom

1. Close memory-heavy apps (browser tabs, VS Code, Spotify): HydraDB gets stopped when memory runs out.
2. Open the live link once so the Space is awake (startup takes about a minute).
3. Have the README results section and the repository open in tabs.
