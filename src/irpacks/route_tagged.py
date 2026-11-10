"""Route: tagged-PDF structure tree (e.g. NIST SP 800-61r3, produced by Word/Acrobat).

The PDF's logical structure (H1/H2/P/L/LI/Table/TR/TH/TD/Figure/Caption/TOC)
provides hierarchy and reading order; text comes from the PDF text layer via
marked-content IDs.  Untagged content (running headers/footers) is furniture.
Page-split table rows produced by the tagging tool are repaired geometrically
and the repair is recorded on the row.

Two hierarchy modes (recipe ``structure``):
* default (NIST SP 800-61r3): a flat Sect of H1/H2 headings, numbered sections, CSF row locators;
* ``outline`` (e.g. the CISA playbooks and #StopRansomware Guide, Word-tagged): containers (Part/Sect/Div/...)
  are walked into, and the PDF outline (bookmarks) gives the hierarchy. A block opens a section when its text
  matches the next outline entry on the entry's page, whether it is tagged H1-H6 or, as Word often tags it, P.
  Tagged headings that are not in the outline stay in place as discrete headings.
"""

from __future__ import annotations

import collections
import re
import time
from pathlib import Path

from . import pdfsource
from .adochtml import md_escape
from .util import read_json, sha256_text, slugify, text_key, write_json

COORDS = pdfsource.COORDS
_LABEL_RE = re.compile(r"^(?:[ivxlcdm]+|\d+)$", re.I)
_CSF_ID = re.compile(r"^([A-Z]{2})(?:\.([A-Z]{2})(?:-(\d{2}))?)?\b")
_HNUM = re.compile(r"^(\d+(?:\.\d+)*)\.\s+(.*)$")
# list-label glyphs; "\uf0b7"/"\uf0a7" are the Symbol/Wingdings bullets Word emits in its private-use area
_HARD_BULLETS = {"•", "\uf0b7", "\uf0a7", "\uf0d8", "\uf076"}
_BULLETS = {"o", "▪", "◦", "§", "-", "–"} | _HARD_BULLETS


def _r(v):
    return round(float(v), 2)


def _extract(pdf_path: str, cache: Path, log=print) -> dict:
    if cache.exists():
        d = read_json(cache)
        if d.get("cache_version") == 4:
            d["from_cache"] = True
            return d
    import pdfplumber
    t0 = time.time()
    out = {"cache_version": 4, "pages": [], "tree": None}
    with pdfplumber.open(pdf_path) as pdf:
        for pg in pdf.pages:
            chars = [{"text": c["text"], "x0": _r(c["x0"]), "top": _r(c["top"]), "x1": _r(c["x1"]),
                      "bottom": _r(c["bottom"]), "size": _r(c.get("size", 0)), "mcid": c.get("mcid"),
                      "font": c.get("fontname", "")} for c in pg.chars]
            images = [{"bbox": [_r(i["x0"]), _r(i["top"]), _r(i["x1"]), _r(i["bottom"])], "srcsize": list(i.get("srcsize") or []),
                       "mcid": i.get("mcid")} for i in pg.images]
            links = [{"uri": h.get("uri"), "bbox": [_r(h["x0"]), _r(h["top"]), _r(h["x1"]), _r(h["bottom"])]}
                     for h in (pg.hyperlinks or []) if h.get("uri")]
            out["pages"].append({"page": pg.page_number, "width": _r(pg.width), "height": _r(pg.height),
                                 "rotation": int(pg.rotation or 0), "chars": chars, "images": images, "links": links,
                                 "n_rects": len(pg.rects), "n_curves": len(pg.curves), "n_lines": len(pg.lines)})

        out["tree"] = _struct_tree(pdf)
    out["elapsed_s"] = round(time.time() - t0, 1)
    write_json(cache, out)
    out["from_cache"] = False
    return out


def _decode(v) -> str | None:
    from pdfminer.utils import decode_text
    if v is None:
        return None
    if isinstance(v, bytes):
        return decode_text(v).replace("\x00", "").strip()
    return str(v)


def _struct_tree(pdf) -> list[dict]:
    """Walk /StructTreeRoot keeping each marked-content reference's own page.

    Returns nodes ``{"type", "mcrefs": [[page, mcid], ...], "children", "alt_text", "attributes"}``.
    pdfplumber's tree assigns one page per element, which mis-resolves MCIDs of
    elements that continue on the next page; this walker uses the /Pg of each
    MCR (falling back to the nearest ancestor's /Pg).
    """
    from pdfminer.pdftypes import resolve1, PDFObjRef
    from pdfminer.psparser import PSLiteral

    doc = pdf.doc
    page_no = {p.page_obj.pageid: p.page_number for p in pdf.pages}
    root = resolve1(doc.catalog.get("StructTreeRoot"))
    if not root:
        return []
    role_map = {k: resolve1(v) for k, v in (resolve1(root.get("RoleMap")) or {}).items()}

    def name(v):
        v = resolve1(v)
        return v.name.decode() if isinstance(v, PSLiteral) and isinstance(v.name, bytes) else (
            v.name if isinstance(v, PSLiteral) else str(v))

    def pg_of(obj, inherited):
        pg = obj.get("Pg") if isinstance(obj, dict) else None
        if isinstance(pg, PDFObjRef):
            return page_no.get(pg.objid, inherited)
        return inherited

    def attrs_of(obj):
        a = resolve1(obj.get("A")) if isinstance(obj, dict) else None
        items = a if isinstance(a, list) else ([a] if a else [])
        out = {}
        for it in items:
            it = resolve1(it)
            if isinstance(it, dict):
                for k, v in it.items():
                    v = resolve1(v)
                    out[k] = name(v) if isinstance(v, PSLiteral) else (v if isinstance(v, (int, float, str)) else str(v))
        return out

    def walk(obj, inherited_pg, depth=0):
        obj = resolve1(obj)
        if not isinstance(obj, dict) or depth > 60:
            return None
        tag = name(obj.get("S")) if obj.get("S") is not None else ""
        if tag in role_map:
            tag = name(role_map[tag])
        pg = pg_of(obj, inherited_pg)
        node = {"type": tag, "mcrefs": [], "children": [], "kids": [], "alt_text": _decode(resolve1(obj.get("Alt"))),
                "attributes": attrs_of(obj)}
        k = resolve1(obj.get("K"))
        kids = k if isinstance(k, list) else ([] if k is None else [k])
        for kid in kids:
            kid_r = resolve1(kid)
            if isinstance(kid_r, int):
                node["mcrefs"].append([pg, kid_r])
                node["kids"].append({"mc": [pg, kid_r]})
            elif isinstance(kid_r, dict):
                t = name(kid_r.get("Type")) if kid_r.get("Type") is not None else ""
                if t == "MCR" or ("MCID" in kid_r and "S" not in kid_r):
                    ref = [pg_of(kid_r, pg), resolve1(kid_r.get("MCID"))]
                    node["mcrefs"].append(ref)
                    node["kids"].append({"mc": ref})
                elif t == "OBJR":
                    continue
                else:
                    ch = walk(kid_r, pg, depth + 1)
                    if ch is not None:
                        node["children"].append(ch)
                        node["kids"].append({"node": len(node["children"]) - 1})
        return node

    k = resolve1(root.get("K"))
    kids = k if isinstance(k, list) else [k]
    return [n for n in (walk(x, None) for x in kids) if n is not None]


