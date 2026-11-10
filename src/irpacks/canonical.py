"""Canonical document builder for the ``asciidoctor_html_verified_against_pdf`` route.

Inputs: the authoritative PDF and a structured candidate (Asciidoctor HTML)
registered for the same edition.  Output: ``document.json`` (hierarchy +
ordered elements + provenance), ``page-map.json``, an alignment/difference
report and issue records.  Text is accepted from the structured candidate only
where :mod:`irpacks.align` establishes a match with the PDF; otherwise the PDF
text is used, or the element is marked unverified and kept out of the
validated retrieval corpus.
"""

from __future__ import annotations

import collections
import re
from pathlib import Path
from typing import Any

from . import adochtml, align, pdfsource
from .adochtml import FN_CLOSE, FN_OPEN, XR_CLOSE, XR_OPEN, XR_SEP, md_escape
from .util import collapse_ws, sha256_text, short_hash, slugify, text_key

DOC_SCHEMA_VERSION = "1.0.0"

TYPE_CODES = {
    "part": "part", "chapter": "ch", "division": "div", "section": "s", "heading": "h", "paragraph": "p",
    "list": "l", "list_item": "i", "code": "c", "listing": "lst", "table": "t", "figure": "f", "caption": "cap",
    "admonition": "a", "sidebar": "sb", "quote": "q", "math": "m", "footnote": "fn", "block_title": "bt",
    "attribution": "at", "open_block": "ob", "example": "ex", "callout_list": "co", "dlist": "dl",
    "dlist_item": "di", "thematic_break": "hr", "unknown_block": "x", "toc_entry": "toc", "index_entry": "ix",
    "pdf_text": "pt",
}

_FN_RE = re.compile(f"{re.escape(FN_OPEN)}(.*?){re.escape(FN_CLOSE)}")
_XR_RE = re.compile(f"{re.escape(XR_OPEN)}(.*?){re.escape(XR_SEP)}(.*?){re.escape(XR_CLOSE)}")


# ---------------------------------------------------------------------------
# Identity


def assign_ids(root: dict) -> list[dict]:
    """Assign stable hierarchical IDs; return elements in document order.

    Division/part/chapter IDs are semantic (``fm-preface``, ``part2``, ``ch06``,
    ``bm-index``); nested IDs append per-type ordinals within the parent
    (``ch06.s01.p03``).  IDs are scoped to the edition: unchanged input yields
    unchanged IDs; edits upstream in the same parent may renumber siblings.
    """
    order: list[dict] = []
    top = root["children"]
    last_part = max((i for i, c in enumerate(top) if c["type"] == "part"), default=-1)

    def visit(e: dict, parent: dict | None, top_index: int | None = None) -> None:
        if parent is None:
            e["id"] = "doc"
        elif e["type"] == "chapter":
            e["id"] = f"ch{e['attrs']['number']:02d}"
        elif e["type"] == "part":
            e["id"] = f"part{e['attrs'].get('number') or slugify(e['attrs'].get('title', 'x'))}"
        elif e["type"] == "division" and parent["type"] == "document":
            title = e["attrs"].get("title") or e.get("anchor") or "division"
            prefix = "bm" if top_index is not None and top_index > last_part >= 0 else "fm"
            e["id"] = f"{prefix}-{slugify(title, 40)}"
        else:
            counters = parent.setdefault("_counters", collections.Counter())
            code = TYPE_CODES.get(e["type"], "x")
            counters[code] += 1
            e["id"] = f"{parent['id']}.{code}{counters[code]:02d}"
        e["parent_id"] = parent["id"] if parent else None
        order.append(e)
        for i, c in enumerate(e["children"]):
            visit(c, e, i if parent is None else None)

    visit(root, None)
    for e in order:
        e.pop("_counters", None)
    return order


def _prune(root: dict, keep: set[str]) -> list[dict]:
    """Keep elements in ``keep`` (with descendants), their ancestors, and ancestors' headings."""
    def contains(e) -> bool:
        return e["id"] in keep or any(contains(c) for c in e["children"])

    def prune(e, inside: bool):
        inside = inside or e["id"] in keep or (e["type"] == "open_block" and e["attrs"].get("partintro"))
        if not inside:
            e["children"] = [c for c in e["children"] if contains(c) or c["type"] == "heading" or
                             (c["type"] == "open_block" and c["attrs"].get("partintro"))]
        for c in e["children"]:
            prune(c, inside)

    prune(root, False)
    order: list[dict] = []

    def walk(e):
        order.append(e)
        for c in e["children"]:
            walk(c)

    walk(root)
    return order


