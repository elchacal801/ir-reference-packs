"""Ranking-feature ablation on the TUNING split only (never run on held-out questions).

Records which generic ranking features were tried before the configuration in irpacks.search.RANKING was frozen.

    uv run python eval/ablate_tuning.py   # writes eval/results/tuning-ablation.json
"""
from __future__ import annotations

import json
import sys

from irpacks import evaluate, search
from irpacks.util import PROJECT_ROOT, utc_now, write_json

IDX = PROJECT_ROOT / "build" / "local" / "index.sqlite"
CW = {"notes": 0.5, "references": 0.5}
OFF = dict(stopwords=False, all_terms_first=False, corpus_weights={}, proximity_weight=0.0, visual_boost=1.0,
           alias_slots=0)
VARIANTS = {
    "all features off (BM25 over all query words)": OFF,
    "+ stopword removal": {**OFF, "stopwords": True},
    "+ all-terms-first": {**OFF, "all_terms_first": True},
    "+ corpus weights": {**OFF, "corpus_weights": CW},
    "+ proximity 1.0": {**OFF, "proximity_weight": 1.0},
    "+ visual intent 1.5": {**OFF, "visual_boost": 1.5},
    "+ alias slots 2": {**OFF, "alias_slots": 2},
    "proximity 1.0 + alias 2": {**OFF, "proximity_weight": 1.0, "alias_slots": 2},
    "proximity 1.0 + alias 2 + visual 1.5": {**OFF, "proximity_weight": 1.0, "alias_slots": 2, "visual_boost": 1.5},
    "FROZEN: proximity 1.0 + alias 2 + visual 1.5 + corpus weights": {
        **OFF, "proximity_weight": 1.0, "alias_slots": 2, "visual_boost": 1.5, "corpus_weights": CW},
    "frozen + all-terms-first": {**OFF, "proximity_weight": 1.0, "alias_slots": 2, "visual_boost": 1.5,
                                 "corpus_weights": CW, "all_terms_first": True},
    "frozen + stopword removal": {**OFF, "proximity_weight": 1.0, "alias_slots": 2, "visual_boost": 1.5,
                                  "corpus_weights": CW, "stopwords": True},
    "frozen with proximity 0.5": {**OFF, "proximity_weight": 0.5, "alias_slots": 2, "visual_boost": 1.5,
                                  "corpus_weights": CW},
    "frozen with proximity 2.0": {**OFF, "proximity_weight": 2.0, "alias_slots": 2, "visual_boost": 1.5,
                                  "corpus_weights": CW},
}


def main() -> int:
    qs = [q for q in evaluate.load_questions()["questions"] if q["split"] == "tuning"]
    chunks = {c["chunk_id"]: c for c in search.all_chunks(IDX)}
    labels = {}
    for p in {c["pack_id"] for c in chunks.values()}:
        for pg in evaluate.read_json(PROJECT_ROOT / "build" / "local" / p / "page-map.json")["pages"]:
            labels[(p, pg["page"])] = pg["page_label"]
    out = []
    for name, cfg in VARIANTS.items():
        rows = [evaluate.score_question(q, [x["chunk_id"] for x in search.search(IDX, q["question"], k=5, ranking=cfg)["results"]],
                                        chunks, IDX, 5, labels, 3000) for q in qs]
        s = evaluate.summarize(rows, 5)
        out.append({"variant": name, "ranking": {**search.RANKING, **cfg}, "evidence_complete_at_5": s["evidence_complete_at_5"],
                    "mean_group_recall": s["mean_group_recall"], "mrr": s["mrr"], "failures": s["failures"]})
        print(f"{name:62s} {s['evidence_complete_at_5']:>6}  recall={s['mean_group_recall']:.3f}  mrr={s['mrr']:.3f}  {s['failures']}")
    write_json(PROJECT_ROOT / "eval" / "results" / "tuning-ablation.json",
               {"run_at": utc_now(), "split": "tuning", "k": 5, "note": "tuning split only; held-out never used for selection",
                "variants": out})
    return 0


if __name__ == "__main__":
    sys.exit(main())