def _printed_label(p: dict, pattern: re.Pattern | None = None) -> str | None:
    """Printed page number from untagged footer text (bottom 8% of the page). With ``pattern`` (recipe
    ``printed_labels.pattern``), group 1 of its first match on a footer line is the label (e.g. "Page | 7")."""
    h = p["height"]
    foot = [c for c in p["chars"] if c["mcid"] is None and c["top"] > h * 0.92 and c["text"].strip()]
    lines = pdfsource._lines_from_chars(foot)
    for ln in sorted(lines, key=lambda l: -l["bbox"][1]):
        t = ln["text"].strip()
        if pattern is not None:
            m = pattern.search(t)
            if m:
                return m.group(1)
        elif _LABEL_RE.match(t):
            return t
    return None


class _Text:
    def __init__(self, pages: dict[int, dict]):
        self.pages = pages
        self.by_mcid: dict[tuple[int, int], list[dict]] = collections.defaultdict(list)
        for p in pages.values():
            for c in p["chars"]:
                if c["mcid"] is not None:
                    self.by_mcid[(p["page"], c["mcid"])].append(c)
        self.used: set[tuple[int, int]] = set()
        self.last_collected: list[tuple[int, int]] = []
        self.hyphen_joins = 0
        self.split_items = 0

    def collect(self, node) -> list[tuple[int, dict]]:
        """Characters of a structure node in its /K order (own content interleaved with children)."""
        out = []
        kids = node.get("kids")
        if kids is None:  # synthetic node: own refs, then children
            kids = [{"mc": r} for r in node.get("mcrefs", [])] + [{"node": i} for i in range(len(node.get("children", [])))]
        for k in kids:
            if "mc" in k:
                pg, m = k["mc"]
                if pg is None:
                    continue
                self.used.add((pg, m))
                self.last_collected.append((pg, m))
                out += [(pg, c) for c in self.by_mcid.get((pg, m), [])]
            else:
                out += self.collect(node["children"][k["node"]])
        return out

    def text(self, chs: list[tuple[int, dict]]) -> str:
        s = []
        prev = None
        for pno, c in chs:
            if prev is not None:
                pp, pc = prev
                newline = pno != pp or c["top"] > pc["bottom"] - 1
                if newline:
                    tail = "".join(s).rstrip()
                    if tail.endswith("-") and len(tail) > 1 and tail[-2].isalpha() and c["text"][:1].isalpha():
                        s = [tail]  # line ends in a real hyphen: keep it, no space
                        self.hyphen_joins += 1
                    elif not "".join(s[-1:]).endswith(" "):
                        s.append(" ")
                elif c["x0"] - pc["x1"] > 0.25 * max(c["size"], 1) and not "".join(s[-1:]).endswith(" ") and c["text"] != " ":
                    s.append(" ")
            s.append(c["text"])
            prev = (pno, c)
        return re.sub(r"[ \t]+", " ", "".join(s)).strip()

    @staticmethod
    def spans(chs, labels) -> list[dict]:
        by = collections.defaultdict(list)
        for pno, c in chs:
            if c["text"].strip():
                by[pno].append(c)
        out = []
        for pno in sorted(by):
            cs = by[pno]
            out.append({"locator_type": "pdf_page", "page": pno, "page_label": labels.get(pno),
                        "bbox": [min(c["x0"] for c in cs), min(c["top"] for c in cs), max(c["x1"] for c in cs),
                                 max(c["bottom"] for c in cs)],
                        "coords": COORDS, "role": "text", "n_key_chars": sum(1 for c in cs if c["text"].isalnum()),
                        "mapping_status": "native_structure"})
        return out