def chapter_of(order: list[dict]) -> dict[str, str | None]:
    """Map element id -> enclosing chapter/division id (top-level book unit)."""
    by_id = {e["id"]: e for e in order}
    out: dict[str, str | None] = {}
    for e in order:
        cur, unit = e, None
        while cur is not None:
            if cur["type"] in ("chapter",) or (cur["type"] == "division" and by_id.get(cur["parent_id"], {}).get("type") == "document"):
                unit = cur["id"]
                break
            if cur["type"] == "part" and unit is None:
                unit = cur["id"]
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
        out[e["id"]] = unit
    return out


# ---------------------------------------------------------------------------
# Text resolution helpers


def _apply_text(s: str, values: dict[str, Any], fn_labels: dict[int, str], fn_ok: set[int]) -> str:
    def fn(m):
        n = int(m.group(1)) if m.group(1).isdigit() else None
        lab = fn_labels.get(n)
        return f"[{lab}]" if lab else f"[{m.group(1)}]"

    s = _FN_RE.sub(fn, s)
    it = iter(values.get("xr", []))

    def xr(m):
        v = next(it, None)
        return v if v else m.group(1)

    return _XR_RE.sub(xr, s)


def _apply_md(s: str, values: dict[str, Any], fn_labels: dict[int, str], fn_ids: dict[int, str],
              fn_ok: set[int]) -> str:
    def fnmd(m):
        n = int(m.group(1)) if m.group(1).isdigit() else None
        if n in fn_ok and n in fn_ids:
            return f"[^{fn_ids[n]}]"
        return f"\\[{fn_labels.get(n, m.group(1))}\\]"  # printed label without a note

    s = re.sub(r"\[\^" + re.escape(FN_OPEN) + r"(.*?)" + re.escape(FN_CLOSE) + r"\]", fnmd, s)
    it = iter(values.get("xr_md", []))

    def xr(m):
        v = next(it, None)
        return v if v is not None else m.group(1)

    return _XR_RE.sub(xr, s)


def _strip_markers(s: str) -> str:
    s = _FN_RE.sub(lambda m: f"[{m.group(1)}]", s)
    return _XR_RE.sub(lambda m: m.group(1), s)


# ---------------------------------------------------------------------------
# PDF-only regions


def _column_lines(page: dict, furniture: list[dict], columns: int) -> list[dict]:
    chars = [c for c in page["chars"] if c["text"].strip() and not any(pdfsource.char_in_bbox(c, f["bbox"]) for f in furniture)]
    if not chars:
        return []
    if columns == 1:
        return pdfsource._lines_from_chars(chars)
    mid = page["width"] / 2
    left = [c for c in chars if (c["x0"] + c["x1"]) / 2 < mid]
    right = [c for c in chars if (c["x0"] + c["x1"]) / 2 >= mid]
    lines = pdfsource._lines_from_chars(left) + pdfsource._lines_from_chars(right)
    for ln in lines:
        ln["column"] = 0 if ln["bbox"][0] < mid else 1
    return lines


_TOC_RE = re.compile(r"^(?P<title>.*?)[\s.]*\.{2,}[\s.]*(?P<page>[0-9ivxlc]+)$|^(?P<title2>.*?)\s+(?P<page2>[0-9ivxlc]+)$", re.I)
_IX_LOC = re.compile(r",\s*((?:[0-9]+|[ivxlc]+)(?:\s*,\s*(?:[0-9]+|[ivxlc]+))*)\s*$", re.I)
_IX_SEE = re.compile(r"\(see\s+(also\s+)?(?P<see>[^)]+)\)\s*$", re.I)


def extract_toc(pages: dict[int, dict], furniture, first: int, last: int) -> list[dict]:
    out = []
    xs = []
    for pno in range(first, last + 1):
        for ln in _column_lines(pages[pno], furniture.get(pno, []), 1):
            xs.append(ln["bbox"][0])
    base = min(xs) if xs else 0
    for pno in range(first, last + 1):
        p = pages[pno]
        for ln in _column_lines(p, furniture.get(pno, []), 1):
            text = ln["text"]
            m = _TOC_RE.match(text)
            title, plab = (text, None)
            if m:
                title = (m.group("title") or m.group("title2") or "").rstrip(" .")
                plab = m.group("page") or m.group("page2")
            if text.strip().lower() == "table of contents":
                kind = "heading"
            else:
                kind = "entry"
            out.append({"page": pno, "page_label": p["label"], "bbox": ln["bbox"], "text": text,
                        "title": title, "target_label": plab, "kind": kind,
                        "indent": round(ln["bbox"][0] - base, 1)})
    return out


