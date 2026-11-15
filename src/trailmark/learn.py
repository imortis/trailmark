"""The learning loop: every finished task leaves the graph sharper.

After a task, the caller reports which files were actually changed. Four
things are then written, and all the durable state is graph structure in
HydraDB:

  experience  a ``Task`` node with ``TOUCHED`` edges to the changed files.
              Later queries find similar past tasks and walk TOUCHED. Git
              history loaded at ingest is the same structure, so a repository
              starts with experience on day one.
  trails      when a changed file was not ranked first, the strongest seed
              files get a ``TRAIL`` edge to it. A trail is only laid if
              HydraDB finds a real structural path between the two files
              (algo.SPpaths), and shorter paths earn more weight. Trails that
              lead to wrong answers decay.
  policy      (optional, off by default) the weight of each kind of evidence
              is nudged by an online pairwise ranking update.
  heat        how often each file has been changed, a prior on where fixes land.

Trail and weight updates are bounded (caps, decay, non-negative weights) so a
run of unusual tasks cannot drag the graph far.
"""

from __future__ import annotations

import json
import math
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .hydra import Hydra
from .ingest import HISTORY_KIND, SLOT_SIZE, edge_batch
from .retrieve import FEATURES, Result, Retriever
from .text import BM25, tokenize

TASK_ID_BASE = 10_000_000
NODE_TASK = "UNWIND $rows AS row MERGE (n {id: row.id}) SET n:Task, n.key = row.key, n.ts = row.ts"
WEIGHT_MAX = 5.0
SIMILAR_POOL = 40  # past tasks shortlisted by raw score before similarity is normalised


@dataclass
class LearnConfig:
    run: str = ""  # isolates one run's learned edges and task ids from another's
    use_history: bool = True      # git history loaded at ingest counts as experience
    use_experience: bool = True
    use_trails: bool = True
    # Online re-weighting of evidence. Off by default: on a fixed held-out set it
    # made the learning curve dip nearly 20 points before recovering, with no final gain
    # (results/reef.curve.*.json). Kept as an ablation.
    use_policy: bool = False
    use_heat: bool = True
    ground_trails: bool = True    # require a structural path before laying a trail
    link_companions: bool = True  # files changed by one task get trails to each other
    warmup: int = 0               # replay this many of the newest history commits to pre-train the policy
    similar_tasks: int = 8
    min_similarity: float = 0.25
    trail_seeds: int = 3
    trail_lr: float = 0.5
    trail_cap: float = 3.0
    trail_decay: float = 0.85
    trail_max_hops: int = 3
    policy_lr: float = 0.05
    negatives: int = 10


def pairwise_update(weights: dict[str, float], gold: dict[str, float], negative: dict[str, float], lr: float) -> float:
    """One RankNet-style step so ``gold`` outscores ``negative``. Returns the loss before the step.

    Weights stay in [0, WEIGHT_MAX]: every feature is evidence *for* a file,
    so a negative weight would be uninterpretable.
    """
    margin = sum(weights.get(k, 0.0) * (gold.get(k, 0.0) - negative.get(k, 0.0)) for k in FEATURES)
    margin = max(-30.0, min(30.0, margin))
    grad = 1.0 / (1.0 + math.exp(margin))  # d(loss)/d(margin), negated
    for k in FEATURES:
        diff = gold.get(k, 0.0) - negative.get(k, 0.0)
        if diff:
            weights[k] = max(0.0, min(WEIGHT_MAX, weights.get(k, 0.0) + lr * grad * diff))
    return math.log1p(math.exp(-margin))


@dataclass
class Outcome:
    """What one recorded task changed in the graph."""
    task_id: int
    trails_laid: int = 0
    trails_reinforced: int = 0
    trails_decayed: int = 0
    trails_skipped_ungrounded: int = 0
    policy_pairs: int = 0
    notes: list[str] = field(default_factory=list)