def run(pdf_path: str, recipe: dict, meta: dict, cache_dir: Path, *, scope: str | None = None, log=print) -> dict:
    t0 = time.time()
    ex = _extract(pdf_path, cache_dir / "tagged.json.gz", log=log)
    pages = {p["page"]: p for p in ex["pages"]}
    outline = recipe.get("structure") == "outline"
    label_pat = re.compile(recipe["printed_labels"]["pattern"]) if recipe.get("printed_labels") else None
    csf = recipe.get("native_locators", None if outline else "csf2") == "csf2"
    labels = {pno: _printed_label(p, label_pat) for pno, p in pages.items()}
    furniture_pats = [re.compile(x) for x in recipe.get("furniture_patterns", [])]
    T = _Text(pages)
    marks = pdfsource.bookmarks(pdf_path)
    issues: list[dict] = []
    conv = collections.Counter()

    def issue(sev, kind, loc, msg, **kw):
        issues.append({"severity": sev, "kind": kind, "location": loc, "message": msg, **kw})

    # ---- element factory (both hierarchy modes)
    root = {"id": "doc", "type": "document", "children": [], "attrs": {}, "text": "", "md": "", "anchor": None,
            "source_spans": [], "mapping_status": "aggregate"}
    els: list[dict] = [root]
    counters: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    mcid_owner: dict[tuple[int, int], str] = {}

    def new(parent: dict, type_: str, code: str, mcids: list | None = None, **kw) -> dict:
        counters[parent["id"]][code] += 1
        e = {"id": f"{parent['id']}.{code}{counters[parent['id']][code]:02d}", "type": type_, "children": [],
             "attrs": {}, "text": "", "md": "", "anchor": None, "source_spans": [], "mapping_status": "native_structure"}
        e.update(kw)
        e["parent_id"] = parent["id"]
        parent["children"].append(e)
        els.append(e)
        for pm in (mcids if mcids is not None else T.last_collected):  # marked content owned by this element
            mcid_owner.setdefault(pm, e["id"])
        T.last_collected = []
        return e

    def top(id_, type_, title, anchor, number=None):
        e = {"id": id_, "type": type_, "children": [], "attrs": {"title": title, "number": number, "level": 1},
             "text": "", "md": "", "anchor": anchor, "source_spans": [], "mapping_status": "aggregate", "parent_id": "doc"}
        root["children"].append(e)
        els.append(e)
        return e

    bookmark_roles: dict[int, str] = {}
    if outline:
        bookmark_roles = _outline_build(ex["tree"], T, labels, marks, new, top, issue, conv, csf,
                                        hidden=recipe.get("exclude_hidden_text", []))
    else:  # NIST SP 800-61r3: Document > Sect > flat blocks; H1 units, H2 numbered sections
        # Document > Sect > blocks (NIST SP 800-61r3); fall back to the first level with block children
        sect = ex["tree"]
        while sect and len(sect) == 1 and sect[0]["type"] in ("Document", "Sect", "Part", "Art", ""):
            sect = sect[0]["children"]
        # ---- build a block list from the flat Sect children
        blocks = []
        for n in sect:
            t = n["type"]
            T.last_collected = []
            if t in ("H1", "H2", "H3"):
                chs = T.collect(n)
                blocks.append({"kind": "heading", "level": int(t[1]), "text": T.text(chs), "spans": T.spans(chs, labels), "mcids": list(T.last_collected)})
            elif t == "P":
                chs = T.collect(n)
                txt = T.text(chs)
                if txt:
                    blocks.append({"kind": "paragraph", "text": txt, "spans": T.spans(chs, labels), "chars": chs, "mcids": list(T.last_collected)})
            elif t == "L":
                blocks.append({"kind": "list", "node": n})
            elif t == "Table":
                blocks.append({"kind": "table", "node": n})
            elif t == "Caption":
                chs = T.collect(n)
                blocks.append({"kind": "caption", "text": T.text(chs), "spans": T.spans(chs, labels), "mcids": list(T.last_collected)})
            elif t == "Figure":
                blocks.append({"kind": "figure", "node": n})
            elif t == "TOC":
                blocks.append({"kind": "toc", "node": n})
            else:
                chs = T.collect(n)
                if chs:
                    blocks.append({"kind": "paragraph", "text": T.text(chs), "spans": T.spans(chs, labels), "chars": chs,
                                   "mcids": list(T.last_collected)})
                    conv[f"untyped_struct_{t}_as_paragraph"] += 1

        mark_titles = {m["title"].strip() for m in marks}
        front = top("fm-front-matter", "division", "Front Matter", "front-matter")
        front["attrs"]["synthetic_title"] = True
        cur_top, cur_sec = front, None
        seen_body = False
        pending_caption = None
        last_figure = None
        pending_fig_caption = None
        for bi, b in enumerate(blocks):
            k = b["kind"]
            nxt_kind = blocks[bi + 1]["kind"] if bi + 1 < len(blocks) else None
            if k == "heading":
                text = b["text"]
                is_top = b["level"] == 1 and text in mark_titles
                if is_top:
                    seen_body = True
                    m = _HNUM.match(text)
                    if m and "." not in m.group(1):
                        num = int(m.group(1))
                        cur_top = top(f"sec{num}", "chapter", m.group(2), f"sec-{num}-{slugify(m.group(2), 40)}", number=num)
                    else:
                        pre = "bm" if text.startswith(("References", "Appendix")) else "fm"
                        cur_top = top(f"{pre}-{slugify(text, 40)}", "division", text, slugify(text, 60))
                    new(cur_top, "heading", "h", text=text, attrs={"level": 1, "title": text, "discrete": False},
                        source_spans=b["spans"], mcids=b["mcids"])
                    cur_sec = None
                    continue
                if b["level"] == 2 or (b["level"] == 1 and not is_top):
                    parent = cur_top
                    if b["level"] == 1:  # title-page heading (not a navigation unit)
                        new(cur_top, "heading", "h", text=text, attrs={"level": 1, "title": text, "discrete": True},
                            source_spans=b["spans"], mcids=b["mcids"])
                        continue
                    m = _HNUM.match(text)
                    title = m.group(2) if m else text
                    sec = new(parent, "section", "s", attrs={"level": 2, "title": title, "number": m.group(1) if m else None},
                              mapping_status="aggregate", mcids=[])
                    sec["anchor"] = f"sec-{m.group(1).replace('.', '-')}-{slugify(title, 40)}" if m else f"{parent['id']}-{slugify(title, 40)}"
                    new(sec, "heading", "h", text=text, attrs={"level": 2, "title": title, "discrete": False},
                        source_spans=b["spans"], mcids=b["mcids"])
                    cur_sec = sec
                    continue
            container = cur_sec or cur_top
            if k == "paragraph":
                if nxt_kind == "figure" and re.match(r"^(?:Figure|Fig\.) \d+\.", b["text"]):
                    pending_fig_caption = b  # tag order puts the caption before its figure
                    continue
                if last_figure is not None and re.match(r"^(?:Figure|Fig\.) \d+\.", b["text"]):
                    cap = new(last_figure, "caption", "cap", text=b["text"], source_spans=b["spans"],
                              attrs={"caption_of": "figure"}, mcids=b["mcids"])
                    cap["md"] = md_escape(b["text"])
                    m = re.match(r"^(?:Figure|Fig\.) (\d+)\.\s*(.*)$", b["text"])
                    last_figure["attrs"].update({"label": "Figure", "number": int(m.group(1)), "caption": m.group(2)})
                    last_figure = None
                    continue
                e = new(container, "paragraph", "p", text=b["text"], source_spans=b["spans"], mcids=b["mcids"])
                e["md"], e["attrs"]["links"] = _md_with_links(b, pages)
                last_figure = None
            elif k == "caption":
                pending_caption = b
            elif k == "list":
                _list(new, container, b["node"], T, labels)
            elif k == "table":
                _table(new, container, b["node"], T, labels, pending_caption, issue, conv, csf)
                pending_caption = None
            elif k == "figure":
                last_figure = _figure(new, container, b["node"], pages, labels)
                if pending_fig_caption is not None:
                    pc = pending_fig_caption
                    cap = new(last_figure, "caption", "cap", text=pc["text"], source_spans=pc["spans"],
                              attrs={"caption_of": "figure"}, mcids=pc["mcids"])
                    cap["md"] = md_escape(pc["text"])
                    m = re.match(r"^(?:Figure|Fig\.) (\d+)\.\s*(.*)$", pc["text"])
                    last_figure["attrs"].update({"label": "Figure", "number": int(m.group(1)), "caption": m.group(2)})
                    pending_fig_caption = None
                    last_figure = None
            elif k == "toc":
                toc = top("fm-table-of-contents", "division", "Table of Contents", "table-of-contents")
                for i, ch in enumerate(b["node"].get("children", [])):
                    chs = T.collect(ch)
                    t = T.text(chs)
                    if t:
                        mm = re.match(r"^(.*?)[\s.]*\.{2,}\s*([0-9ivxlc]+)$", t)
                        new(toc, "toc_entry", "toc", text=t, source_spans=T.spans(chs, labels),
                            attrs={"title": mm.group(1).strip() if mm else t, "target_page_label": mm.group(2) if mm else None,
                                   "indent": 0})
                cur_top = toc
                cur_sec = None

    # ---- recover marked content that the structure tree does not reference (source tagging defects)
    by_id_tmp = {e["id"]: e for e in els}
    for pno, p in sorted(pages.items()):
        orphan = collections.defaultdict(list)
        for c in p["chars"]:
            if c["mcid"] is not None and (pno, c["mcid"]) not in T.used and c["text"].strip():
                orphan[c["mcid"]].append(c)
        for mcid in sorted(orphan):
            prev = [(m, owner) for (pp, m), owner in mcid_owner.items() if pp == pno and m < mcid]
            nxt = [(m, owner) for (pp, m), owner in mcid_owner.items() if pp == pno and m > mcid]
            if not prev and not nxt:
                continue
            before = not prev  # no earlier owner on this page: insert before the next one
            anchor_id = max(prev)[1] if prev else min(nxt)[1]
            anchor_el = by_id_tmp[anchor_id]
            parent = by_id_tmp.get(anchor_el.get("parent_id"))
            if parent is None:
                continue
            chs = [(pno, c) for c in p["chars"] if c["mcid"] == mcid]
            T.used.add((pno, mcid))
            txt = T.text(chs).lstrip("•◦▪- ").strip()
            typ, code = ("list_item", "i") if anchor_el["type"] == "list_item" else ("paragraph", "p")
            rec_el = new(parent, typ, code, text=txt, source_spans=T.spans(chs, labels),
                         attrs={"recovered_from_untagged_structure": True, "mcid": mcid}, mcids=[(pno, mcid)])
            rec_el["md"] = md_escape(txt)
            # move it directly after its anchor in reading order
            parent["children"].remove(rec_el)
            parent["children"].insert(parent["children"].index(anchor_el) + (0 if before else 1), rec_el)
            by_id_tmp[rec_el["id"]] = rec_el
            mcid_owner[(pno, mcid)] = rec_el["id"]
            conv["recovered_unreferenced_marked_content"] += 1
            issue("minor", "source_tagging_defect_recovered", rec_el["id"],
                  f"Marked content (page {pno}, MCID {mcid}) is not referenced by the PDF structure tree; recovered "
                  f"as a {typ} {'before' if before else 'after'} {anchor_id}: {txt[:100]}")

    # ---- coverage: tagged chars not referenced by the structure tree; untagged chars outside furniture bands
    furniture = {}
    gaps_by_page = collections.defaultdict(list)
    for pno, p in pages.items():
        h = p["height"]
        fl = []
        untagged = [c for c in p["chars"] if c["mcid"] is None and c["text"].strip()]
        band = [c for c in untagged if c["top"] < h * 0.09 or c["top"] > h * 0.92]
        for ln in pdfsource._lines_from_chars(band):
            fl.append({"text": ln["text"], "bbox": ln["bbox"]})
        furniture[pno] = fl
        body_untagged = [c for c in untagged if c not in band]
        if body_untagged and furniture_pats:  # recipe-declared furniture (e.g. a TLP marking) anywhere on the page
            keep = []
            for ln in pdfsource._lines_from_chars(body_untagged):
                if any(fp.search(ln["text"].strip()) for fp in furniture_pats):
                    fl.append({"text": ln["text"], "bbox": ln["bbox"]})
                else:
                    keep.append(ln)
            body_untagged = [c for c in body_untagged if any(pdfsource.char_in_bbox(c, ln["bbox"], tol=0.5) for ln in keep)]
        if body_untagged:
            lines = pdfsource._lines_from_chars(body_untagged)
            txt = " ".join(l["text"] for l in lines)
            gaps_by_page[pno].append({"class": "untagged_body_text", "n_key_chars": len(text_key(txt)), "text": txt[:300]})
        unref = collections.defaultdict(list)
        for c in p["chars"]:
            if c["mcid"] is not None and (pno, c["mcid"]) not in T.used and c["text"].strip():
                unref[c["mcid"]].append(c)
        for mcid, cs in unref.items():
            txt = "".join(c["text"] for c in cs)
            gaps_by_page[pno].append({"class": "tagged_but_unreferenced", "n_key_chars": len(text_key(txt)),
                                      "text": txt[:300], "mcid": mcid})
    for pno, gs in gaps_by_page.items():
        for g in gs:
            if g["n_key_chars"] >= 3:
                issue("major", f"coverage:{g['class']}", f"page:{pno}", f"PDF text not represented: {g['text'][:120]}")

    # ---- finalize records
    flat: list[dict] = []

    def walk(e):
        flat.append(e)
        for c in e["children"]:
            walk(c)

    walk(root)
    for i, e in enumerate(flat):
        e["order"] = i
        e.setdefault("parent_id", None)
        e["children"] = [c if isinstance(c, str) else c["id"] for c in e["children"]]
    by_id = {e["id"]: e for e in flat}
    for e in reversed(flat):  # aggregate container spans
        if not e["children"] or e["type"] in ("figure", "table"):
            continue
        agg = {s["page"]: dict(s, role="aggregate", mapping_status="aggregate", n_key_chars=None) for s in e["source_spans"]}
        for cid in e["children"]:
            for s in by_id[cid]["source_spans"]:
                a = agg.get(s["page"])
                if a is None:
                    agg[s["page"]] = dict(s, role="aggregate", mapping_status="aggregate", n_key_chars=None)
                else:
                    a["bbox"] = [min(a["bbox"][0], s["bbox"][0]), min(a["bbox"][1], s["bbox"][1]),
                                 max(a["bbox"][2], s["bbox"][2]), max(a["bbox"][3], s["bbox"][3])]
        e["source_spans"] = [agg[k] for k in sorted(agg)]
        if e["mapping_status"] == "native_structure" and e["type"] not in ("list_item",):
            e["mapping_status"] = "aggregate"
    records = []
    for e in flat:
        payload = e["text"]
        if e["type"] == "table":
            payload = "\n".join("\t".join(c["text"] for c in r["cells"]) for r in e["attrs"]["rows"])
        rec = {"id": e["id"], "type": e["type"], "order": e["order"], "parent_id": e.get("parent_id"),
               "children": e["children"], "anchor": e.get("anchor"), "text": e.get("text") or "",
               "md": e.get("md") or (md_escape(e["text"]) if e.get("text") else ""), "attrs": e["attrs"],
               "source_spans": e["source_spans"], "mapping_status": e["mapping_status"],
               "extraction": ({"method": "tagged_pdf_structure_tree", "route": "tagged_pdf_structure",
                               "unit_statuses": [], "similarity": None, "visible_equal": None,
                               "visible_differences": [], "variables": []} if e["type"] != "document" else None),
               "content_hash": sha256_text(e["type"] + "\x1f" + payload)}
        records.append(rec)
    document = {
        "schema_version": "1.0.0",
        **{k: meta[k] for k in ("document_id", "pack_id", "pack_version", "edition_id", "title", "authors",
                                "edition_statement", "route")},
        "sources": meta["sources"], "coordinate_convention": COORDS,
        "page_numbering": {"physical": "one-based PDF page index",
                           "printed": "read from each page's untagged footer text (the PDF has no /PageLabels)"},
        "root_id": "doc", "page_labels": [{"page": p, "label": labels[p]} for p in sorted(pages)],
        "element_count": len(records), "elements": records,
    }
    page_map = _page_map(pages, labels, furniture, gaps_by_page, records, meta)
    summary = {
        "structure_elements": dict(collections.Counter(n["type"] for n in _walk_nodes(ex["tree"]))),
        "printed_labels_found": sum(1 for v in labels.values() if v), "pages": len(pages),
        "line_end_hyphen_joins": T.hyphen_joins, "list_items_split_at_printed_bullets": T.split_items,
        **{k: v for k, v in conv.items()},
        "elapsed_s": round(time.time() - t0, 1),
    }
    return {"document": document, "page_map": page_map, "annotations": [], "images": {}, "issues": issues,
            "bookmark_roles": bookmark_roles,
            "qa": {"structure-report.json": summary}, "from_cache": ex.get("from_cache"), "route": "tagged_pdf_structure",
            "route_summary": summary, "conversion_changes": [
                "Text and reading order come from the PDF's tagged structure tree (marked-content IDs); untagged running "
                "headers and footers are excluded from reading text and kept in `page-map.json`.",
                "Line breaks inside paragraphs and table cells are joined with spaces; a line ending in a hyphen is "
                f"joined without a space and the hyphen kept ({T.hyphen_joins} occurrences).",
                f"Table rows split across pages by the tagging tool are merged into one logical row using cell x-positions "
                f"({conv.get('table_continuation_rows_merged', 0)} continuation rows); header rows repeated on continuation "
                f"pages ({conv.get('table_repeated_header_rows_dropped', 0)}) are not repeated in the canonical table.",
                "Figure descriptions are the alternate text stored in the PDF's tags (source text); figure images are "
                "rendered from the PDF region.",
                f"List items that the source tags merge across a page break but that are printed as separate bullets are "
                f"split at the printed bullet ({T.split_items} occurrence(s)); bullet glyphs are list labels, not item text.",
                f"List items the source tags without a bullet inside a bulleted list (a wrapped bullet, or an indented "
                f"paragraph) are joined to the preceding item ({getattr(T, 'merged_items', 0)} occurrence(s)).",
                "Printed page numbers are read from each page's footer (the PDF has no page-label tree).",
                "Generated material, clearly marked: linked contents, page anchors, the chunk heading-context line, and "
                "fragment markers for split tables."]}