def extract_index(pages: dict[int, dict], furniture, first: int, last: int) -> list[dict]:
    """Two-column index: entries with nesting by indentation and continuation lines."""
    raw = []
    for pno in range(first, last + 1):
        p = pages[pno]
        lines = _column_lines(p, furniture.get(pno, []), 2)
        lines.sort(key=lambda l: (l.get("column", 0), l["bbox"][1]))
        for ln in lines:
            raw.append((pno, p["label"], ln))
    # column x baselines per (page, column)
    base: dict[tuple[int, int], float] = {}
    for pno, _, ln in raw:
        k = (pno, ln.get("column", 0))
        base[k] = min(base.get(k, 1e9), ln["bbox"][0])
    entries: list[dict] = []
    stack: list[str] = []
    for pno, lab, ln in raw:
        text = ln["text"].strip()
        indent = ln["bbox"][0] - base[(pno, ln.get("column", 0))]
        if text.lower() == "index" and ln["size"] > 14:
            entries.append({"page": pno, "page_label": lab, "bbox": ln["bbox"], "text": text, "kind": "heading"})
            continue
        if len(text) <= 2 and (text.isalpha() or text == "@") and indent < 2:
            entries.append({"page": pno, "page_label": lab, "bbox": ln["bbox"], "text": text, "kind": "letter"})
            stack = []
            continue
        level = 0 if indent < 6 else (1 if indent < 20 else 2)
        # continuation of a wrapped entry: deeper indent than a sub-entry and previous entry incomplete
        if entries and entries[-1]["kind"] == "entry" and indent >= 20 and level == 2 and \
                not _IX_LOC.search(entries[-1]["text"]) and not _IX_SEE.search(entries[-1]["text"]) \
                and entries[-1]["level"] < 2:
            prev = entries[-1]
            prev["text"] = prev["text"] + " " + text
            prev["bbox"] = [min(prev["bbox"][0], ln["bbox"][0]), prev["bbox"][1], max(prev["bbox"][2], ln["bbox"][2]), ln["bbox"][3]]
            prev["continued_lines"] = prev.get("continued_lines", 1) + 1
            _parse_index_entry(prev, stack)
            continue
        e = {"page": pno, "page_label": lab, "bbox": ln["bbox"], "text": text, "kind": "entry", "level": level}
        stack = stack[:level]
        _parse_index_entry(e, stack)
        stack.append(e["term"])
        entries.append(e)
    return entries


def _parse_index_entry(e: dict, stack: list[str]) -> None:
    text = e["text"]
    see = _IX_SEE.search(text)
    e["see"] = see.group("see").strip() if see else None
    body = text[: see.start()].strip() if see else text
    m = _IX_LOC.search(body)
    e["locators"] = [x.strip() for x in m.group(1).split(",")] if m else []
    e["term"] = (body[: m.start()] if m else body).strip().rstrip(",")
    e["path"] = stack[: e.get("level", 0)] + [e["term"]]


# ---------------------------------------------------------------------------
# Main build


