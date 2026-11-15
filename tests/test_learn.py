
from trailmark.learn import WEIGHT_MAX, pairwise_update
from trailmark.retrieve import DEFAULT_WEIGHTS


def score(w, f):
    return sum(w.get(k, 0.0) * v for k, v in f.items())


def test_pairwise_update_moves_weight_toward_the_evidence_the_right_file_had():
    w = dict(DEFAULT_WEIGHTS)
    gold = {"bm25": 0.2, "cochange": 0.9}
    wrong = {"bm25": 0.9}
    assert score(w, gold) < score(w, wrong)
    before = dict(w)
    for _ in range(200):
        pairwise_update(w, gold, wrong, lr=0.1)
    assert w["cochange"] > before["cochange"]
    assert w["bm25"] < before["bm25"]
    assert score(w, gold) > score(w, wrong)
    # features neither file had are untouched
    assert w["imports_in"] == before["imports_in"]


def test_pairwise_update_keeps_weights_bounded_and_loss_falls():
    w = dict(DEFAULT_WEIGHTS)
    gold, wrong = {"trail": 1.0}, {"mention_name": 1.0}
    losses = [pairwise_update(w, gold, wrong, lr=1.0) for _ in range(500)]
    assert losses[-1] < losses[0]
    assert all(0.0 <= v <= WEIGHT_MAX for v in w.values())
    assert w["mention_name"] == 0.0  # clipped, never negative


def test_pairwise_update_barely_moves_when_already_well_ranked():
    w = dict(DEFAULT_WEIGHTS)
    before = dict(w)
    pairwise_update(w, {"bm25": 1.0, "mention_name": 1.0, "cochange": 1.0}, {"bm25": 0.01}, lr=0.05)
    assert all(abs(w[k] - before[k]) < 0.01 for k in w)