_CONTAINERS = {"Document", "Part", "Sect", "Div", "Art", "NonStruct", "BlockQuote", "Private", "Index", "Story", ""}
_HTAG = re.compile(r"^H([1-6])?$")
_TEXT_TAGS = {"P", "Span", "Link", "Reference", "Note", "Quote", "Code", "Lbl", "BibEntry", "TOCI", "Annot", "Form",
              "Formula"}
_FIG_CAP = re.compile(r"^(?:Figure|Fig\.)\s*(\d+)[.:]\s*(.*)$")


def _outline_blocks(tree, T: _Text, labels, conv) -> list[dict]:
    """Reading-order blocks of a structure tree, walking into containers (Part/Sect/Div/...) at any depth."""
    blocks: list[dict] = []

    def text_block(n, kind="paragraph", **kw):
        T.last_collected = []
        chs = T.collect(n)
        txt = T.text(chs)
        if txt:
            blocks.append({"kind": kind, "text": txt, "spans": T.spans(chs, labels), "chars": chs,
                           "mcids": list(T.last_collected), **kw})

    def walk(nodes):
        for n in nodes:
            typ = n["type"]
            if typ in _CONTAINERS:
                if n.get("mcrefs"):  # text placed directly in a container
                    text_block({"mcrefs": n["mcrefs"], "children": []})
                    conv["container_text_as_paragraph"] += 1
                walk(n.get("children", []))
            elif _HTAG.match(typ):
                text_block(n, "heading", level=int(_HTAG.match(typ).group(1) or 1))
            elif typ in ("L", "Table", "Figure", "TOC"):
                blocks.append({"kind": {"L": "list", "Table": "table", "Figure": "figure", "TOC": "toc"}[typ], "node": n})
            elif typ == "Caption":
                text_block(n, "caption")
            elif typ == "Artifact":
                T.collect(n)  # tagged furniture: accounted for, not reading text
            else:
                text_block(n)
                if typ not in _TEXT_TAGS:
                    conv[f"untyped_struct_{typ}_as_paragraph"] += 1
    walk(tree)
    return blocks