def build(pdf_path: str, html_path: str, recipe: dict, ids: dict, cache_dir: Path, log=print,
          scope: dict | None = None) -> dict:
    """Run extraction + alignment and return the canonical structures.

    ``scope`` (pilot runs) = {"keep": [element ids], "pages": [physical pages]}: only those
    elements are aligned against only those pages.
    """
    scope_pages = sorted(scope["pages"]) if scope else None
    cache_name = "pdf_pages.json.gz" if not scope else f"pdf_pages.scope-{short_hash(scope_pages)}.json.gz"
    data = pdfsource.extract_pages(pdf_path, cache_dir / cache_name, pages=scope_pages, log=log)
    pages = data["pages"]
    n_pages_total = pdfsource.page_count(pdf_path)
    pages_by_no = {p["page"]: p for p in pages}
    furniture = pdfsource.detect_furniture(pages)
    marks = pdfsource.bookmarks(pdf_path)
    masked: set[int] = set()
    for reg in recipe.get("pdf_only_regions", []):
        rng = _bookmark_range(marks, reg["bookmark"], n_pages_total)
        if rng:
            masked.update(range(rng[0], rng[1] + 1))
    stream = align.build_stream(pages, furniture, masked_pages=masked)
    align.set_pages(pages)
    parsed = adochtml.parse_document(html_path)
    root = parsed["root"]
    order = assign_ids(root)
    pruned_key = ""
    if scope:
        full = list(order)
        order = _prune(root, set(scope["keep"]))
        kept = {e["id"] for e in order}
        pruned_key = "".join(text_key(_strip_markers(t)) for e in full if e["id"] not in kept for t in (e.get("align") or []))
        pruned_key += "".join(text_key(_strip_markers(f["text"])) for f in parsed["footnotes"].values())
    by_id = {e["id"]: e for e in order}
    unit_of = chapter_of(order)

    # xref expected titles (anchor -> candidate renderings)
    titles: dict[str, list[str]] = collections.defaultdict(list)
    for e in order:
        a = e.get("anchor")
        if not a:
            continue
        t = e["attrs"].get("title") or (e["text"] if e["type"] == "heading" else None)
        if e["type"] in ("chapter", "section", "division", "part"):
            head = next((c for c in e["children"] if c["type"] == "heading"), None)
            if head is not None:
                t = head["attrs"].get("title") or head["text"]
                titles[a].append(collapse_ws(_strip_markers(head["text"])))
        if t:
            titles[a].append(collapse_ws(_strip_markers(t)))
        if e["attrs"].get("caption"):
            titles[a].append(e["attrs"]["caption"])
    for e in order:  # headings carry their own anchors
        if e["type"] == "heading" and e.get("anchor"):
            titles[e["anchor"]].append(e["attrs"].get("title") or e["text"])

    # ---- body alignment (headings of divisions whose content is a masked PDF-only region, e.g. the index,
    #      take their locator from the PDF-only extraction instead)
    pdf_only_heads = set()
    for reg in recipe.get("pdf_only_regions", []):
        tgt = by_id.get(reg.get("attach_to") or "")
        if tgt is not None:
            pdf_only_heads.update(c["id"] for c in tgt["children"] if c["type"] == "heading")
    units: list[align.Unit] = []
    for e in order:
        if e["align"] and e["id"] not in pdf_only_heads:
            units.extend(align.make_units(e["id"], e["align"]))
    align.align_units(stream, units, titles)

    # ---- footnotes: labels from resolved references; definitions scoped to their chapter
    fn_labels: dict[int, str] = {}
    fn_chapter: dict[int, str] = {}
    for u in units:
        for p in u.parts:
            if p.kind == "fn":
                n = int(p.text)
                fn_chapter.setdefault(n, unit_of.get(u.element_id))
                if p.resolved and n not in fn_labels:
                    fn_labels[n] = p.resolved
    chap_range: dict[str, list[int]] = {}
    for u in units:
        if u.start is None:
            continue
        cid = unit_of.get(u.element_id)
        r = chap_range.setdefault(cid, [u.start, u.end])
        r[0], r[1] = min(r[0], u.start), max(r[1], u.end)
    starts_sorted = sorted(v[0] for v in chap_range.values())
    referenced = set(fn_chapter)
    footnotes = {n: f for n, f in parsed["footnotes"].items() if n in referenced}
    fn_units = []
    for n, fn in sorted(footnotes.items()):
        cid = fn_chapter.get(n)
        lo = chap_range.get(cid, [0, 0])[0]
        nxt = [s for s in starts_sorted if s > lo]
        hi = nxt[0] if nxt else len(stream.key)
        lab = fn_labels.get(n)
        txt = (f"[{lab}] " if lab else "") + fn["text"]
        fn_units.extend(align.make_units(f"fn:{n}", [txt], group="footnote", scope=(lo, hi)))
    align.align_scoped(stream, fn_units, titles)
    fn_unit_by_n = {int(u.element_id.split(":")[1]): u for u in fn_units}
    fn_ok = {n for n, u in fn_unit_by_n.items() if u.status == "exact"}

    # ---- footnote elements (placed at the end of their chapter, as printed)
    fn_ids: dict[int, str] = {}
    fn_elems: dict[str, list[dict]] = collections.defaultdict(list)
    for n, fn in sorted(footnotes.items()):
        cid = fn_chapter.get(n) or "doc"
        lab = fn_labels.get(n)
        fid = f"{cid}.fn{lab.zfill(2) if lab else 'x' + str(n)}"
        fn_ids[n] = fid.replace(".", "-")
        u = fn_unit_by_n.get(n)
        el = {"type": "footnote", "anchor": f"fn-{fid.replace('.', '-')}", "text": fn["text"], "md": fn["md"],
              "children": [], "attrs": {"label": lab, "html_number": n, "chapter_id": cid,
                                        "links": fn["links"], "xrefs": fn["xrefs"]},
              "align": [], "id": fid, "parent_id": cid, "_units": [u] if u else []}
        fn_elems[cid].append(el)

    # ---- resolve element text from units
    units_by_el: dict[str, list[align.Unit]] = collections.defaultdict(list)
    for u in units:
        units_by_el[u.element_id].append(u)
    for e in order:
        e["_units"] = units_by_el.get(e["id"], [])
    for cid, els in fn_elems.items():
        for el in els:
            by_id[el["id"]] = el

    issues: list[dict] = []

    def issue(sev, kind, loc, msg, **kw):
        issues.append({"severity": sev, "kind": kind, "location": loc, "message": msg, **kw})

    all_elems = order + [el for els in fn_elems.values() for el in els]
    for e in all_elems:
        us = e.get("_units") or []
        if not us:
            continue
        e["_resolution"] = _resolve_element(e, us, stream, pages_by_no, fn_labels, fn_ids, fn_ok, issue)

    # footnote text: strip the leading "[label] " that was added for alignment
    for els in fn_elems.values():
        for el in els:
            lab = el["attrs"]["label"]
            if lab and el["text"].startswith(f"[{lab}] "):
                el["text"] = el["text"][len(lab) + 3:]
            el["md"] = re.sub(r"^\\\[\d+\\\]\s*", "", el["md"])
            if el["attrs"]["html_number"] not in fn_ok:
                issue("major", "footnote_not_in_pdf", el["id"],
                      f"Footnote text present in the HTML candidate (#{el['attrs']['html_number']}) was not found in the PDF; "
                      "excluded from the edition text and recorded as an annotation.", html_text=el["text"][:300])
                el["_excluded"] = True

    # ---- hyperlink targets: verify structured-candidate hrefs against PDF link annotations
    _verify_links(all_elems, pages_by_no)

    # ---- figures and math: match to PDF image objects
    figures = [e for e in order if e["type"] in ("figure", "math")]
    _match_images(figures, by_id, pages_by_no, issue)

    # ---- coverage gaps
    covered = align.covered_ranges(units + fn_units)
    gaps = align.coverage(stream, covered)
    gap_records = _classify_gaps(gaps, stream, pages_by_no, order, recipe, marks, furniture, n_pages_total,
                                 pruned_key=pruned_key)

    # ---- pair unverified leaf elements with unclassified PDF gaps between their neighbours
    _pair_gaps(order, units, gap_records, stream, pages_by_no, issue)
    # superseded records: an element resolved by gap pairing, or a footnote reported as HTML-only, keeps
    # only its final issue
    resolved = {i["location"] for i in issues if i["kind"] in ("text_differs_from_structured_candidate",
                                                                "footnote_not_in_pdf")}
    issues[:] = [i for i in issues if not (i["kind"] == "unverified_structured_text" and i["location"] in resolved)]

    # ---- PDF-only regions
    pdf_only = _pdf_only_regions(recipe, marks, pages_by_no, furniture, n_pages_total)

    result = {
        "parsed": parsed, "root": root, "order": order, "by_id": by_id, "units": units, "fn_units": fn_units,
        "fn_elems": fn_elems, "fn_labels": fn_labels, "fn_ids": fn_ids, "fn_ok": fn_ok, "stream": stream,
        "pages": pages, "pages_by_no": pages_by_no, "furniture": furniture, "bookmarks": marks,
        "gaps": gap_records, "pdf_only": pdf_only, "issues": issues, "pdf_extract_elapsed_s": data["elapsed_s"],
        "fonts": data["fonts"], "unit_of": unit_of, "from_cache": data.get("from_cache"),
        "n_pages_total": n_pages_total, "scope": scope,
    }
    return result