class Learner:
    def __init__(self, retriever: Retriever, db: Hydra, config: LearnConfig | None = None):
        self.r = retriever
        self.ws = retriever.ws
        self.db = db
        self.cfg = config or LearnConfig()
        suffix = f"_{self.cfg.run}" if self.cfg.run else ""
        self.touched_kind = f"TOUCHED_RUN{suffix}"  # distinct from the history edge type
        self.trail_kind = f"TRAIL{suffix}"
        self.cofix_kind = f"COFIX{suffix}"  # (:File)-[:COFIX {n}]->(:File): changed by the same task n times
        retriever.config.trail_kind = self.trail_kind
        if not self.cfg.use_trails:
            retriever.config.kinds = tuple(k for k in retriever.config.kinds if k != "TRAIL")
        # Task ids sit inside the repository's slot, above files and symbols, one million per run.
        run_slot = zlib.crc32(self.cfg.run.encode()) % 79 if self.cfg.run else 0
        self._id_base = int(self.ws.meta.get("slot", 0)) * SLOT_SIZE + TASK_ID_BASE + run_slot * 1_000_000
        # Local mirrors of what is in HydraDB. The database is the store; these
        # avoid a read before every write and feed the task text index.
        self.task_ids: list[int] = []
        self.task_kind: list[str] = []       # edge type that links each task to its files
        self.task_terms: list[list[str]] = []  # distinct terms, for similarity normalisation
        self.own_tasks: list[dict] = []      # tasks recorded by this learner (for save/load)
        self.task_index = BM25()
        self.heat: dict[int, int] = {}
        self.trails: dict[tuple[int, int], tuple[float, int]] = {}
        self.cofix: dict[tuple[int, int], int] = {}
        self._recorded = 0
        self.history_loaded = 0
        if self.cfg.use_history:
            self._load_history()

    # -- experience from git history -------------------------------------------
    def _register(self, task_id: int, kind: str, text: str, files: list[int]) -> None:
        tokens = tokenize(text)
        self.task_ids.append(task_id)
        self.task_kind.append(kind)
        self.task_terms.append(list(set(tokens)))
        self.task_index.add(tokens)
        for f in files:
            self.heat[f] = self.heat.get(f, 0) + 1

    def _load_history(self) -> None:
        path = self.ws.dir / "history.json"
        if not path.exists():
            return
        history = json.loads(path.read_text(encoding="utf-8"))
        split = max(0, len(history) - self.cfg.warmup)
        for h in history[:split]:
            self._register(h["id"], HISTORY_KIND, h["text"], h["files"])
        self.history_loaded = split
        # Warm-up: the newest commits are held back, then replayed one at a time
        # exactly like live tasks (rank first, then learn), so the policy starts
        # tuned to this repository without ever seeing an answer early.
        for h in history[split:]:
            res = self.locate(h["text"])
            self._learn(res, h["files"], Outcome(task_id=h["id"]))
            self._register(h["id"], HISTORY_KIND, h["text"], h["files"])
            self.history_loaded += 1

    # -- query time ----------------------------------------------------------
    def _experience(self, query: str) -> dict[int, dict[str, float]]:
        """Similar past tasks, then ``Task -TOUCHED-> File`` in HydraDB."""
        if not self.cfg.use_experience or not self.task_ids:
            return {}
        scores = self.task_index.scores(tokenize(query))
        order = scores.argsort()[::-1][:SIMILAR_POOL]
        picked: list[tuple[int, float]] = []
        for i in order:
            if scores[i] <= 0:
                break
            # Similarity = how much of the past task the new one covers. Past
            # tasks are short (a commit subject), new ones long, so the past
            # task's own score ceiling is the right yardstick.
            bound = self.task_index.max_score(self.task_terms[i])
            sim = min(1.0, float(scores[i]) / bound) if bound > 0 else 0.0
            if sim >= self.cfg.min_similarity:
                picked.append((int(i), sim))
        picked.sort(key=lambda p: -p[1])
        picked = picked[: self.cfg.similar_tasks]
        if not picked:
            return {}
        out: dict[int, dict[str, float]] = {}
        by_kind: dict[str, list[tuple[int, float]]] = {}
        for i, sim in picked:
            by_kind.setdefault(self.task_kind[i], []).append((self.task_ids[i], sim))
        for kind, items in by_kind.items():
            rows = self.db.query_many(
                f"MATCH (t {{id: $id}})-[:{kind}]->(f) RETURN f.id AS id", [{"id": t} for t, _ in items]
            )
            for (_, sim), files in zip(items, rows):
                for row in files:
                    f = out.setdefault(row["id"], {"experience": 0.0})
                    f["experience"] = min(1.0, f["experience"] + sim / math.sqrt(len(files)))
        return out

    def _heat(self, file_id: int) -> dict[str, float]:
        n = self.heat.get(file_id, 0)
        if not n or not self.cfg.use_heat:
            return {}
        return {"hot": math.log1p(n) / math.log1p(self._heat_max())}

    def _heat_max(self) -> int:
        if getattr(self, "_heat_n", -1) != len(self.task_ids):
            self._heat_n, self._heat_hi = len(self.task_ids), max(self.heat.values(), default=1)
        return self._heat_hi

    def locate(self, query: str) -> Result:
        return self.r.locate(query, extra=self._experience(query), annotate=self._heat)

    # -- after the task ------------------------------------------------------
    def record(self, query: str, result: Result, changed: list[int], key: str = "", ts: int | None = None) -> Outcome:
        """Report the files a task actually changed. Call once, after ``locate``."""
        changed = [f for f in dict.fromkeys(changed) if f in self.ws.file_by_id]
        self._recorded += 1
        task_id = self._id_base + self._recorded
        out = Outcome(task_id=task_id)
        if not changed:
            out.notes.append("no changed file is in the graph; nothing recorded")
            return out
        # 1. experience
        self.db.batch(NODE_TASK, [{"id": task_id, "key": key or f"task-{task_id}", "ts": int(ts or time.time())}])
        self.db.batch(edge_batch(self.touched_kind), [{"src": task_id, "dst": f} for f in changed])
        self._learn(result, changed, out)
        self._register(task_id, self.touched_kind, query, changed)
        self.own_tasks.append({"id": task_id, "key": key, "text": query, "files": changed})
        return out

    def _learn(self, result: Result, changed: list[int], out: Outcome) -> None:
        """Trail and policy updates from one outcome."""
        rank = {h.file_id: i for i, h in enumerate(result.hits, 1)}
        gold = set(changed)
        # 2. trails
        if self.cfg.use_trails:
            self._decay_misleading_trails(result, gold, out)
            for g in changed:
                if rank.get(g) == 1:
                    continue  # already found first; no shortcut needed
                seeds = [(s, strength) for s, strength in result.seeds if s != g][: self.cfg.trail_seeds]
                for s, strength in seeds:
                    self._lay_trail(s, g, strength, out)
            # Files changed by the same task are linked to each other: finding
            # one should surface its companions next time. Co-change is direct
            # evidence, so these trails need no structural grounding.
            if self.cfg.link_companions and 1 < len(changed) <= 6:
                for a in changed:
                    for b in changed:
                        if a != b:
                            self._lay_trail(a, b, 1.0, out, grounded=True)
                            self._count_cofix(a, b)
        # 3. policy
        if self.cfg.use_policy:
            weights = self.r.config.weights
            negatives = [h for h in result.hits if h.file_id not in gold][: self.cfg.negatives]
            for g in changed:
                feats = result.candidates.get(g)
                if feats is None:
                    continue  # never became a candidate: a recall miss, not a ranking error
                for neg in negatives:
                    pairwise_update(weights, feats, neg.features, self.cfg.policy_lr)
                    out.policy_pairs += 1

    def _lay_trail(self, src: int, dst: int, strength: float, out: Outcome, grounded: bool = False) -> None:
        cfg = self.cfg
        existing = self.trails.get((src, dst))
        hops = 1
        if existing is None and cfg.ground_trails and not grounded:
            found = self.r.path_len(src, dst, cfg.trail_max_hops)
            if found is None:
                out.trails_skipped_ungrounded += 1
                return
            hops = max(1, found)
        gain = cfg.trail_lr * strength / hops
        if existing is None:
            w = min(cfg.trail_cap, gain)
            self.db.run(
                f"MERGE (a {{id: {int(src)}}})-[:{self.trail_kind} {{w: {w:.4f}, hits: 1, hops: {hops}}}]->(b {{id: {int(dst)}}})"
            )
            self.trails[(src, dst)] = (w, 1)
            out.trails_laid += 1
        else:
            w = min(cfg.trail_cap, existing[0] + gain)
            self._set_trail(src, dst, w, existing[1] + 1)
            out.trails_reinforced += 1

    def _count_cofix(self, a: int, b: int) -> None:
        """Keep an exact count of how often two files were changed by the same task.

        Trails carry a blended weight for ranking; this is the plain fact an
        agent can be shown ("changed together in 3 past tasks").
        """
        n = self.cofix.get((a, b), 0) + 1
        if n == 1:
            self.db.run(f"MERGE (a {{id: {int(a)}}})-[:{self.cofix_kind} {{n: 1}}]->(b {{id: {int(b)}}})")
        else:
            self.db.run(f"MATCH (a {{id: $a}})-[r:{self.cofix_kind}]->(b {{id: $b}}) SET r.n = $n", a=a, b=b, n=n)
        self.cofix[(a, b)] = n

    def _set_trail(self, src: int, dst: int, w: float, hits: int) -> None:
        self.db.run(
            f"MATCH (a {{id: $s}})-[r:{self.trail_kind}]->(b {{id: $d}}) SET r.w = $w, r.hits = $h",
            s=src, d=dst, w=round(w, 4), h=hits,
        )
        self.trails[(src, dst)] = (w, hits)

    def _decay_misleading_trails(self, result: Result, gold: set[int], out: Outcome) -> None:
        """A trail that pushed a wrong file into the top results loses weight."""
        wrong_top = {h.file_id for h in result.hits[:5] if h.file_id not in gold}
        for seed, _ in result.seeds:
            for row in self.r.last_trails.get(seed, []):
                key = (seed, row["id"])
                if row["id"] in wrong_top and key in self.trails:
                    w, hits = self.trails[key]
                    self._set_trail(seed, row["id"], w * self.cfg.trail_decay, hits)
                    out.trails_decayed += 1

    # -- persistence of the local mirrors --------------------------------------
    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(
                {
                    "config": asdict(self.cfg),
                    "weights": self.r.config.weights,
                    "recorded": self._recorded,
                    "own_tasks": self.own_tasks,
                    "trails": [[a, b, w, h] for (a, b), (w, h) in self.trails.items()],
                    "cofix": [[a, b, n] for (a, b), n in self.cofix.items()],
                },
                indent=1,
            ),
            encoding="utf-8",
        )

    def load(self, path: str | Path) -> bool:
        """Restore what ``save`` wrote. Returns False if there is nothing to load."""
        path = Path(path)
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        self.r.config.weights.update(data.get("weights", {}))
        self._recorded = int(data.get("recorded", 0))
        for t in data.get("own_tasks", []):
            self._register(t["id"], self.touched_kind, t["text"], t["files"])
            self.own_tasks.append(t)
        self.trails = {(a, b): (w, h) for a, b, w, h in data.get("trails", [])}
        self.cofix = {(a, b): n for a, b, n in data.get("cofix", [])}
        return True