def _outline_build(tree, T: _Text, labels, marks, new, top, issue, conv, csf: bool, hidden=()) -> None:
    """Hierarchy from the PDF outline: a heading or paragraph whose text matches the next outline entry (allowing a
    few entries without a heading, e.g. bookmarks on table header cells), on the entry's page or the next, opens
    a unit (outline level 0) or a section nested by outline level."""
    blocks = _outline_blocks(tree, T, labels, conv)
    if hidden:  # recipe-declared regions whose text is not printed (e.g. covered by artwork): not reading text
        kept = []
        for b in blocks:
            spans = b.get("spans")
            if spans is None:
                T.last_collected = []
                spans = T.spans(T.collect(b["node"]), labels)
            region = next((h for h in hidden if spans and all(
                s["page"] == h["page"] and s["bbox"][0] >= h["bbox"][0] - 1 and s["bbox"][1] >= h["bbox"][1] - 1
                and s["bbox"][2] <= h["bbox"][2] + 1 and s["bbox"][3] <= h["bbox"][3] + 1 for s in spans)), None)
            if region is None:
                kept.append(b)
                continue
            text = b.get("text") or T.text(T.collect(b["node"]))
            issue("minor", "source_hidden_text_excluded", f"page:{region['page']}",
                  f"{region['reason']} Excluded {b['kind']}: {text[:100]}")
            conv["hidden_blocks_excluded"] += 1
        blocks = kept
    entries = [dict(m, key=text_key(m["title"])) for m in marks if text_key(m["title"])]
    roles: dict[int, str] = {}
    ptr = 0
    front = top("fm-front-matter", "division", "Front Matter", "front-matter")
    front["attrs"]["synthetic_title"] = True
    stack: list[tuple[int, dict]] = [(-1, front)]
    unit_ids: set[str] = set()
    anchors: set[str] = set()
    pending_caption = None
    last_figure = None

    def match(b, exact: bool = False):
        nonlocal ptr
        if b["kind"] not in ("heading", "paragraph", "caption", "row") or not b["spans"]:
            return None
        k, pg = text_key(b["text"]), b["spans"][0]["page"]
        window = 60 if b["kind"] in ("heading", "caption") else 8  # tagged headings may skip many entries
        for j in range(ptr, min(ptr + window, len(entries))):
            o = entries[j]
            same = k == o["key"] or (not exact and k.startswith(o["key"]) and len(k) <= len(o["key"]) + 2)
            if same and o["page"] <= pg <= o["page"] + 1:
                for s in entries[ptr:j]:
                    issue("minor", "outline_entry_without_heading", f"page:{s['page']}",
                          f"outline entry {s['title']!r} (level {s['level']}) has no heading block in the structure tree")
                ptr = j + 1
                return o
        return None

    def unique(base: str, seen: set) -> str:
        out, n = base, 2
        while out in seen:
            out, n = f"{base}-{n}", n + 1
        seen.add(out)
        return out

    def open_unit(o, b, level: int) -> None:
        nonlocal stack
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else front
        title = b["text"]
        if level == 0:
            uid = unique(slugify(title, 40), unit_ids)
            unit = top(uid, "chapter", title, unique(uid, anchors))
        else:
            unit = new(parent, "section", "s", attrs={"level": level + 1, "title": title, "number": None},
                       mapping_status="aggregate", mcids=[])
            unit["anchor"] = unique(f"{parent.get('anchor') or parent['id']}-{slugify(title, 40)}", anchors)
        new(unit, "heading", "h", text=title, attrs={"level": level + 1, "title": title, "discrete": False},
            source_spans=b["spans"], mcids=b["mcids"])
        stack.append((level, unit))

    def node_block(n, kind):
        T.last_collected = []
        chs = T.collect(n)
        return {"kind": kind, "text": T.text(chs), "spans": T.spans(chs, labels), "chars": chs,
                "mcids": list(T.last_collected)}

    # Word tags a table's title as its Caption child: lift it out as its own block so it can open a unit
    expanded: list[dict] = []
    for b in blocks:
        if b["kind"] == "table":
            for c in b["node"].get("children", []):
                if c["type"] == "Caption":
                    cb = node_block(c, "caption")
                    if cb["text"]:
                        expanded.append(cb)
            b = dict(b, node=dict(b["node"], children=[c for c in b["node"].get("children", []) if c["type"] != "Caption"]))
        expanded.append(b)
    # a paragraph tagged just before a caption but printed below it on the same page reads after it
    for i in range(1, len(expanded)):
        cap, prev = expanded[i], expanded[i - 1]
        if cap["kind"] == "caption" and prev["kind"] == "paragraph" and cap["spans"] and prev["spans"] \
                and cap["spans"][0]["page"] == prev["spans"][0]["page"] \
                and prev["spans"][0]["bbox"][1] > cap["spans"][-1]["bbox"][3]:
            expanded[i - 1], expanded[i] = cap, prev
            conv["paragraph_moved_below_table_title"] += 1
    blocks = expanded

    for b in blocks:
        kind = b["kind"]
        container = stack[-1][1]
        o = match(b)
        if o is not None:
            open_unit(o, b, o["level"])
            last_figure = None
            continue
        if kind == "table":
            # Word tags a table's title as its Caption child, and a checklist's phase and step headings as full-width
            # rows. A caption matching the next outline entry opens that unit; a row matching it opens a section
            # (nested below the unit that holds the table) and the table continues in it under the same header row.
            node = b["node"]
            trs: list[dict] = []
            for c in node.get("children", []):
                if c["type"] == "TR":
                    trs.append(c)
                elif c["type"] in ("THead", "TBody", "TFoot"):
                    trs += [r for r in c.get("children", []) if r["type"] == "TR"]
            header = trs[0] if trs and any(cc["type"] == "TH" for cc in trs[0].get("children", [])) else None
            base = next((lv for lv, _ in reversed(stack) if lv >= 0 and not _.get("attrs", {}).get("in_table")), -1)
            segment: list[dict] = []

            def flush():
                nonlocal pending_caption
                if segment:
                    _table(new, stack[-1][1], {"type": "Table", "children": ([header] if header else []) + segment},
                           T, labels, pending_caption, issue, conv, csf)
                    pending_caption = None
                    segment.clear()
            for tr in trs:
                if tr is header:
                    continue
                cells = [cc for cc in tr.get("children", []) if cc["type"] in ("TH", "TD")]
                filled = [cc for cc in cells if T.text(T.collect(cc))] if cells else []
                ro = None
                if len(filled) == 1:  # a full-width row: a heading if it is the next outline entry
                    rb = node_block(tr, "row")
                    ro = match(rb, exact=True)
                if ro is None:
                    segment.append(tr)
                    continue
                flush()
                roles[ro["order"]] = "table_row_heading"
                open_unit(ro, rb, base + 1 + ro["level"])
                stack[-1][1]["attrs"]["in_table"] = True
                conv["outline_headings_in_table_rows"] += 1
            flush()
            continue
        if kind == "heading":  # tagged heading that is not an outline entry (e.g. a cover title)
            new(container, "heading", "h", text=b["text"], attrs={"level": b["level"], "title": b["text"], "discrete": True},
                source_spans=b["spans"], mcids=b["mcids"])
        elif kind == "paragraph":
            e = new(container, "paragraph", "p", text=b["text"], source_spans=b["spans"], mcids=b["mcids"])
            e["md"], e["attrs"]["links"] = _md_with_links(b, T.pages)
            last_figure = None
        elif kind == "caption":
            if last_figure is not None:
                cap = new(last_figure, "caption", "cap", text=b["text"], source_spans=b["spans"],
                          attrs={"caption_of": "figure"}, mcids=b["mcids"])
                cap["md"] = md_escape(b["text"])
                m = _FIG_CAP.match(b["text"])
                last_figure["attrs"].update({"label": "Figure" if m else None, "number": int(m.group(1)) if m else None,
                                             "caption": m.group(2) if m else b["text"]})
                last_figure = None
            else:
                pending_caption = b
        elif kind == "list":
            _list(new, container, b["node"], T, labels)
        elif kind == "figure":
            last_figure = _figure(new, container, b["node"], T.pages, labels)
            if pending_caption is not None and _FIG_CAP.match(pending_caption["text"]):
                pc = pending_caption
                cap = new(last_figure, "caption", "cap", text=pc["text"], source_spans=pc["spans"],
                          attrs={"caption_of": "figure"}, mcids=pc["mcids"])
                cap["md"] = md_escape(pc["text"])
                m = _FIG_CAP.match(pc["text"])
                last_figure["attrs"].update({"label": "Figure", "number": int(m.group(1)), "caption": m.group(2)})
                pending_caption = None
                last_figure = None
        elif kind == "toc":
            toc = top("fm-table-of-contents", "division", "Table of Contents", "table-of-contents")
            for ch in b["node"].get("children", []):
                T.last_collected = []
                chs = T.collect(ch)
                tx = T.text(chs)
                if tx:
                    mm = re.match(r"^(.*?)[\s.]*\.{2,}\s*([0-9ivxlc]+)$", tx)
                    new(toc, "toc_entry", "toc", text=tx, source_spans=T.spans(chs, labels),
                        attrs={"title": mm.group(1).strip() if mm else tx, "target_page_label": mm.group(2) if mm else None,
                               "indent": 0})
            stack = [(-1, toc)]
    if pending_caption is not None:  # a caption with no figure or table after it stays as text
        e = new(stack[-1][1], "paragraph", "p", text=pending_caption["text"], source_spans=pending_caption["spans"],
                mcids=pending_caption["mcids"])
        e["md"] = md_escape(pending_caption["text"])
    for s in entries[ptr:]:
        issue("minor", "outline_entry_without_heading", f"page:{s['page']}",
              f"outline entry {s['title']!r} (level {s['level']}) has no heading block in the structure tree")
    return roles


