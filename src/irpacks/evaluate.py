"""Retrieval evaluation against source-verified evidence (eval/questions.yaml).

Metrics (retrieval/evidence only — there is no answer-generation component, so no answer accuracy or
abstention quality is measured or claimed):

* evidence_complete@k — every evidence group of the question is satisfied by some chunk in the top k
  (a group is a set of alternative elements; table-row evidence also requires the row's native locator);
* group_recall@k — fraction of evidence groups satisfied;
* first_hit_rank / MRR — rank of the first result satisfying any group;
* citation_valid — each satisfying chunk's citation covers the evidence element's PDF pages and its printed
  labels agree with the page map;
* required_context_complete — `irpacks context` on the satisfying chunk returns all required context within
  the budget (questions marked requires_context, or chunks that declare required context);
* duplicate_rate — share of top-k results that repeat elements already returned by a higher-ranked result.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import yaml

from . import search as search_mod
from .context import expand
from .util import PROJECT_ROOT, read_json, sha256_text, utc_now, write_json

EVAL_DIR = PROJECT_ROOT / "eval"


def evidence_quote(el: dict, native_locator: str | None = None) -> str:
    """The verbatim evidence quote for an element (or one of its table rows): whitespace-normalised, ≤ 200 chars."""
    if native_locator:
        row = next((r for r in el["attrs"]["rows"] if r.get("native_locator") == native_locator), None)
        if row is None:
            raise KeyError(f"no row {native_locator} in {el['id']}")
        quote = " | ".join(c["text"] for c in row["cells"])
    elif el["type"] == "table":
        quote = (el["attrs"].get("caption") or "") + " — " + " | ".join(c["text"] for c in el["attrs"]["rows"][1]["cells"])
    elif el["type"] == "figure":
        quote = el["attrs"].get("caption") or ""
    else:
        quote = el["text"]
    return " ".join(quote.split())[:200]


def load_questions(verify: bool = True, path: str | Path | None = None) -> dict:
    data = yaml.safe_load(Path(path or EVAL_DIR / "questions.yaml").read_text(encoding="utf-8"))
    if verify:
        docs = {}
        for q in data["questions"]:
            for group in q["evidence"]:
                for alt in group:
                    if alt["pack"] not in docs:
                        d = read_json(PROJECT_ROOT / "build" / "local" / alt["pack"] / "document.json")
                        docs[alt["pack"]] = {e["id"]: e for e in d["elements"]}
                    el = docs[alt["pack"]].get(alt["element_id"])
                    if el is None:
                        raise SystemExit(f"{q['id']}: evidence element {alt['element_id']} no longer exists")
                    if alt.get("quote") is None:  # quote withheld (source not cleared for redistribution): check its hash
                        if sha256_text(evidence_quote(el, alt.get("native_locator"))) != alt["quote_sha256"]:
                            raise SystemExit(f"{q['id']}: evidence for {alt['element_id']} no longer matches its recorded hash")
                        continue
                    hay = el["text"] if el["type"] not in ("table", "figure") else json.dumps(el["attrs"], ensure_ascii=False)
                    probe = alt["quote"][:60]
                    if el["type"] == "table":
                        probe = probe.split(" — ")[-1][:40] if " — " in probe else probe[:40]
                    if " ".join(probe.split()) not in " ".join(hay.split()) and el["type"] != "table":
                        raise SystemExit(f"{q['id']}: evidence quote for {alt['element_id']} no longer matches its text")
    return data


def _satisfies(chunk: dict, alt: dict) -> bool:
    if chunk["pack_id"] != alt["pack"] or alt["element_id"] not in chunk["source_element_ids"]:
        return False
    if alt.get("native_locator"):
        return any(s.get("native_locator") == alt["native_locator"] for s in chunk["source_spans"])
    return True


def score_question(q: dict, ranked_ids: list[str], chunks: dict[str, dict], index: Path, k: int,
                   page_labels: dict[tuple[str, int], str | None], budget: int) -> dict:
    ranked = [chunks[c] for c in ranked_ids[:k] if c in chunks]
    res: dict[str, Any] = {"id": q["id"], "split": q["split"], "answerable": q["answerable"],
                           "returned": [c["chunk_id"] for c in ranked]}
    seen: set[tuple[str, str]] = set()
    dups = 0
    for c in ranked:
        els = {(c["pack_id"], e) for e in c["source_element_ids"]}
        if els & seen:
            dups += 1
        seen |= els
    res["duplicates"] = dups
    if not q["answerable"]:
        res["note"] = "unanswerable: retrieval returned results; abstention is not evaluated without a generator"
        return res
    groups_hit, first_rank, satisfying = [], None, []
    for group in q["evidence"]:
        hit_rank = None
        for r, c in enumerate(ranked, 1):
            if any(_satisfies(c, a) for a in group):
                hit_rank = r
                satisfying.append((c, [a for a in group if _satisfies(c, a)]))
                break
        groups_hit.append(hit_rank)
        if hit_rank and (first_rank is None or hit_rank < first_rank):
            first_rank = hit_rank
    res["group_ranks"] = groups_hit
    res["evidence_complete"] = all(r is not None for r in groups_hit)
    res["group_recall"] = sum(1 for r in groups_hit if r) / len(groups_hit)
    res["first_hit_rank"] = first_rank
    # citation validity for satisfying chunks
    cv = []
    for c, alts in satisfying:
        ok = True
        cpages = set(c["citation"]["pdf_pages"])
        curls = set(c["citation"].get("urls") or ([c["citation"]["url"]] if c["citation"].get("url") else []))
        for a in alts:
            pages = {l["pdf_page"] for l in a["locators"] if l.get("pdf_page")}
            if not pages <= cpages:
                ok = False
            urls = {l["url"] for l in a["locators"] if l.get("url")}  # unpaginated sources: URL#anchor must be cited
            if not urls <= curls:
                ok = False
        for p, lab in zip(c["citation"]["pdf_pages"], c["citation"]["printed_pages"]):
            if page_labels.get((c["pack_id"], p), lab) != lab:
                ok = False
        cv.append(ok)
    res["citation_valid"] = all(cv) if cv else None
    # required context
    rc = []
    for c, _ in satisfying:
        if q.get("requires_context") or c["required_context_ids"]:
            ex = expand(index, c["chunk_id"], budget=budget)
            rc.append(bool(ex.get("required_context_complete")))
    res["required_context_complete"] = all(rc) if rc else None
    if q.get("needs_visual"):
        res["visual_asset_exposed"] = any(any(a.get("kind") == "figure" for a in c["asset_refs"]) for c, _ in satisfying)
    return res


def run_eval(split: str = "all", index: str | None = None, k: int = 5, out: str | None = None, budget: int = 3000,
             backend: str = "local-fts5", results_file: str | None = None, label: str | None = None,
             questions: str | None = None, ranking: str = "v4") -> dict:
    """``ranking``: "v4" (search.RANKING), or a frozen earlier ranking: "v3" (search.RANKING_V3) or "v2"."""
    cfg = {"v2": search_mod.RANKING_V2, "v3": search_mod.RANKING_V3}.get(ranking, search_mod.RANKING)
    qs = load_questions(path=questions)
    idx = Path(index) if index else PROJECT_ROOT / "build" / "local" / "index.sqlite"
    if not idx.exists():
        raise SystemExit(f"no index at {idx}; run `uv run irpacks index`")
    chunks = {c["chunk_id"]: c for c in search_mod.all_chunks(idx)}
    page_labels = {}
    for pack in {c["pack_id"] for c in chunks.values()}:
        pm_file = PROJECT_ROOT / "build" / "local" / pack / "page-map.json"
        for p in (read_json(pm_file)["pages"] if pm_file.exists() else []):  # unpaginated packs have none
            page_labels[(pack, p["page"])] = p["page_label"]
    external = json.loads(Path(results_file).read_text()) if results_file else None
    selected = [q for q in qs["questions"] if split == "all" or q["split"] == split]
    if not selected:
        raise SystemExit(f"no questions with split {split!r} in {questions or 'eval/questions.yaml'}")
    rows, used = [], {}
    for q in selected:
        if external is not None:
            ranked = external["results"].get(q["id"], [])
        else:
            r = search_mod.search(idx, q["question"], k=k, ranking=cfg)
            ranked = [x["chunk_id"] for x in r["results"]]
            used.setdefault("ranking", r["ranking"])
            used.setdefault("dense", r.get("dense"))
        rows.append(score_question(q, ranked, chunks, idx, k, page_labels, budget))
    summary = summarize(rows, k)
    result = {
        "run_at": utc_now(), "backend": backend if external is None else external.get("backend", backend),
        "label": label, "split": split, "k": k, "context_budget_tokens": budget,
        "questions_file": Path(questions).name if questions else "questions.yaml",
        "index": idx.relative_to(PROJECT_ROOT).as_posix() if idx.is_relative_to(PROJECT_ROOT) else str(idx),
        "tokenizer": __import__("irpacks.tokens", fromlist=["identity"]).identity(),
        "generation_model": None,
        "backend_details": (external or {}).get("details") if external is not None else {
            "ranking": used.get("ranking", cfg), "dense": used.get("dense"), "fts_tokenizer": search_mod.FTS_TOKENIZER,
            "index_text": _index_meta(idx).get("index_text", "markdown"), "dense_model": _index_meta(idx).get("dense_model"),
            "bm25_column_weights": {"heading_path": 2.0, "text": 1.0}},
        "summary": summary, "questions": rows,
    }
    if out:
        write_json(out, result)
    return result


def _index_meta(idx: Path) -> dict:
    import sqlite3
    con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()


def summarize(rows: list[dict], k: int) -> dict:
    ans = [r for r in rows if r["answerable"]]
    complete = [r for r in ans if r["evidence_complete"]]
    multi = [r for r in ans if len(r["group_ranks"]) > 1]
    cv = [r["citation_valid"] for r in ans if r.get("citation_valid") is not None]
    rc = [r["required_context_complete"] for r in ans if r.get("required_context_complete") is not None]
    ret = sum(len(r["returned"]) for r in rows)
    return {
        "questions": len(rows),
        "answerable": len(ans),
        "unanswerable": len(rows) - len(ans),
        f"evidence_complete_at_{k}": f"{len(complete)}/{len(ans)}",
        f"evidence_complete_at_{k}_rate": round(len(complete) / len(ans), 3) if ans else None,
        "mean_group_recall": round(statistics.mean(r["group_recall"] for r in ans), 3) if ans else None,
        "multi_evidence_questions": f"{sum(1 for r in multi if r['evidence_complete'])}/{len(multi)} complete",
        "mrr": round(statistics.mean((1 / r["first_hit_rank"]) if r["first_hit_rank"] else 0 for r in ans), 3) if ans else None,
        "citation_valid": f"{sum(cv)}/{len(cv)}",
        "required_context_complete": f"{sum(rc)}/{len(rc)}",
        "duplicate_results": f"{sum(r['duplicates'] for r in rows)}/{ret}",
        "failures": [r["id"] for r in ans if not r["evidence_complete"]],
        "scoring_rule": ("a question counts as retrieved only if ALL its evidence groups are satisfied within the top "
                         f"{k}; group alternatives are interchangeable; table-row evidence also needs the row locator"),
    }
