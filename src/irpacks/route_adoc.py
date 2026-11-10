"""Route: Asciidoctor HTML structure verified block-by-block against the PDF."""

from __future__ import annotations

import collections
import time
from pathlib import Path

from . import adochtml, canonical, docmodel, pdfsource


def _pilot_scope(pdf_path: str, html_path: str, recipe: dict) -> dict:
    """Physical page ranges for the pilot's kept chapters/sections (from bookmarks, not offsets)."""
    keep = recipe["pilot"]["keep"]
    marks = pdfsource.bookmarks(pdf_path)
    n_total = pdfsource.page_count(pdf_path)
    parsed = adochtml.parse_document(html_path)
    order = canonical.assign_ids(parsed["root"])
    by_id = {e["id"]: e for e in order}
    chapters = [(i, m) for i, m in enumerate(marks) if m["title"].startswith("Chapter ")]
    pages: set[int] = set()
    detail = []
    for kid in keep:
        el = by_id[kid]
        ch_id = kid.split(".")[0]
        ch_num = int(ch_id[2:])
        ci, cm = next((i, m) for i, m in chapters if m["title"].startswith(f"Chapter {ch_num}."))
        ch_end = next((m["page"] for m in marks[ci + 1:] if m["level"] <= cm["level"] and m["page"] > cm["page"]),
                      n_total + 1) - 1
        if el["type"] == "chapter":
            start, end = cm["page"], ch_end
        else:
            title = el["attrs"]["title"]
            j = next(i for i in range(ci + 1, len(marks)) if marks[i]["title"] == title and marks[i]["level"] > cm["level"])
            start = marks[j]["page"]
            nxt = next((m["page"] for m in marks[j + 1:] if m["level"] <= marks[j]["level"]), ch_end + 1)
            end = max(start, nxt if nxt == start else nxt - 1)
            if nxt > start:
                end = nxt  # the next section may begin on the same page this one ends
        rng = set(range(start, min(end, ch_end) + 1))
        rng |= {ch_end - 1, ch_end}  # chapter-end notes as printed
        rng |= {cm["page"]}  # chapter opening page (chapter heading)
        part = next((m for m in reversed(marks[:ci]) if m["title"].startswith("Part ")), None)
        if part:
            rng |= {part["page"]}  # part page (part heading and introduction)
        pages |= rng
        detail.append({"keep": kid, "pages": [start, min(end, ch_end)], "chapter_end_pages": [ch_end - 1, ch_end]})
    return {"keep": keep, "pages": sorted(pages), "detail": detail}


def run(pdf_path: str, html_path: str, recipe: dict, meta: dict, cache_dir: Path, *, scope: str | None = None,
        log=print) -> dict:
    t0 = time.time()
    sc = _pilot_scope(pdf_path, html_path, recipe) if scope == "pilot" else None
    result = canonical.build(pdf_path, html_path, recipe, {}, cache_dir, log=log, scope=sc)
    fin = docmodel.finalize(result, meta)
    elapsed = round(time.time() - t0, 1)
    qa = {"alignment-report.json": alignment_report(result, fin, elapsed),
          "page-inventory.json": {"pages": [{k: p[k] for k in ("page", "page_label", "content_class", "body_chars",
                                                                 "vector_objects", "body_key_chars", "uncovered_key_chars")}
                                            for p in fin["page_map"]["pages"]]}}
    if sc:
        qa["pilot-scope.json"] = sc
    return {"document": fin["document"], "page_map": fin["page_map"], "annotations": fin["annotations"],
            "images": result["parsed"]["images"], "issues": result["issues"], "qa": qa,
            "from_cache": result["from_cache"], "bookmarks": result["bookmarks"], "scope": sc,
            "parsed_meta": result["parsed"]["meta"], "unknown_blocks": result["parsed"]["unknown_blocks"],
            "layout_skips": result["parsed"]["layout_skips"]}


def alignment_report(result: dict, fin: dict, elapsed: float) -> dict:
    units = result["units"] + result["fn_units"]
    st = collections.Counter(u.status for u in units)
    var = collections.Counter(p.resolution for u in units for p in u.parts if p.kind != "lit")
    vis = collections.Counter()
    other = []
    for e in fin["document"]["elements"]:
        ex = e.get("extraction") or {}
        for d in ex.get("visible_differences") or []:
            vis[d["class"]] += 1
            if d["class"] == "other":
                other.append({"element_id": e["id"], **d})
    methods = collections.Counter((e.get("extraction") or {}).get("method") for e in fin["document"]["elements"]
                                  if e.get("extraction"))
    xref_changes = []
    for e in fin["document"]["elements"]:
        for v in (e.get("extraction") or {}).get("variables", []):
            if v["kind"] == "xref" and v["resolution"] in ("pdf_title", "pdf_region"):
                xref_changes.append({"element_id": e["id"], "html_text": v["html_text"], "pdf_text": v["pdf_text"],
                                     "target": v["target"], "resolution": v["resolution"]})
    link_status = collections.Counter()
    unmatched_links = []
    for e in fin["document"]["elements"]:
        for l in e["attrs"].get("links") or []:
            st_ = l.get("pdf_link_annotation", "unchecked")
            link_status[st_] += 1
            if st_ == "not_found" and len(unmatched_links) < 50:
                unmatched_links.append({"element_id": e["id"], "href": l["href"], "text": l.get("text")})
    gaps = collections.Counter(g["class"] for g in result["gaps"])
    gap_chars = collections.Counter()
    for g in result["gaps"]:
        gap_chars[g["class"]] += g["n_key_chars"]
    stream = result["stream"]
    covered = sum(b - a for a, b in __import__("irpacks.align", fromlist=["covered_ranges"]).covered_ranges(units)
                  if a is not None and b is not None)
    by_chapter = collections.defaultdict(collections.Counter)
    for u in result["units"]:
        by_chapter[result["unit_of"].get(u.element_id)][u.status] += 1
    return {
        "route": "asciidoctor_html_verified_against_pdf",
        "elapsed_s": elapsed,
        "pdf_extract_elapsed_s": result["pdf_extract_elapsed_s"],
        "structured_candidate_generator": result["parsed"]["meta"].get("generator"),
        "units_total": len(units),
        "unit_status": dict(st),
        "unit_status_by_top_level_unit": {k or "none": dict(v) for k, v in sorted(by_chapter.items(), key=lambda kv: str(kv[0]))},
        "variable_resolution": dict(var),
        "xref_wording_taken_from_pdf": xref_changes,
        "visible_difference_classes": dict(vis),
        "visible_differences_other": other,
        "element_methods": dict(methods),
        "pdf_body_key_chars": len(stream.key),
        "pdf_key_chars_covered_by_structured_units": covered,
        "uncovered_gap_classes": dict(gaps),
        "uncovered_gap_key_chars_by_class": dict(gap_chars),
        "unclassified_gaps": [g for g in result["gaps"] if g["class"] == "unclassified"],
        "external_links_vs_pdf_annotations": dict(link_status),
        "external_links_not_found_in_pdf": unmatched_links,
        "structured_candidate_unknown_blocks": result["parsed"]["unknown_blocks"],
        "structured_candidate_layout_skips": result["parsed"]["layout_skips"],
        "html_only_content": [i for i in result["issues"] if i["kind"] in ("footnote_not_in_pdf",)],
        "interpretation": (
            "Equivalence is established per block: a structured-candidate block is used only when its literal "
            "text is located in the PDF body text (case/whitespace/punctuation-insensitive key) at a position "
            "consistent with document order; punctuation-level differences are classified above. Matching "
            "titles or metadata were not treated as evidence of equivalence."),
    }