def _walk_nodes(nodes):
    for n in nodes or []:
        yield n
        yield from _walk_nodes(n.get("children", []))


def _md_with_links(b: dict, pages) -> tuple[str, list[dict]]:
    text = b["text"]
    md = md_escape(text)
    links = []
    for pno in {s["page"] for s in b["spans"]}:
        for l in pages[pno]["links"]:
            inside = [c for p2, c in b["chars"] if p2 == pno and pdfsource.char_in_bbox(c, l["bbox"], tol=1)]
            lt = re.sub(r"\s+", " ", "".join(c["text"] for c in inside)).strip()
            if lt and text.count(lt) == 1:
                links.append({"href": l["uri"], "text": lt, "pdf_link_annotation": "source"})
                esc = md_escape(lt)
                if md.count(esc) == 1:
                    md = md.replace(esc, f"[{esc}]({l['uri']})")
    return md, links


def _lead_chars(node, T: _Text, n: int = 2) -> list[dict]:
    """The first ``n`` characters of a structure node in its /K order, from the first visible one (no side effects
    on the text tracker)."""
    out: list[dict] = []

    def walk(nd) -> bool:
        kids = nd.get("kids")
        if kids is None:
            kids = [{"mc": r} for r in nd.get("mcrefs", [])] + [{"node": i} for i in range(len(nd.get("children", [])))]
        for k in kids:
            if "mc" in k:
                for c in T.by_mcid.get(tuple(k["mc"]), []):
                    if out or c["text"].strip():
                        out.append(c)
                        if len(out) >= n:
                            return True
            elif walk(nd["children"][k["node"]]):
                return True
        return False
    walk(node)
    return out


def _starts_with_bullet(node, T: _Text) -> bool:
    """A hard bullet glyph, or a soft one ("o", "-", ...) followed by a space or set in a different font."""
    lead = _lead_chars(node, T)
    if not lead:
        return False
    first = lead[0]
    if first["text"] in _HARD_BULLETS:
        return True
    if first["text"] in _BULLETS and len(lead) > 1:
        return not lead[1]["text"].strip() or lead[1].get("font") != first.get("font")
    return False