def _resolve_element(e, us, stream, pages_by_no, fn_labels, fn_ids, fn_ok, issue) -> dict:
    """Set e['text'] / e['md'] (and table cells) from aligned units."""
    statuses = [u.status for u in us]
    spans: list[dict] = []
    for u in us:
        if u.start is not None and u.status in ("exact", "fuzzy"):
            spans.extend(align.unit_spans(stream, pages_by_no, u.start, u.end))
    # merge spans per page
    merged: dict[int, dict] = {}
    for s in spans:
        m = merged.get(s["page"])
        if m is None:
            merged[s["page"]] = dict(s)
        else:
            m["bbox"] = [min(m["bbox"][0], s["bbox"][0]), min(m["bbox"][1], s["bbox"][1]),
                         max(m["bbox"][2], s["bbox"][2]), max(m["bbox"][3], s["bbox"][3])]
            m["n_key_chars"] += s["n_key_chars"]
    e["_spans"] = [merged[k] for k in sorted(merged)]
    xr_vals, xr_md_vals, var_records = [], [], []
    for u in us:
        for p in u.parts:
            if p.kind == "xr":
                if p.resolution in ("pdf_title", "pdf_region") and p.resolved:
                    xr_vals.append(p.resolved)
                    xr_md_vals.append(md_escape(p.resolved))
                else:
                    xr_vals.append(None)
                    xr_md_vals.append(None)
                var_records.append({"kind": "xref", "target": p.expected[0] if p.expected else None,
                                    "html_text": p.text, "pdf_text": p.resolved, "resolution": p.resolution})
            elif p.kind == "fn":
                var_records.append({"kind": "footnote_ref", "html_number": int(p.text), "pdf_label": p.resolved,
                                    "resolution": p.resolution})
    vals = {"xr": xr_vals, "xr_md": xr_md_vals}
    method = "html_structured_pdf_verified"
    pdf_fallback = None
    if any(s == "fuzzy" for s in statuses):
        method = "pdf_text_fallback"
    elif any(s in ("unmatched",) for s in statuses):
        method = "unverified"
    if e["type"] == "table":
        row_units = us[1:] if e["attrs"].get("caption") is not None else us
        if len(row_units) != len(e["attrs"]["rows"]):
            issue("major", "table_unit_mismatch", e["id"], "Row units do not correspond to table rows.")
        for row, u in zip(e["attrs"]["rows"], row_units):
            uv = [(p.resolved if p.resolution in ("pdf_title", "pdf_region") else None) for p in u.parts if p.kind == "xr"]
            it_t, it_m = iter(uv), iter(md_escape(v) if v else None for v in uv)
            for c in row["cells"]:
                n_x = len(_XR_RE.findall(c["text"]))
                cv = {"xr": [next(it_t, None) for _ in range(n_x)]}
                cmv = {"xr_md": [next(it_m, None) for _ in range(n_x)]}
                c["text"] = _apply_text(c["text"], cv, fn_labels, fn_ok)
                c["md"] = _apply_md(c["md"], cmv, fn_labels, fn_ids, fn_ok)
            row["source_spans"] = [{"locator_type": "pdf_page", "page": sp["page"], "page_label": sp["page_label"],
                                    "bbox": sp["bbox"], "coords": pdfsource.COORDS, "role": "text",
                                    "n_key_chars": sp["n_key_chars"], "mapping_status": "verified",
                                    "native_locator": None}
                                   for sp in align.unit_spans(stream, pages_by_no, u.start, u.end)] \
                if u.start is not None else []
            if u.status == "fuzzy":
                row["pdf_text"] = align.pdf_text_between(stream, pages_by_no, u.start, u.end)
                row["verification"] = "pdf_differs"
                issue("minor", "table_row_differs", e["id"], "Table row text differs between HTML candidate and PDF; "
                      "PDF text recorded on the row.", pdf_text=row["pdf_text"][:200])
            elif u.status == "unmatched":
                row["verification"] = "unverified"
                issue("major", "table_row_unmatched", e["id"], "Table row not located in the PDF.",
                      html_text=" | ".join(c["text"] for c in row["cells"])[:200])
            else:
                row["verification"] = "verified"
        e["text"] = _apply_text(e["text"], {"xr": []}, fn_labels, fn_ok)
        e["text"] = _strip_markers(e["text"])
        if method == "pdf_text_fallback":
            method = "html_structured_pdf_verified_with_row_differences"
    elif method == "html_structured_pdf_verified":
        e["text"] = _apply_text(e["text"], vals, fn_labels, fn_ok)
        e["md"] = _apply_md(e["md"], vals, fn_labels, fn_ids, fn_ok) if e.get("md") else e.get("md", "")
    elif method == "pdf_text_fallback":
        u = us[0]
        pdf_fallback = align.pdf_text_between(stream, pages_by_no, u.start, u.end)
        # keep trailing punctuation that the key span excludes
        html_plain = _strip_markers(e["text"])
        tail = re.search(r"[^\w\s]+$", html_plain)
        if tail and not pdf_fallback.endswith(tail.group(0)):
            pdf_fallback += tail.group(0) if tail.group(0) in (".", ".)", ")", ":", ";", "?") else ""
        issue("major" if e["type"] != "code" else "critical", "text_differs_from_structured_candidate", e["id"],
              "Structured candidate text differs from the PDF beyond typography; PDF text used.",
              html_text=html_plain[:300], pdf_text=pdf_fallback[:300], ratio=us[0].ratio)
        e["attrs"]["structured_candidate_text"] = html_plain
        e["text"] = pdf_fallback
        e["md"] = md_escape(pdf_fallback) if e["type"] != "code" else ""
    else:  # unverified
        e["text"] = _strip_markers(e["text"])
        e["md"] = _strip_markers(e.get("md") or "")
        issue("major", "unverified_structured_text", e["id"],
              "Structured candidate text not located in the PDF; kept only as unverified and excluded "
              "from validated retrieval.", html_text=e["text"][:300])
    for u in us:
        if u.status == "exact":
            for p in u.parts:
                if p.kind != "lit" and p.resolution == "unresolved":
                    issue("minor", "unresolved_inline_variable", e["id"],
                          f"Inline {p.kind} '{p.text}' rendering could not be determined from the PDF; HTML text kept.")
    vis_diffs = [d for u in us for d in (u.visible_diff or [])]
    return {
        "method": method,
        "unit_statuses": statuses,
        "ratio": min((u.ratio for u in us if u.ratio is not None), default=None),
        "visible_equal": all(u.visible_equal for u in us if u.status in ("exact", "fuzzy")) if any(
            u.status in ("exact", "fuzzy") for u in us) else None,
        "visible_differences": _classify_visible(vis_diffs, e["type"]),
        "variables": var_records,
        "pdf_fallback": pdf_fallback is not None,
    }


