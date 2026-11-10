"""Ranking diagnosis on the tuning pool: where does each question's evidence rank, up to depth 50?

Usage:  uv run python eval/diagnose_ranks.py [--ranking '{"alias_mode": "per_pack"}'] [--detail] [--json out.json]

The tuning pool is every question set except the sealed held-out set y (questions-y.yaml): the original tuning and
held-out splits, the Microsoft, vendor and CISA sets, x-tuning, and set x (each held-out set was run once before it
joined the pool). The script refuses the y-heldout split, so the sealed set cannot be looked at while tuning.
Reported per configuration: evidence complete@5, top-1 and top-3 first hits, MRR@5, and per question the rank of
each evidence group's first satisfying chunk (None when not in the top 50).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from irpacks import evaluate, search  # noqa: E402

POOL = ["questions.yaml", "questions-ms.yaml", "questions-vendor.yaml", "questions-cisa.yaml", "questions-x-tuning.yaml",
        "questions-x.yaml"]  # x was run once for v3 (2026-10-25); it is tuning data from v4 on
SEALED = "y-heldout"


def index_path() -> Path:
    """The combined index, or another one for an experiment (DIAGNOSE_INDEX)."""
    import os
    return Path(os.environ.get("DIAGNOSE_INDEX") or ROOT / "build" / "local" / "index.sqlite")


def pool() -> list[dict]:
    out = []
    for f in POOL:
        for q in evaluate.load_questions(path=ROOT / "eval" / f)["questions"]:
            if q["split"] == SEALED:
                raise SystemExit("the sealed held-out split must not be used for tuning")
            out.append(dict(q, _file=f))
    return out


def run(ranking: dict | None, depth: int = 50) -> dict:
    idx = index_path()
    chunks = {c["chunk_id"]: c for c in search.all_chunks(idx)}
    rows = []
    for q in pool():
        if not q["answerable"]:
            continue
        # top-5 behaviour comes from a k=5 search (slot reservation depends on k); depth ranks from a k=50 search
        top5 = [chunks[r["chunk_id"]] for r in search.search(idx, q["question"], k=5, ranking=ranking)["results"]]
        deep = [chunks[r["chunk_id"]] for r in search.search(idx, q["question"], k=depth, ranking=ranking)["results"]]
        ranked = top5 + [c for c in deep if c["chunk_id"] not in {x["chunk_id"] for x in top5}]
        ranks = []
        for group in q["evidence"]:
            ranks.append(next((n for n, c in enumerate(ranked, 1) if any(evaluate._satisfies(c, a) for a in group)), None))
        first = min((r for r in ranks if r), default=None)
        rows.append({"id": q["id"], "file": q["_file"], "question": q["question"], "group_ranks": ranks,
                     "complete5": all(r is not None and r <= 5 for r in ranks),
                     "rr5": (1 / first) if first and first <= 5 else 0.0, "first": first,
                     "top5_packs": [c["pack_id"] for c in ranked[:5]]})
    n = len(rows)
    summary = {"answerable": n, "complete@5": sum(r["complete5"] for r in rows),
               "top1": sum(1 for r in rows if r["first"] == 1), "top3": sum(1 for r in rows if r["first"] and r["first"] <= 3),
               "mrr@5": round(sum(r["rr5"] for r in rows) / n, 3),
               "multi_group_complete@5": f"{sum(r['complete5'] for r in rows if len(r['group_ranks']) > 1)}/"
                                         f"{sum(1 for r in rows if len(r['group_ranks']) > 1)}"}
    return {"ranking": {**search.RANKING, **(ranking or {})}, "summary": summary, "rows": rows}


def per_source(ranking: dict | None, k: int = 5) -> dict:
    """Multi-source questions searched as the MCP `per_source` option does: one search per source, each group
    satisfied within its own source's top k. Sources are the packs the question names; when it names fewer than
    two, the packs of its evidence groups (as an agent passing `packs` would)."""
    idx = index_path()
    import sqlite3
    con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
    aliases = {p: json.loads(a) for p, a in con.execute("SELECT pack_id, source_aliases FROM packs")}
    con.close()
    chunks = {c["chunk_id"]: c for c in search.all_chunks(idx)}
    rows = []
    for q in pool():
        if not q["answerable"] or len(q["evidence"]) < 2:
            continue
        named = search.named_sources(q["question"], aliases)[1]
        sources = named if len(named) >= 2 else list(dict.fromkeys(a["pack"] for g in q["evidence"] for a in g))
        got = {p: [chunks[r["chunk_id"]] for r in search.search(idx, q["question"], k=k, packs=[p], ranking=ranking)["results"]]
               for p in sources}
        ok = [any(evaluate._satisfies(c, a) for p in sources for c in got[p] for a in g) for g in q["evidence"]]
        rows.append({"id": q["id"], "sources": sources, "named": len(named) >= 2, "groups_found": ok, "complete": all(ok)})
    return {"multi_source_questions": len(rows), "complete_per_source": sum(r["complete"] for r in rows), "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ranking", default=None, help="JSON overrides of search.RANKING")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--per-source", action="store_true", help="also score multi-source questions searched per source")
    ap.add_argument("--json")
    a = ap.parse_args()
    r = run(json.loads(a.ranking) if a.ranking else None)
    print(json.dumps(r["summary"]))
    if a.per_source:
        ps = per_source(json.loads(a.ranking) if a.ranking else None)
        r["per_source"] = ps
        print(json.dumps({k: v for k, v in ps.items() if k != "rows"}),
              [(x["id"], x["complete"]) for x in ps["rows"]])
    if a.detail:
        for row in r["rows"]:
            if not row["complete5"] or row["first"] != 1:
                print(f"{row['id']:5s} ranks={row['group_ranks']} top5={row['top5_packs']}  {row['question'][:90]}")
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=1))


if __name__ == "__main__":
    main()