def _list(new, container, node, T: _Text, labels) -> None:
    lst = item = None
    list_bulleted = False
    for li in node.get("children", []):
        if li["type"] == "L":  # a nested list tagged as a child of the list (after its item), as Word does
            if item is None:
                # before any item: Word split the list at a page break, and these are the sub-items of the previous
                # list's last item; with no previous list they stay a list of their own
                prev = container["children"][-1] if container["children"] else None
                prev_item = next((c for c in reversed(prev["children"]) if c["type"] == "list_item"), None) \
                    if prev is not None and prev["type"] == "list" else None
                _list(new, prev_item if prev_item is not None else container, li, T, labels)
            else:
                _list(new, item, li, T, labels)
            continue
        if li["type"] != "LI":
            continue

        body = [c for c in li.get("children", []) if c["type"] in ("LBody", "P", "Lbl")]
        own, nested = [], []
        for bnode in body:
            if bnode["type"] == "Lbl":
                T.collect(bnode)  # list label (bullet) is not item text
                continue
            if bnode.get("mcrefs"):
                own.append({"mcrefs": bnode["mcrefs"], "children": []})
            for c in bnode.get("children", []):
                (nested if c["type"] == "L" else own).append(c)
        chs = []
        for o in own:
            chs += T.collect(o)
        bulleted = _starts_with_bullet(li, T) or any(bn["type"] == "Lbl" for bn in body)  # a label tag is a bullet
        # an item without a bullet in a bulleted list continues the previous item (Word splits a wrapped bullet or an
        # indented paragraph into its own list item); a page break may put it at the start of the next list
        target = item
        if target is None and not bulleted:
            prev = container["children"][-1] if container["children"] else None
            target = next((c for c in reversed(prev["children"]) if c["type"] == "list_item"), None) \
                if prev is not None and prev["type"] == "list" else None
        if chs and not bulleted and target is not None and (list_bulleted or item is None) \
                and target.get("attrs", {}).get("bulleted"):
            txt = T.text(chs)
            sep = " " if target["text"] and target["text"][-1] not in ".:;!?)" else "\n"
            target["text"] = (target["text"] + sep + txt).strip()
            target["md"] = md_escape(target["text"])
            target["source_spans"] = target["source_spans"] + T.spans(chs, labels)
            target["attrs"]["continued_from_unbulleted_item"] = True
            T.merged_items = getattr(T, "merged_items", 0) + 1
            T.last_collected = []
            for nl in nested:
                _list(new, target, nl, T, labels)
            continue
        list_bulleted = list_bulleted or bulleted
        if lst is None:
            lst = new(container, "list", "l", attrs={"ordered": False, "style": None, "start": None, "title": None},
                      mapping_status="aggregate", mcids=[])
        # a leading bullet glyph set in a different font from the item text is a label, not text
        if chs and chs[0][1]["text"] in _BULLETS and len(chs) > 2:
            first, nxt_c = chs[0][1], next((c for _, c in chs[1:] if c["text"].strip()), None)
            if nxt_c is not None and (first["font"] != nxt_c["font"] or first["text"] in _HARD_BULLETS
                                      or not chs[1][1]["text"].strip()):  # a soft bullet followed by a space
                chs = chs[1:]
        pieces, cur = [], []
        for idx, (pg, c) in enumerate(chs):
            if cur and c["text"] == "•" and (pg != cur[-1][0] or c["top"] > cur[-1][1]["bottom"] - 1):
                pieces.append(cur)
                cur = []
                continue  # the bullet glyph itself is a label
            cur.append((pg, c))
        pieces.append(cur)
        for pi, piece in enumerate(pieces):
            txt = T.text(piece)
            item = new(lst, "list_item", "i", text=txt, source_spans=T.spans(piece, labels),
                       attrs={"checked": None, **({"split_from_tagged_item": True} if len(pieces) > 1 else {}),
                              **({"bulleted": True} if bulleted else {})})
            item["md"] = md_escape(txt)
            if len(pieces) > 1 and pi > 0:
                T.split_items += 1
        for nl in nested:
            _list(new, item, nl, T, labels)


def _table(new, container, node, T: _Text, labels, caption, issue, conv, csf: bool = True) -> None:
    raw_rows = []
    for tr in node.get("children", []):
        if tr["type"] != "TR":
            continue
        cells = []
        for c in tr.get("children", []):
            if c["type"] not in ("TH", "TD"):
                continue
            # paragraphs inside a cell (e.g. separate R1/R2/C1 items) stay separate
            paras, chs = [], []
            for k in c.get("kids") or [{"node": i} for i in range(len(c.get("children", [])))]:
                sub = T.collect(c["children"][k["node"]]) if "node" in k else T.collect({"mcrefs": [k["mc"]], "children": [], "kids": [k]})
                if sub:
                    paras.append(T.text(sub))
                    chs += sub
            sp = T.spans(chs, labels)
            cells.append({"text": "\n".join(p_ for p_ in paras if p_), "header": c["type"] == "TH",
                          "scope": (c.get("attributes") or {}).get("Scope"), "spans": sp,
                          "x": (sp[0]["bbox"][0] + sp[0]["bbox"][2]) / 2 if sp else None,
                          "x0": sp[0]["bbox"][0] if sp else None})
        raw_rows.append(cells)
    if not raw_rows:
        return
    # leading rows narrower than the table (a title row such as "Response Contacts:") are its caption, not its header
    widest = max(len(r) for r in raw_rows)
    lead = []
    while len(raw_rows) > 1 and len(raw_rows[0]) < widest:
        lead.append(raw_rows.pop(0))
    if lead:
        conv["table_title_rows_as_caption"] += len(lead)
        lead_text = " ".join(c["text"] for r in lead for c in r if c["text"])
        lead_spans = [s for r in lead for c in r for s in c["spans"]]
        caption = ({"text": f"{caption['text']} {lead_text}".strip(), "spans": caption["spans"] + lead_spans}
                   if caption else {"text": lead_text, "spans": lead_spans})
    header = raw_rows[0]
    ncol = len(header)
    col_x0 = [c["x0"] for c in header]
    header_key = text_key(" ".join(c["text"] for c in header))

    def col_of(cell) -> int:
        if cell["x0"] is None:
            return 0
        best = 0
        for i, x in enumerate(col_x0):
            if x is not None and cell["x0"] >= x - 4:
                best = i
        return best

    rows = [{"section": "thead", "cells": [{"text": c["text"], "md": md_escape(c["text"]), "header": True, "colspan": 1,
                                             "rowspan": 1, "halign": None} for c in header],
             "source_spans": [s for c in header for s in c["spans"]], "native_locator": None}]
    last_in_col: list[dict | None] = [None] * ncol
    for cells in raw_rows[1:]:
        if text_key(" ".join(c["text"] for c in cells)) == header_key:
            conv["table_repeated_header_rows_dropped"] += 1
            continue
        first = cells[0]["text"] if cells else ""
        m = _CSF_ID.match(first) if first and csf else None
        row_pages = [s["page"] for c in cells for s in c["spans"]]
        prev_pages = [s["page"] for s in rows[-1]["source_spans"]] if rows else []
        new_page = bool(row_pages and prev_pages and min(row_pages) > max(prev_pages))
        if len(cells) < ncol and not new_page and rows and rows[-1]["section"] != "thead" or \
                (len(cells) < ncol and rows and rows[-1]["section"] == "thead"):
            # a row with spanned cells (rowspan), not a page-split continuation
            placed: dict[int, dict] = {}
            for c in cells:
                placed[col_of(c)] = c
            out_cells = []
            for ci in range(ncol):
                if ci in placed:
                    c = placed[ci]
                    cell = {"text": c["text"], "md": md_escape(c["text"]), "header": c["header"], "colspan": 1,
                            "rowspan": 1, "halign": None}
                    out_cells.append(cell)
                    last_in_col[ci] = cell
                elif last_in_col[ci] is not None:
                    last_in_col[ci]["rowspan"] += 1
            rows.append({"section": "tbody", "cells": out_cells, "source_spans": [s for c in cells for s in c["spans"]],
                         "native_locator": None})
            conv["table_rowspan_rows"] += 1
            continue
        if len(cells) == ncol and (m or (rows and rows[-1]["section"] == "thead") or (not m and not new_page)):
            r = {"section": "tbody", "cells": [{"text": c["text"], "md": md_escape(c["text"]), "header": c["header"],
                                                 "colspan": 1, "rowspan": 1, "halign": None} for c in cells],
                 "source_spans": [s for c in cells for s in c["spans"]]}
            for ci, cell in enumerate(r["cells"]):
                last_in_col[ci] = cell
            if m:
                fid, cat, sub = m.group(1), m.group(2), m.group(3)
                cid = fid + (f".{cat}" if cat else "") + (f"-{sub}" if sub else "")
                r["native_locator"] = f"csf2:{cid}"
                r["group"] = fid + (f".{cat}" if cat else "")
                r["csf_level"] = "subcategory" if sub else ("category" if cat else "function")
            else:
                r["native_locator"] = None
            rows.append(r)
            continue
        # continuation of the previous logical row (split across a page break by the tagging tool)
        if len(rows) < 2:
            issue("minor", "table_row_unplaced", "table", f"row before first data row: {first[:60]}")
            continue
        prev = rows[-1]
        for c in cells:
            if not c["text"]:
                continue
            ci = col_of(c)
            pc = prev["cells"][ci]
            pc["text"] = (pc["text"] + "\n" + c["text"]).strip()  # continuation starts a new paragraph line
            pc["md"] = md_escape(pc["text"])
        prev["source_spans"] += [s for c in cells for s in c["spans"]]
        prev["continued_across_pages"] = sorted({s["page"] for s in prev["source_spans"]})
        conv["table_continuation_rows_merged"] += 1
    # merge spans per page for each row
    for r in rows:
        agg = {}
        for s in r["source_spans"]:
            a = agg.get(s["page"])
            if a is None:
                agg[s["page"]] = dict(s)
            else:
                a["bbox"] = [min(a["bbox"][0], s["bbox"][0]), min(a["bbox"][1], s["bbox"][1]),
                             max(a["bbox"][2], s["bbox"][2]), max(a["bbox"][3], s["bbox"][3])]
                a["n_key_chars"] = (a.get("n_key_chars") or 0) + (s.get("n_key_chars") or 0)
        r["source_spans"] = [dict(agg[k], native_locator=r.get("native_locator")) for k in sorted(agg)]
    cap_text = caption["text"] if caption else None
    mm = re.match(r"^Table (\d+)\.\s*(.*)$", cap_text or "")
    complex_ = any(c["rowspan"] > 1 for r in rows for c in r["cells"])
    attrs = {"rows": rows, "complex": complex_, "label": "Table" if mm else None, "number": int(mm.group(1)) if mm else None,
             "caption": mm.group(2).strip() if mm else cap_text, "caption_md": md_escape(mm.group(2).strip()) if mm else None,
             "n_rows": len(rows), "n_cols": ncol, "header_rows": 1, "row_groups": "csf_category" if csf else None}
    spans = {}
    for s in (caption["spans"] if caption else []) + [s for r in rows for s in r["source_spans"]]:
        a = spans.get(s["page"])
        if a is None:
            spans[s["page"]] = dict(s, native_locator=None)
        else:
            a["bbox"] = [min(a["bbox"][0], s["bbox"][0]), min(a["bbox"][1], s["bbox"][1]),
                         max(a["bbox"][2], s["bbox"][2]), max(a["bbox"][3], s["bbox"][3])]
    text = "\n".join(([cap_text] if cap_text else []) + [" | ".join(c["text"] for c in r["cells"]) for r in rows])
    new(container, "table", "t", text=text, attrs=attrs, source_spans=[spans[k] for k in sorted(spans)])