def _classify_visible(diffs: list, owner: str = "") -> list[dict]:
    out = []
    for tag, a, b, ctx in diffs:
        if tag == "replace" and a == "." and b == "|":
            cls = "caption_separator"
        elif (a in ("(", ")") and b == "") or (tag == "replace" and a == ")" and b.isdigit()):
            cls = "callout_marker_style"
        elif tag == "insert" and b in ("•", "▪", "◦", "-") or (tag == "insert" and re.fullmatch(r"\d+\.", b or "")):
            cls = "list_marker"
        elif tag == "insert" and b == "," and (owner == "attribution" or "—" in ctx or "-" in ctx[:3]):
            cls = "attribution_style"
        elif tag == "delete" and a == "." and re.search(r"\d\.", ctx):
            cls = "heading_number_style"
        else:
            cls = "other"
        out.append({"class": cls, "op": tag, "structured": a, "pdf": b, "context": ctx})
    return out


def _match_images(figures: list[dict], by_id: dict, pages: dict[int, dict], issue) -> None:
    used: set[tuple[int, int]] = set()
    # candidate pages from neighbours: caption span, else previous/next placed element
    order_ids = list(by_id.keys())
    pos = {k: i for i, k in enumerate(order_ids)}

    def near_page(e) -> int | None:
        cap = next((c for c in e["children"] if c["type"] == "caption"), None)
        if cap is not None and cap.get("_spans"):
            return cap["_spans"][0]["page"]
        i = pos[e["id"]]
        for j in range(i - 1, -1, -1):
            sp = by_id[order_ids[j]].get("_spans")
            if sp:
                return sp[-1]["page"]
        for j in range(i + 1, len(order_ids)):  # nothing placed before: use the next placed element
            sp = by_id[order_ids[j]].get("_spans")
            if sp:
                return sp[0]["page"]
        return None

    for e in figures:
        pg = near_page(e)
        want = e["attrs"].get("pixel_size")
        best = None
        if pg is not None:
            for pno in (pg, pg - 1, pg + 1, pg - 2):
                if pno not in pages:
                    continue
                for k, im in enumerate(pages[pno]["images"]):
                    if (pno, k) in used:
                        continue
                    dims_ok = bool(want) and list(im["srcsize"]) == list(want)
                    score = (0 if dims_ok else 1, abs(pno - pg))
                    if e["type"] == "math":
                        score = (0, abs(pno - pg))
                    if best is None or score < best[0]:
                        best = (score, pno, k, im, dims_ok)
        if best is None:
            e["attrs"]["pdf_image"] = None
            issue("major", "figure_without_pdf_image", e["id"], "No PDF image object matched this figure/formula.")
            continue
        _, pno, k, im, dims_ok = best
        if e["type"] == "figure" and not dims_ok:
            # accept only if nothing better: record uncertainty
            issue("minor", "figure_dims_mismatch", e["id"],
                  f"Matched PDF image on page {pno} but pixel dimensions differ "
                  f"(HTML {want}, PDF {im['srcsize']}).")
        used.add((pno, k))
        e["attrs"]["pdf_image"] = {"page": pno, "index_on_page": k, "bbox": im["bbox"], "srcsize": im["srcsize"],
                                   "raw_sha256": im["raw_sha256"], "dims_match": dims_ok}
        e.setdefault("_spans", [])
        e["_image_span"] = {"page": pno, "page_label": pages[pno]["label"], "bbox": im["bbox"], "n_key_chars": 0,
                            "kind": "image"}
    # unused images reported
    all_imgs = {(p["page"], k) for p in pages.values() for k in range(len(p["images"]))}
    for pno, k in sorted(all_imgs - used):
        im = pages[pno]["images"][k]
        issue("info" if pno == 1 else "minor", "pdf_image_unmatched", f"page:{pno}",
              f"PDF image object {k} on page {pno} (srcsize {im['srcsize']}) not matched to a structured element.")


