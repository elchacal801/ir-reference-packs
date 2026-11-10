"""Finalize builder output into the canonical document and page map.

``document.json`` is the single canonical representation per source edition.
Every export (Markdown, chunks, search index, public subset) is generated from
it.  Element records carry provenance (extraction method, mapping status,
typed source spans) and a content hash.
"""

from __future__ import annotations

import collections
import re
from typing import Any

from . import pdfsource
from .adochtml import MARKERS
from .canonical import DOC_SCHEMA_VERSION, _strip_markers
from .util import sha256_text, slugify

COORDS = pdfsource.COORDS

# mapping statuses that may enter the validated retrieval corpus
CITABLE = {"verified", "verified_with_differences", "pdf_text_fallback", "pdf_only", "verified_image",
           "visually_verified", "native_structure"}

_INTERNAL = {"align", "_units", "_resolution", "_spans", "_image_span", "_excluded", "children", "parent_id", "id",
             "type", "text", "md", "attrs", "anchor"}


def _clean_attrs(attrs: dict) -> dict:
    out = {}
    for k, v in attrs.items():
        if k in ("footnote_refs",):
            continue
        out[k] = v
    return out


def _leaf_status(e: dict) -> str:
    res = e.get("_resolution")
    if e["type"] in ("figure",):
        img = e["attrs"].get("pdf_image")
        if img and img.get("dims_match"):
            return "verified_image"
        return "unverified" if not img else "position_inferred"
    if e["type"] == "math":
        return "position_inferred" if e["attrs"].get("pdf_image") else "unverified"
    if res is None:
        return "aggregate" if e["children"] else ("not_applicable" if not e.get("text") else "unverified")
    m = res["method"]
    if m == "pdf_text_fallback":
        return "pdf_text_fallback"
    if m == "unverified":
        return "unverified"
    others = [d for d in res["visible_differences"] if d["class"] == "other"]
    if m.endswith("with_row_differences"):
        return "verified_with_differences"
    return "verified_with_differences" if others else "verified"