def _figure(new, container, node, pages, labels) -> dict:
    refs = node.get("mcrefs") or []
    pno = refs[0][0] if refs else None
    bbox = None
    if pno:
        imgs = [i for i in pages[pno]["images"] if i.get("mcid") in [m for p_, m in refs if p_ == pno]]
        if imgs:
            bbox = [min(i["bbox"][0] for i in imgs), min(i["bbox"][1] for i in imgs),
                    max(i["bbox"][2] for i in imgs), max(i["bbox"][3] for i in imgs)]
    alt = node.get("alt_text") or None
    spans = []
    if bbox:
        spans.append({"locator_type": "pdf_page", "page": pno, "page_label": labels.get(pno), "bbox": bbox,
                      "coords": COORDS, "role": "image", "n_key_chars": 0, "mapping_status": "native_structure"})
    fig = new(container, "figure", "f", source_spans=spans,
              attrs={"label": None, "number": None, "caption": None, "alt": alt, "alt_text_source": "pdf_structure_tree" if alt else None,
                     "pdf_image": {"page": pno, "bbox": bbox, "dims_match": None} if bbox else None})
    return fig


def _page_map(pages, labels, furniture, gaps_by_page, records, meta) -> dict:
    by_page = collections.defaultdict(list)
    for e in records:
        if e["children"] and e["type"] not in ("figure", "list_item"):
            continue
        for s in e["source_spans"]:
            if s.get("role") == "aggregate":
                continue
            if e["id"] not in by_page[s["page"]]:
                by_page[s["page"]].append(e["id"])
    img_owner = {}
    for e in records:
        for s in e["source_spans"]:
            if s.get("role") == "image":
                img_owner[(s["page"], tuple(s["bbox"]))] = e["id"]
    out = []
    for pno in sorted(pages):
        p = pages[pno]
        body = [c for c in p["chars"] if c["mcid"] is not None and c["text"].strip()]
        body_key = sum(1 for c in body if c["text"].isalnum())
        gaps = gaps_by_page.get(pno, [])
        unc = sum(g["n_key_chars"] for g in gaps)
        imgs = [{"bbox": i["bbox"], "srcsize": i["srcsize"], "raw_sha256": None,
                 "element_id": next((v for (pp, bb), v in img_owner.items() if pp == pno and list(bb) == i["bbox"]), None)}
                for i in p["images"]]
        if not body and not p["images"] and not (p["n_rects"] + p["n_curves"] + p["n_lines"]):
            cls = "no_extractable_content"
        elif not body:
            cls = "visual_only"
        elif p["images"]:
            cls = "text_and_images"
        else:
            cls = "text"
        status = ("unresolved" if any(g["n_key_chars"] >= 3 for g in gaps) else
                  ("visual_content" if cls == "visual_only" else ("unresolved" if cls == "no_extractable_content" else
                                                                  ("text_with_explained_gaps" if unc else "text_covered"))))
        out.append({"page": pno, "page_label": labels.get(pno), "width": p["width"], "height": p["height"],
                    "rotation": p["rotation"], "content_class": cls, "body_chars": len(body),
                    "vector_objects": p["n_rects"] + p["n_curves"] + p["n_lines"],
                    "furniture": furniture.get(pno, []), "images": imgs, "body_key_chars": body_key,
                    "uncovered_key_chars": unc, "uncovered_regions": gaps, "element_ids": by_page.get(pno, []),
                    "coverage_status": status})
    from .docmodel import label_ranges
    return {"schema_version": "1.0.0", "document_id": meta["document_id"],
            "source_sha256": meta["sources"]["authoritative"]["sha256"], "coordinate_convention": COORDS,
            "page_count": len(pages), "label_ranges": label_ranges([labels.get(p) for p in sorted(pages)]),
            "pages": out}