def _mostly_in(txt: str, hay: str, n: int = 30, frac: float = 0.7) -> bool:
    """True if >= frac of the overlapping n-char shingles of txt occur in hay (pilot out-of-scope test)."""
    if len(txt) <= n:
        return txt in hay
    shingles = [txt[i:i + n] for i in range(0, len(txt) - n + 1, n // 2)]
    return sum(1 for sh in shingles if sh in hay) >= frac * len(shingles)


def _norm_uri(u: str) -> str:
    u = u.strip()
    u = re.sub(r"^https?://", "", u)
    return u.rstrip("/")


def _verify_links(elems: list[dict], pages_by_no: dict[int, dict]) -> None:
    """Mark each external link as found / not found among the PDF's URI annotations on the element's pages."""
    for e in elems:
        links = e["attrs"].get("links")
        if not links:
            continue
        pages = {s["page"] for s in (e.get("_spans") or [])}
        uris = {_norm_uri(l["uri"]) for p in pages for l in pages_by_no.get(p, {}).get("links", [])}
        for l in links:
            l["pdf_link_annotation"] = ("matched" if _norm_uri(l["href"]) in uris else "not_found") if pages else "unlocated"


def _pair_gaps(order, units, gaps, stream, pages_by_no, issue) -> None:
    placed = [u for u in units if u.start is not None and u.status in ("exact", "fuzzy")]
    by_el: dict[str, list] = collections.defaultdict(list)
    for u in units:
        by_el[u.element_id].append(u)
    seq = [u for u in units]
    pos = {u.uid: i for i, u in enumerate(seq)}
    for e in order:
        res = e.get("_resolution")
        if not res or res["method"] != "unverified" or e["type"] == "table":
            continue
        us = by_el[e["id"]]
        i0, i1 = pos[us[0].uid], pos[us[-1].uid]
        prev = next((seq[k] for k in range(i0 - 1, -1, -1) if seq[k].end is not None and seq[k].status in ("exact", "fuzzy")), None)
        nxt = next((seq[k] for k in range(i1 + 1, len(seq)) if seq[k].start is not None and seq[k].status in ("exact", "fuzzy")), None)
        lo = prev.end if prev else 0
        hi = nxt.start if nxt else len(stream.key)
        cands = [g for g in gaps if g["class"] == "unclassified" and g["key_start"] >= lo and g["key_end"] <= hi]
        if len(cands) != 1:
            continue
        g = cands[0]
        txt = align.pdf_text_between(stream, pages_by_no, g["key_start"], g["key_end"])
        html_plain = e["text"]
        tail = re.search(r"[.:;)]$", html_plain)
        if tail and not txt.endswith(tail.group(0)):
            txt += tail.group(0)
        e["attrs"]["structured_candidate_text"] = html_plain
        e["text"] = txt
        e["md"] = md_escape(txt)
        e["_spans"] = align.unit_spans(stream, pages_by_no, g["key_start"], g["key_end"])
        res["method"] = "pdf_text_fallback"
        res["pdf_fallback"] = True
        res["paired_gap"] = [g["key_start"], g["key_end"]]
        g["class"] = "paired_with_element"
        g["element_id"] = e["id"]
        for it in issues_for(e["id"]):
            pass
        issue("major", "text_differs_from_structured_candidate", e["id"],
              "Structured candidate text not found; the PDF text located between its neighbours is used.",
              html_text=html_plain[:300], pdf_text=txt[:300])


def issues_for(_eid):
    return []


def _bookmark_range(marks: list[dict], title: str, n_pages: int) -> tuple[int, int] | None:
    for i, m in enumerate(marks):
        if m["title"].strip().lower() == title.strip().lower() and m["page"]:
            nxt = next((x["page"] for x in marks[i + 1:] if x["page"] and x["page"] > m["page"]), n_pages + 1)
            return m["page"], nxt - 1
    return None


def _pdf_only_regions(recipe: dict, marks, pages: dict[int, dict], furniture, n_total: int) -> dict:
    out = {}
    for reg in recipe.get("pdf_only_regions", []):
        rng = _bookmark_range(marks, reg["bookmark"], n_total)
        if not rng or any(p not in pages for p in range(rng[0], rng[1] + 1)):
            out[reg["kind"]] = {"range": None, "entries": []}
            continue
        if reg["kind"] == "toc":
            out["toc"] = {"range": list(rng), "entries": extract_toc(pages, furniture, *rng), **reg}
        elif reg["kind"] == "index":
            out["index"] = {"range": list(rng), "entries": extract_index(pages, furniture, *rng), **reg}
    return out


def _classify_gaps(gaps, stream, pages_by_no, order, recipe, marks, furniture, n_total, pruned_key: str = "") -> list[dict]:
    regions = {}
    for reg in recipe.get("pdf_only_regions", []):
        rng = _bookmark_range(marks, reg["bookmark"], n_total)
        if rng:
            regions[reg["kind"]] = rng
    header_keys: dict[str, str] = {}
    for e in order:
        if e["type"] == "table":
            for r in e["attrs"]["rows"]:
                if r["section"] == "thead":
                    header_keys[text_key(" ".join(c["text"] for c in r["cells"]))] = e["id"]
    out = []
    for a, b in gaps:
        page = stream.key_src[a][0]
        txt = stream.key[a:b]
        rec = {"key_start": a, "key_end": b, "page": page, "page_label": pages_by_no[page]["label"],
               "n_key_chars": b - a}
        if any(lo <= page <= hi for kind, (lo, hi) in regions.items()):
            kind = next(k for k, (lo, hi) in regions.items() if lo <= page <= hi)
            rec["class"] = f"pdf_only_{kind}"
        elif txt in header_keys:
            rec["class"] = "repeated_table_header"
            rec["table_id"] = header_keys[txt]
        elif b - a <= 3 and (txt.isdigit() or (len(txt) == 1 and txt.isalpha())):
            rec["class"] = "list_or_callout_marker"
        elif pruned_key and len(txt) >= 4 and _mostly_in(txt, pruned_key):
            rec["class"] = "outside_pilot_scope"
        else:
            rec["class"] = "unclassified"
        if rec["class"] in ("unclassified", "repeated_table_header") or b - a > 3:
            rec["text"] = align.pdf_text_between(stream, pages_by_no, a, b)[:500]
        out.append(rec)
    return out