def finalize(result: dict, meta: dict) -> dict:
    """Return {"document": ..., "page_map": ..., "alignment": ..., "annotations": [...]}."""
    order: list[dict] = result["order"]
    pages = result["pages"]
    by_id = result["by_id"]
    issues = result["issues"]

    # --- insert PDF-only regions and footnotes into the tree
    root = result["root"]
    toc = result["pdf_only"].get("toc")
    if toc and toc.get("entries"):
        div = {"type": "division", "anchor": "printed-table-of-contents", "text": "", "md": "", "children": [],
               "attrs": {"level": 1, "title": "Table of Contents", "pdf_only": True}, "align": [],
               "id": "fm-table-of-contents", "parent_id": "doc"}
        n = 0
        for ent in toc["entries"]:
            n += 1
            el = {"type": "heading" if ent["kind"] == "heading" else "toc_entry", "anchor": None,
                  "text": ent["text"], "md": "", "children": [], "align": [],
                  "attrs": {"title": ent["title"], "target_page_label": ent["target_label"], "indent": ent["indent"],
                            **({"level": 1} if ent["kind"] == "heading" else {})},
                  "id": f"fm-table-of-contents.{'h' if ent['kind'] == 'heading' else 'toc'}{n:03d}",
                  "parent_id": "fm-table-of-contents",
                  "_pdf_span": {"page": ent["page"], "page_label": ent["page_label"], "bbox": ent["bbox"]}}
            div["children"].append(el)
        idx = next((i for i, c in enumerate(root["children"]) if c["id"] == "fm-front-matter"), -1)
        root["children"].insert(idx + 1, div)
    ix = result["pdf_only"].get("index")
    if ix and ix.get("entries"):
        tgt = next((c for c in root["children"] if c["id"] == "bm-index"), None)
        if tgt is not None:
            n = 0
            for ent in ix["entries"]:
                if ent["kind"] == "heading":
                    # the structured candidate already provides the "Index" heading; attach the PDF span to it
                    head = next((c for c in tgt["children"] if c["type"] == "heading"), None)
                    if head is not None and not head.get("_spans"):
                        head["_pdf_span"] = {"page": ent["page"], "page_label": ent["page_label"], "bbox": ent["bbox"]}
                    continue
                n += 1
                kind = "index_entry"
                el = {"type": kind, "anchor": None, "text": ent["text"], "md": "", "children": [], "align": [],
                      "attrs": {"entry_kind": ent["kind"], "level": ent.get("level"), "term": ent.get("term"),
                                "path": ent.get("path"), "locator_labels": ent.get("locators", []),
                                "see": ent.get("see")},
                      "id": f"bm-index.ix{n:04d}", "parent_id": "bm-index",
                      "_pdf_span": {"page": ent["page"], "page_label": ent["page_label"], "bbox": ent["bbox"]}}
                tgt["children"].append(el)
    annotations = []
    for cid, els in result["fn_elems"].items():
        parent = by_id.get(cid)
        for el in els:
            if el.get("_excluded"):
                annotations.append({
                    "annotation_id": f"ann-{el['id']}", "kind": "structured_candidate_only_text",
                    "origin": "structured_candidate:html", "review_status": "unreviewed",
                    "target_element_id": None, "related_label": el["attrs"]["label"],
                    "text": el["text"],
                    "note": "Footnote definition present in the HTML edition but not printed in the PDF; the PDF "
                            "caption shows the reference label without a note. Not part of the converted edition text.",
                })
                continue
            if parent is not None:
                parent["children"].append(el)

    # --- flatten in document order
    flat: list[dict] = []

    def walk(e, parent_id):
        e["parent_id"] = parent_id
        flat.append(e)
        for c in e["children"]:
            walk(c, e["id"])

    walk(root, None)

    # --- leaf spans and statuses
    page_labels = {p["page"]: p["label"] for p in pages}
    recs: dict[str, dict] = {}
    for i, e in enumerate(flat):
        spans = []
        status = _leaf_status(e)
        for s in e.get("_spans") or []:
            spans.append({"locator_type": "pdf_page", "page": s["page"], "page_label": s["page_label"],
                          "bbox": s["bbox"], "coords": COORDS, "role": "text",
                          "n_key_chars": s.get("n_key_chars"), "mapping_status": "verified" if status in (
                              "verified", "verified_with_differences") else status})
        if e.get("_image_span"):
            s = e["_image_span"]
            spans.append({"locator_type": "pdf_page", "page": s["page"], "page_label": s["page_label"],
                          "bbox": s["bbox"], "coords": COORDS, "role": "image", "n_key_chars": 0,
                          "mapping_status": "verified_image" if status == "verified_image" else "position_inferred"})
        if e.get("_pdf_span"):
            s = e["_pdf_span"]
            spans.append({"locator_type": "pdf_page", "page": s["page"], "page_label": s["page_label"],
                          "bbox": s["bbox"], "coords": COORDS, "role": "text", "n_key_chars": None,
                          "mapping_status": "pdf_only"})
            status = "pdf_only" if status in ("unverified", "not_applicable", "aggregate") and not e["children"] else status
        text = e.get("text") or ""
        if any(m in text for m in MARKERS):
            text = _strip_markers(text)
        md = e.get("md") or ""
        res = e.get("_resolution")
        rec = {
            "id": e["id"],
            "type": e["type"],
            "order": i,
            "parent_id": e["parent_id"],
            "children": [c["id"] for c in e["children"]],
            "anchor": e.get("anchor"),
            "text": text,
            "md": md,
            "attrs": _clean_attrs(e.get("attrs") or {}),
            "source_spans": spans,
            "mapping_status": status,
            "extraction": None,
        }
        if res:
            rec["extraction"] = {
                "method": res["method"],
                "route": meta["route"],
                "unit_statuses": res["unit_statuses"],
                "similarity": res["ratio"],
                "visible_equal": res["visible_equal"],
                "visible_differences": res["visible_differences"][:20],
                "variables": res["variables"],
            }
        elif e.get("_pdf_span"):
            rec["extraction"] = {"method": "pdf_text", "route": meta["route"], "unit_statuses": [],
                                 "similarity": None, "visible_equal": None, "visible_differences": [], "variables": []}
        elif e["type"] in ("figure", "math"):
            rec["extraction"] = {"method": "html_asset_matched_to_pdf_image" if e["type"] == "figure" else
                                 "html_latex_with_pdf_rendering", "route": meta["route"], "unit_statuses": [],
                                 "similarity": None, "visible_equal": None, "visible_differences": [], "variables": []}
        recs[e["id"]] = rec

    # --- containers: aggregate spans bottom-up
    for e in reversed(flat):
        rec = recs[e["id"]]
        if not e["children"]:
            continue
        agg: dict[int, dict] = {}
        for s in rec["source_spans"]:
            agg.setdefault(s["page"], dict(s))
        for cid in rec["children"]:
            for s in recs[cid]["source_spans"]:
                a = agg.get(s["page"])
                if a is None:
                    agg[s["page"]] = {"locator_type": "pdf_page", "page": s["page"], "page_label": s["page_label"],
                                      "bbox": list(s["bbox"]), "coords": COORDS, "role": "aggregate",
                                      "n_key_chars": None, "mapping_status": "aggregate"}
                else:
                    a["bbox"] = [min(a["bbox"][0], s["bbox"][0]), min(a["bbox"][1], s["bbox"][1]),
                                 max(a["bbox"][2], s["bbox"][2]), max(a["bbox"][3], s["bbox"][3])]
        rec["source_spans"] = [agg[k] for k in sorted(agg)]
        if rec["mapping_status"] in ("not_applicable", "unverified") and rec["type"] not in ("figure", "math"):
            rec["mapping_status"] = "aggregate"

    # --- content hashes
    for rec in recs.values():
        payload = rec["text"] if rec["text"] else ""
        if rec["type"] == "table":
            payload = "\n".join("\t".join(c["text"] for c in r["cells"]) for r in rec["attrs"]["rows"])
        if rec["type"] == "figure":
            payload = (rec["attrs"].get("sha256") or "") + (rec["attrs"].get("caption") or "")
        rec["content_hash"] = sha256_text(rec["type"] + "\x1f" + payload)

    elements = [recs[e["id"]] for e in flat]
    document = {
        "schema_version": DOC_SCHEMA_VERSION,
        **{k: meta[k] for k in ("document_id", "pack_id", "pack_version", "edition_id", "title", "authors",
                                "edition_statement", "route")},
        "sources": meta["sources"],
        "coordinate_convention": COORDS,
        "page_numbering": {"physical": "one-based PDF page index", "printed": "string label from the PDF /PageLabels "
                           "tree; never derived from a global offset"},
        "root_id": "doc",
        "page_labels": [{"page": p["page"], "label": p["label"]} for p in pages],
        "element_count": len(elements),
        "elements": elements,
    }
    page_map = build_page_map(result, elements, meta)
    return {"document": document, "page_map": page_map, "annotations": annotations}


def build_page_map(result: dict, elements: list[dict], meta: dict) -> dict:
    pages = result["pages"]
    furniture = result["furniture"]
    stream = result["stream"]
    gaps = result["gaps"]
    leaf_by_page: dict[int, list[str]] = collections.defaultdict(list)
    for el in elements:
        # leaves, text-bearing containers (e.g. list items with nested lists) and figures
        if el["children"] and not (el.get("text") and el["type"] not in ("document", "part", "chapter", "division", "section")) \
                and el["type"] != "figure":
            continue
        for s in el["source_spans"]:
            if s.get("role") == "aggregate" and el["type"] != "figure":
                continue
            if el["id"] not in leaf_by_page[s["page"]]:
                leaf_by_page[s["page"]].append(el["id"])
    key_per_page = collections.Counter(stream.key_src[k][0] for k in range(len(stream.key)))
    gaps_by_page: dict[int, list[dict]] = collections.defaultdict(list)
    uncovered = collections.Counter()
    for g in gaps:
        gaps_by_page[g["page"]].append({k: g[k] for k in ("class", "n_key_chars", "text", "table_id", "element_id")
                                        if k in g})
        uncovered[g["page"]] += g["n_key_chars"]
    img_owner = {}
    for el in elements:
        for s in el["source_spans"]:
            if s["role"] == "image":
                img_owner[(s["page"], tuple(s["bbox"]))] = el["id"]
    out_pages = []
    for p in pages:
        pno = p["page"]
        fl = furniture.get(pno, [])
        cls = pdfsource.classify_page(p, fl)
        out_pages.append({
            "page": pno,
            "page_label": p["label"],
            "width": p["width"],
            "height": p["height"],
            "rotation": p["rotation"],
            "content_class": cls["class"],
            "body_chars": cls["body_chars"],
            "vector_objects": cls["vector_objects"],
            "furniture": [{"text": f["text"], "bbox": f["bbox"]} for f in fl],
            "images": [{"bbox": im["bbox"], "srcsize": im["srcsize"], "raw_sha256": im["raw_sha256"],
                        "element_id": img_owner.get((pno, tuple(im["bbox"])))} for im in p["images"]],
            "body_key_chars": key_per_page.get(pno, 0),
            "uncovered_key_chars": uncovered.get(pno, 0),
            "uncovered_regions": gaps_by_page.get(pno, []),
            "element_ids": leaf_by_page.get(pno, []),
        })
    labels = [p["label"] for p in pages]
    return {
        "schema_version": DOC_SCHEMA_VERSION,
        "document_id": meta["document_id"],
        "source_sha256": meta["sources"]["authoritative"]["sha256"],
        "coordinate_convention": COORDS,
        "page_count": len(pages),
        "label_ranges": label_ranges(labels),
        "pages": out_pages,
    }


def _roman_to_int(s: str) -> int | None:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    if not s or any(c not in vals for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        v = vals[c]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def label_ranges(labels: list[str | None]) -> list[dict]:
    """Describe the printed-label scheme as observed runs (documentation only)."""
    runs = []
    for i, lab in enumerate(labels, start=1):
        style = "none" if lab is None else ("decimal" if lab.isdigit() else ("lower_roman" if _roman_to_int(lab) else "other"))
        val = int(lab) if lab and lab.isdigit() else (_roman_to_int(lab) if lab else None)
        if runs and runs[-1]["style"] == style and val is not None and runs[-1]["_last"] is not None and val == runs[-1]["_last"] + 1:
            runs[-1]["end_page"] = i
            runs[-1]["last_label"] = lab
            runs[-1]["_last"] = val
        else:
            runs.append({"start_page": i, "end_page": i, "style": style, "first_label": lab, "last_label": lab, "_last": val})
    for r in runs:
        r.pop("_last")
    return runs
