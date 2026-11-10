"""Route ``pdf_collection_layout``: a collection of PDF documents, one unit each, converted from the page layout.

For PDFs without a usable structure tree: untagged files (for example re-distilled through Ghostscript) and Word
exports whose tags do not carry the printed hierarchy. Each recipe artifact is one PDF, hash-pinned in the lock;
the pack holds one unit per document, as a web pack holds one unit per article.

Per document:
* Lines come from pdfplumber's character layer: text, position, font and size. Nothing in the file is executed and
  no link is followed.
* Running headers and footers: lines whose text (digits ignored) repeats at about the same height on at least half
  of the pages are page furniture, kept out of the reading text. The printed page number is read from them
  (recipe ``printed_labels.pattern``).
* Headings: the PDF outline, when there is one, gives the section titles and levels; the line (or wrapped lines)
  matching the next outline entry becomes a section. Below the outline's depth, and in documents without an
  outline, a bold line that starts with a section number ("7.2 Live acquisition") is a heading, its level the
  number's depth; without an outline, a short bold line on its own is a heading too.
* Ruled tables are found by pdfplumber's line detection; their cells become table rows, and the lines inside them
  are not repeated as text.
* Bulleted, numbered and lettered items become list items, nested by the marker's indent; continuation lines (the
  hanging indent) join their item.
* Other lines join into paragraphs. A paragraph ends at a vertical gap wider than the document's line pitch, at a
  change of left margin or font size, or at a heading; it continues across a page break when its last line has no
  closing punctuation.
* Text printed before the first section (cover page, disclaimer, redistribution policy) is the unit's
  ``front-matter`` section.
* Images are recorded as figures (not captured), except images repeated on most pages (logos).

Locators: every element carries its PDF page, printed page label and bounding box, and a URL locator: the document
URL with the PDF open parameter of its section's first page (``…pdf#page=4``), which serves as the section anchor.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from pathlib import Path

from .mdrender import md_escape

ROUTE = "pdf_collection_layout"
_HARD_BULLETS = {"•", "◦", "▪", "●", "■", "□", "‣", "∙", "", "", "", "", "", "", "",
                 "", "➢", "✓"}
_SOFT_BULLETS = {"o", "-", "–", "*"}  # bullets only when indented and followed by a space
_ENUM = re.compile(r"^(?:\(?(\d{1,2}|[a-z]|[ivx]{1,5})[.)]|\((\d{1,2}|[a-z]|[ivx]{1,5})\))\s+", re.I)
_SECNUM = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+(\S.*)$")
_CLOSING = (".", ":", ";", "?", "!", ")", "”", '"')
# Notice and navigation headings that belong to a document's front matter, not its body
_FRONT_MATTER_TITLES = re.compile(r"^(disclaimer|redistribution policy|requests? for modification|intellectual property|"
                                  r"table of contents|contents|notice|copyright)\b", re.I)
_TOC_LINE = re.compile(r"(\.{3,}|…+)\s*\d{1,3}$|^\d{1,2}(\.\d{1,2})*\.?\s+\S.*\s\d{1,3}$")  # "7.2 Live …… 12", "7.3.2 Groups 13"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _bold(font: str) -> bool:
    f = font.split("+")[-1].lower()
    return any(w in f for w in ("bold", "black", "heavy", "semibold", "demi"))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "section"


# ------------------------------------------------------------------ page extraction

def _page_lines(page, pno: int) -> list[dict]:
    out = []
    for ln in page.extract_text_lines(return_chars=True, strip=True):
        chars = [c for c in ln["chars"] if c["text"].strip()]
        if not chars or not ln["text"].strip():
            continue
        size = collections.Counter(round(c["size"], 1) for c in chars).most_common(1)[0][0]
        out.append({"kind": "line", "page": pno, "text": " ".join(ln["text"].split()), "x0": ln["x0"], "x1": ln["x1"],
                    "top": ln["top"], "bottom": ln["bottom"], "size": size, "n": len(chars),
                    "bold": sum(1 for c in chars if _bold(c["fontname"])) >= 0.8 * len(chars)})
    return out


def _furniture(pages: list[dict]) -> set[tuple[int, int]]:
    """(page, line index) of running headers and footers: text (digits ignored) recurring at about the same height,
    near the top or bottom of the page, on at least half of the pages."""
    n = len(pages)
    if n < 3:
        return set()
    seen = collections.defaultdict(set)
    for p in pages:
        for i, ln in enumerate(p["lines"]):
            if ln["top"] < 0.16 * p["height"] or ln["top"] > 0.80 * p["height"]:
                seen[(re.sub(r"\d+", "#", ln["text"].lower()), round(ln["top"] / 6))].add(p["page"])
    keys = {k for k, ps in seen.items() if len(ps) >= max(2, n // 2)}
    texts = {k[0] for k in keys}  # the same running text printed at another height (a cover page's footer)
    out = set()
    for p in pages:
        for i, ln in enumerate(p["lines"]):
            norm = re.sub(r"\d+", "#", ln["text"].lower())
            zone = ln["top"] < 0.16 * p["height"] or ln["top"] > 0.80 * p["height"]
            if (norm, round(ln["top"] / 6)) in keys or zone and norm in texts:
                out.add((p["page"], i))
    return out


def _cols(t) -> list[tuple[float, float]]:
    """Column x-ranges of a pdfplumber table, from its row with the most cells. A sliver column (narrower than 8 pt,
    the gap between a band's edge and a rule drawn beside it) is merged into the column before it."""
    row = max(t.rows, key=lambda r: sum(1 for c in r.cells if c))
    cols: list[tuple[float, float]] = []
    for c in row.cells:
        if not c:
            continue
        if cols and c[2] - c[0] < 8:
            cols[-1] = (cols[-1][0], c[2])
        else:
            cols.append((c[0], c[2]))
    return cols


def _same_cols(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    return len(a) == len(b) and all(abs(x[0] - y[0]) <= 8 for x, y in zip(a, b))


def _white(color) -> bool:
    """A fill colour that is white in DeviceGray, DeviceRGB or DeviceCMYK."""
    c = color if isinstance(color, (list, tuple)) else (color,)
    if len(c) == 4:
        return all(v == 0 for v in c)
    return bool(c) and all(isinstance(v, (int, float)) and v >= 0.999 for v in c)


def _drawn(obj) -> bool:
    """Not an invisible white box: Word puts white rectangles behind runs of text, which are no table structure."""
    return not (obj.get("object_type") == "rect" and obj.get("fill") and not obj.get("stroke")
                and _white(obj.get("non_stroking_color")))


def _bold_share(chars) -> bool:
    chars = [c for c in chars if c["text"].strip()]
    return bool(chars) and sum(1 for c in chars if _bold(c["fontname"])) >= 0.8 * len(chars)


def _line_words(line: dict, words: list[dict]) -> list[dict]:
    return [w for w in words if line["top"] - 1 <= (w["top"] + w["bottom"]) / 2 <= line["bottom"] + 1
            and w["x0"] >= line["x0"] - 1 and w["x1"] <= line["x1"] + 1]


def _cells(ws: list[dict], cols: list[tuple[float, float]]) -> list[str]:
    cells = [""] * len(cols)
    for w in sorted(ws, key=lambda w: w["x0"]):
        xc = (w["x0"] + w["x1"]) / 2
        ci = max([i for i, (c0, _) in enumerate(cols) if c0 - 3 <= xc] or [0])
        cells[ci] = (cells[ci] + " " + w["text"]).strip()
    return cells


def _alignment(ws: list[dict], cols: list[tuple[float, float]]) -> str | None:
    """'row' when a line's words fall into two or more of the columns, starting in the first; 'cont' when they all
    lie in later columns (a wrapped cell); None when the line does not fit the columns."""
    if not ws or ws[0]["x0"] < cols[0][0] - 4 or max(w["x1"] for w in ws) > cols[-1][1] + 4:
        return None
    cells = _cells(ws, cols)
    if cells[0] and sum(1 for c in cells if c) >= 2:
        return "row"
    if not cells[0] and min(w["x0"] for w in ws) >= cols[1][0] - 4:
        return "cont"
    return None


def _tables(page, lines: list[dict]) -> list[dict]:
    """Tables on a page. A ruled table on its own is read cell by cell. Tables drawn as shaded row bands come out
    of pdfplumber's line detection as many one-row tables with the same columns: those are assembled into one table,
    together with the unshaded rows around them (lines whose words fall into the same columns) and the bold header
    lines just above them, read word by word into the columns."""
    try:
        # a one-column "table" is a frame or box; white text backgrounds are not rules or bands
        found = [t for t in page.filter(_drawn).find_tables() if len(_cols(t)) >= 2]
    except Exception:  # malformed drawing operators: no ruled tables on this page
        return []
    if not found:
        return []
    found.sort(key=lambda t: t.bbox[1])
    words = page.extract_words(extra_attrs=["fontname"])
    body = [l for l in lines if 0.12 * page.height < l["top"] < 0.86 * page.height]  # not the header/footer zones
    lw = {id(l): _line_words(l, words) for l in body}

    def between(a: float, b: float) -> list[dict]:
        return [l for l in body if a < (l["top"] + l["bottom"]) / 2 < b]
    clusters: list[list] = []
    for t in found:
        last = clusters[-1][-1] if clusters else None
        if last is not None and _same_cols(_cols(last), _cols(t)) and \
                all(_alignment(lw[id(l)], _cols(t)) for l in between(last.bbox[3], t.bbox[1])):
            clusters[-1].append(t)
        else:
            clusters.append([t])
    out, used = [], set()
    for group in clusters:
        cols = _cols(group[0])
        top, bottom = group[0].bbox[1], group[-1].bbox[3]
        inside = lambda l: any(t.bbox[1] - 1 <= (l["top"] + l["bottom"]) / 2 <= t.bbox[3] + 1 for t in group)  # noqa: E731
        header_ids = set()
        # upward: unshaded rows and wrapped cells that fit the columns, then bold header lines
        for l in sorted((l for l in body if l["bottom"] <= top + 1 and id(l) not in used), key=lambda l: -l["top"]):
            if top - l["bottom"] > 2.5 * l["size"] or _SECNUM.match(l["text"]):
                break
            if l["bold"] and l["x0"] >= cols[0][0] - 4 and l["x1"] <= cols[-1][1] + 4:
                header_ids.add(id(l))
            elif header_ids or not _alignment(lw[id(l)], cols):
                break
            top = l["top"] - 1
        # downward: unshaded rows and wrapped cells after the last band
        for l in sorted((l for l in body if l["top"] >= bottom - 1 and id(l) not in used), key=lambda l: l["top"]):
            if l["top"] - bottom > 2.5 * l["size"] or _SECNUM.match(l["text"]) and l["bold"] \
                    or not _alignment(lw[id(l)], cols):
                break
            bottom = l["bottom"] + 1
        # lines an earlier table on the page already took are never read twice
        region = [l for l in body if top <= (l["top"] + l["bottom"]) / 2 <= bottom and id(l) not in used]
        if not region:
            continue
        used |= {id(l) for l in region}
        if len(group) == 1 and all(inside(l) for l in region):
            t = group[0]  # one ruled table: its own cell structure (merged cells included)
            rows = t.extract()
            bold = [_bold_share([c for cell in r.cells if cell for c in page.crop(cell).chars]) for r in t.rows[:1]]
            out.append({"kind": "table", "page": page.page_number, "x0": t.bbox[0], "top": t.bbox[1], "x1": t.bbox[2],
                        "bottom": t.bbox[3], "rows": [[" ".join((c or "").split()) for c in r] for r in rows],
                        "header": bool(bold and bold[0]), "assembled": False})
            continue
        bands = [r.bbox for t in group for r in t.rows]
        header, rows, row_band = None, [], None
        for l in sorted(region, key=lambda l: l["top"]):
            cells = _cells(lw[id(l)], cols)
            ymid = (l["top"] + l["bottom"]) / 2
            band = next((b for b in bands if b[1] - 1 <= ymid <= b[3] + 1), None)
            if id(l) in header_ids:  # header lines, merged into one header row
                header = [(h + " " + c).strip() for h, c in zip(header or [""] * len(cols), cells)]
            elif rows and (band is not None and band == row_band or band is None and not cells[0]):
                rows[-1] = [(a + " " + b).strip() if b else a for a, b in zip(rows[-1], cells)]  # wrapped cell
            else:
                rows.append(cells)
                row_band = band
        out.append({"kind": "table", "page": page.page_number, "x0": cols[0][0] - 3, "top": top, "x1": cols[-1][1] + 3,
                    "bottom": bottom, "rows": ([header] if header else []) + rows, "header": header is not None,
                    "assembled": True})
    return out


def _images(page) -> list[dict]:
    return [{"kind": "figure", "page": page.page_number, "x0": im["x0"], "top": im["top"], "x1": im["x1"],
             "bottom": im["bottom"]} for im in page.images if (im["x1"] - im["x0"]) > 40 and (im["bottom"] - im["top"]) > 40]


def extract(path: str | Path, label_pattern: str | None) -> dict:
    """Pages of one PDF: body lines (furniture removed), ruled tables, images and printed page labels."""
    import pdfplumber
    from .pdfsource import bookmarks
    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            lines = _page_lines(page, i)
            pages.append({"page": i, "width": float(page.width), "height": float(page.height),
                          "lines": lines, "tables": _tables(page, lines), "images": _images(page)})
    furn = _furniture(pages)
    lab = re.compile(label_pattern) if label_pattern else None
    img_keys = collections.Counter((round(im["x0"]), round(im["top"])) for p in pages for im in p["images"])
    for p in pages:
        p["furniture"] = [ln for i, ln in enumerate(p["lines"]) if (p["page"], i) in furn]
        p["lines"] = [ln for i, ln in enumerate(p["lines"]) if (p["page"], i) not in furn]
        p["label"] = None
        for ln in p["furniture"]:
            m = lab.search(ln["text"]) if lab else None
            if m:
                p["label"] = m.group(1)
        # repeated images (header logos) are furniture too
        p["images"] = [im for im in p["images"] if img_keys[(round(im["x0"]), round(im["top"]))] < max(2, len(pages) // 2)]
    return {"pages": pages, "outline": bookmarks(str(path))}


# ------------------------------------------------------------------ document conversion

class _Doc:
    def __init__(self, art: dict, ex: dict, issues: list):
        self.art, self.ex, self.issues = art, ex, issues
        self.unit_id, self.url = art["unit_id"], art["url"]
        self.elements: list[dict] = []
        self.counters: dict = {}
        self.stats = collections.Counter()
        self.labels = {p["page"]: p["label"] for p in ex["pages"]}
        body = [ln for p in ex["pages"] for ln in p["lines"]]
        sizes = collections.Counter()
        for ln in body:
            sizes[ln["size"]] += ln["n"]
        self.body_size = sizes.most_common(1)[0][0] if sizes else 12.0
        margins = collections.Counter(round(ln["x0"]) for ln in body if ln["size"] == self.body_size)
        self.margin = margins.most_common(1)[0][0] if margins else 72
        pitches = collections.Counter()
        for p in ex["pages"]:
            ls = p["lines"]
            for a, b in zip(ls, ls[1:]):
                if a["size"] == b["size"] == self.body_size and abs(a["x0"] - b["x0"]) < 3 and 0 < b["top"] - a["top"] < 3 * self.body_size:
                    pitches[round(b["top"] - a["top"], 1)] += 1
        self.pitch = pitches.most_common(1)[0][0] if pitches else self.body_size * 1.2

    # ---------------------------------------------------------- elements
    def _next_id(self, parent_id: str, prefix: str) -> str:
        k = (parent_id, prefix)
        self.counters[k] = self.counters.get(k, 0) + 1
        return f"{parent_id}.{prefix}{self.counters[k]:02d}"

    def span(self, item: dict | None, anchor: str) -> dict:
        page = item["page"] if item else None
        return {"locator_type": "section_anchor", "page": page, "page_label": self.labels.get(page) if page else None,
                "bbox": [round(item["x0"], 1), round(item["top"], 1), round(item["x1"], 1), round(item["bottom"], 1)]
                if item else None, "coordinate_convention": "pdf_points_top_left_origin",
                "url": f"{self.url}#{anchor}", "anchor": anchor, "unit": self.unit_id, "role": "text",
                "mapping_status": "pdf_only"}

    def add(self, eid: str, etype: str, parent: dict | None, spans: list[dict], *, text: str = "", md: str = "",
            attrs: dict | None = None, anchor: str | None = None) -> dict:
        el = {"id": eid, "type": etype, "order": 0, "parent_id": parent["id"] if parent else None, "children": [],
              "anchor": anchor, "text": text, "md": md, "attrs": attrs or {}, "source_spans": spans,
              "mapping_status": "aggregate" if etype in ("chapter", "section", "list") else "pdf_only",
              "extraction": {"method": "pdf_layout", "route": ROUTE, "variables": []},
              "content_hash": _sha(text if text else json.dumps(attrs or {}, sort_keys=True, ensure_ascii=False)),
              "rights": {"state": "pending_review", "rule": "unassigned"}}
        if parent is not None:
            parent["children"].append(eid)
        self.elements.append(el)
        return el

    # ---------------------------------------------------------- headings
    def _outline(self) -> list[dict]:
        """Outline entries that can be printed body headings.
        * Front matter: entries before the first body entry that are on the cover page, repeat the document title,
          or name a notice or the contents ("Disclaimer...", "Table of Contents") stay in the front matter.
        * Bookmark-only groupings (a glossary's single letters) and bookmarks on whole paragraphs (a "Fact: ..."
          answer) are not headings.
        Levels are renumbered so that the top body level is 0. The rest are counted, not matched."""
        title_key = _key(self.art.get("title") or "")
        out, body, dropped = [], False, []  # dropped: levels of bookmark-only groupings above the current entry
        for b in self.ex["outline"]:
            k = _key(b.get("title") or "")
            if not k:
                continue
            while dropped and dropped[-1] >= b["level"]:
                dropped.pop()
            b = dict(b, level=b["level"] - len(dropped))  # children of a dropped grouping move up a level
            if not body and not _SECNUM.match(b["title"]) and (
                    b.get("page") == 1 or _FRONT_MATTER_TITLES.match(b["title"].strip())
                    or title_key and title_key in k and len(k) <= len(title_key) + 12):
                self.stats["outline_entries_front_matter"] += 1
                continue
            body = True
            if len(k) <= 2 or len(b["title"]) > 150:
                self.stats["outline_entries_not_headings"] += 1
                if len(k) <= 2:
                    dropped.append(b["level"] + len(dropped))
                continue
            out.append(dict(b, key=k))
        top = min((b["level"] for b in out), default=0)
        return [dict(b, level=b["level"] - top) for b in out]

    def _match_outline(self, stream: list[dict], i: int, entries: list[dict], ptr: int) -> tuple[int, int, int] | None:
        """(entry index, lines consumed, level) when stream[i] starts the heading of one of the next outline entries."""
        ln = stream[i]
        for j in range(ptr, min(ptr + 4, len(entries))):
            e = entries[j]
            if e.get("page") and abs(e["page"] - ln["page"]) > 1:
                continue
            text, n = "", 0
            while i + n < len(stream) and stream[i + n]["kind"] == "line" and n < 5:
                text += stream[i + n]["text"]
                n += 1
                k = _key(text)
                if k == e["key"]:
                    return j, n, e["level"] + 2
                if not e["key"].startswith(k):
                    break
        return None

    def _numbered_heading(self, ln: dict, outline_depth: int | None) -> int | None:
        if not ln["bold"] or len(ln["text"]) > 110 or ln["size"] < self.body_size - 0.5:
            return None
        m = _SECNUM.match(ln["text"])
        if m:
            depth = m.group(1).count(".") + 1
            if outline_depth is None or depth > outline_depth:
                return depth + 1
            return None
        # without an outline, an unnumbered heading must also be printed larger than the body text (other bold lines,
        # such as table headers or run-in labels, stay bold paragraphs)
        if outline_depth is None and len(ln["text"]) <= 90 and ln["size"] >= self.body_size + 1.5 \
                and not ln["text"].endswith((".", ",", ";", ":")) and not _ENUM.match(ln["text"]) \
                and ln["text"][:1] not in _HARD_BULLETS:
            return 2
        return None

    # ---------------------------------------------------------- build
    def build(self) -> dict:
        a = self.art
        title = a.get("title") or a["unit_id"]
        meta = {k: a.get(k) for k in ("document_number", "version", "document_status", "document_date", "landing_page")
                if a.get(k)}
        unit = self.add(self.unit_id, "chapter", None, [self.span(None, "page=1")], anchor=self.unit_id,
                        attrs={"title": title, "level": 1, "number": None, "url": self.url, "source_id": a["source_id"],
                               "git_commit_id": None, "updated_at": a.get("document_date"), **meta})
        self.add(f"{self.unit_id}.h01", "heading", unit, [self.span(None, "page=1")], text=title, md=md_escape(title),
                 attrs={"title": title, "level": 1})
        stream = []
        for p in self.ex["pages"]:
            items = p["lines"] + p["tables"] + p["images"]
            tables = p["tables"]
            kept = []
            for it in items:
                if it["kind"] == "line" and any(t["x0"] - 2 <= (it["x0"] + it["x1"]) / 2 <= t["x1"] + 2
                                                and t["top"] - 2 <= (it["top"] + it["bottom"]) / 2 <= t["bottom"] + 2
                                                for t in tables):
                    self.stats["lines_in_tables"] += 1
                    self.stats["table_line_chars"] += len(it["text"].replace(" ", ""))
                    continue
                kept.append(it)
            stream += sorted(kept, key=lambda it: (it["top"], it["x0"]))
        entries = self._outline()
        outline_depth = (max(e["level"] for e in entries) + 1) if entries else None
        ptr = 0
        fm = self.add(f"{self.unit_id}.front-matter", "section", unit, [self.span(None, "page=1")],
                      anchor=f"{self.unit_id}--front-matter",
                      attrs={"title": "Cover page and notices", "level": 2, "number": None, "anchor": "page=1",
                             "front_matter": True})
        self.sections = [fm]
        stack: list[tuple[int, dict]] = [(1, unit), (2, fm)]
        cur = {"para": None, "last": None, "lists": []}  # open paragraph, previous line, open list contexts
        i = 0
        while i < len(stream):
            it = stream[i]
            if it["kind"] == "line":
                hit = self._match_outline(stream, i, entries, ptr) if entries else None
                level, n = (hit[2], hit[1]) if hit else (self._numbered_heading(it, outline_depth), 1)
                if hit:
                    for skipped in entries[ptr:hit[0]]:
                        self.issues.append({"severity": "minor", "kind": "outline_entry_without_heading",
                                            "location": self.unit_id, "message": f"outline entry {skipped['title']!r} "
                                            "matched no printed heading; its text stays under the previous section"})
                    ptr = hit[0] + 1
                if level and not hit and stack[-1][1] is fm and (not _SECNUM.match(it["text"]) or _TOC_LINE.search(it["text"])):
                    level = None  # before the first section only outline or numbered headings open one (not TOC lines)
                if level:
                    if stack[-1][1] is fm and len(stack) == 2:
                        stack = [(1, unit)]
                    heading = " ".join(stream[i + k]["text"] for k in range(n))
                    self._close(cur)
                    while stack[-1][0] >= level:
                        stack.pop()
                    stack.append((level, self._section(stack[-1][1], heading, level, it)))
                    i += n
                    continue
            sec = stack[-1][1]
            if it["kind"] == "table":
                self._close(cur)
                self._table(it, sec)
            elif it["kind"] == "figure":
                self._close(cur)
                self.add(self._next_id(sec["id"], "f"), "figure", sec, [self.span(it, sec["attrs"]["anchor"])],
                         attrs={"alt": "", "caption": None, "src_url": None, "captured": False, "label": None,
                                "number": None})
                self.stats["figures_not_captured"] += 1
            else:
                self._line(it, sec, cur)
            i += 1
        self._close(cur)
        if not fm["children"]:  # nothing printed before the first section
            unit["children"].remove(fm["id"])
            self.elements.remove(fm)
            self.sections.remove(fm)
        for p in self.ex["pages"]:
            self.stats["furniture_lines"] += len(p["furniture"])
        return unit

    def _section(self, parent: dict, title: str, level: int, ln: dict) -> dict:
        m = _SECNUM.match(title)
        base = ("s" + m.group(1).replace(".", "-")) if m else _slug(title)
        sid = f"{self.unit_id}.{base}"
        while any(e["id"] == sid for e in self.elements):
            sid += "-x"
        anchor = f"page={ln['page']}"
        sec = self.add(sid, "section", parent, [self.span(ln, anchor)], anchor=f"{self.unit_id}--{sid.split('.', 1)[1]}",
                       attrs={"title": title, "level": level, "number": m.group(1) if m else None, "anchor": anchor})
        self.add(f"{sid}.h01", "heading", sec, [self.span(ln, anchor)], text=title, md=md_escape(title),
                 attrs={"title": title, "level": level})
        self.sections.append(sec)
        return sec

    def _table(self, t: dict, sec: dict) -> None:
        anchor = sec["attrs"]["anchor"]
        width = max(len(r) for r in t["rows"])
        rows = []
        for n, r in enumerate(t["rows"]):
            rows.append({"section": "thead" if n == 0 and t["header"] else "tbody", "source_spans": [self.span(t, anchor)],
                         "verification": "pdf_layout",
                         "cells": [{"text": c, "md": md_escape(c), "header": n == 0 and t["header"], "colspan": 1,
                                    "rowspan": 1, "halign": None} for c in r + [""] * (width - len(r))]})
        text = "\n".join(" | ".join(c["text"] for c in r["cells"]) for r in rows)
        self.add(self._next_id(sec["id"], "t"), "table", sec, [self.span(t, anchor)], text=text,
                 attrs={"rows": rows, "complex": False, "caption": None, "label": None, "number": None,
                        **({"assembled_from_layout": True} if t.get("assembled") else {})})
        self.stats["tables"] += 1
        self.stats["tables_assembled_from_row_bands"] += 1 if t.get("assembled") else 0

    # ---------------------------------------------------------- paragraphs and lists
    def _marker(self, ln: dict) -> tuple[str, bool, str] | None:
        """(marker, ordered, rest of the text) when the line starts a list item."""
        t = ln["text"]
        if t[:1] in _HARD_BULLETS:
            return t[:1], False, t[1:].strip()
        if t[:1] in _SOFT_BULLETS and t[1:2] == " " and ln["x0"] > self.margin + 4:
            return t[:1], False, t[2:].strip()
        m = _ENUM.match(t)
        if m and not ln["bold"] and len(t) > len(m.group(0)):
            return m.group(0).strip(), True, t[len(m.group(0)):].strip()
        return None

    def _gap(self, prev: dict | None, ln: dict) -> bool:
        """True when ln does not continue the block that prev ends: a wide gap, a new page, or a size change."""
        if prev is None or prev["page"] != ln["page"]:
            return True
        return ln["top"] - prev["top"] > self.pitch * 1.3 or abs(ln["size"] - prev["size"]) > 0.6

    def _near(self, prev: dict | None, ln: dict) -> bool:
        """Same page, same size, within 2.2 line pitches: close enough to continue an unfinished sentence."""
        return prev is not None and prev["page"] == ln["page"] and abs(ln["size"] - prev["size"]) <= 0.6 \
            and ln["top"] - prev["top"] <= self.pitch * 2.2

    def _line(self, ln: dict, sec: dict, cur: dict) -> None:
        prev, para, lists = cur["last"], cur["para"], cur["lists"]
        new_page = prev is not None and prev["page"] != ln["page"]
        mk = self._marker(ln)
        if mk:
            self._flush(cur)
            marker, ordered, rest = mk
            # nest by the marker's indent: deeper than the open item's marker -> a sub-list of that item
            while lists and ln["x0"] < lists[-1]["x0"] - 3:
                lists.pop()
            if lists and abs(ln["x0"] - lists[-1]["x0"]) <= 3 and lists[-1]["ordered"] == ordered:
                ctx = lists[-1]
            else:
                if lists and abs(ln["x0"] - lists[-1]["x0"]) <= 3:  # same indent, other kind: a new sibling list
                    lists.pop()
                parent = lists[-1]["item"] if lists else sec
                lst = self.add(self._next_id(parent["id"], "l"), "list", parent, [self.span(ln, sec["attrs"]["anchor"])],
                               attrs={"ordered": ordered, "style": None, "title": None, "start": None})
                ctx = {"x0": ln["x0"], "ordered": ordered, "list": lst, "item": None, "text_x0": None}
                lists.append(ctx)
            item = self.add(self._next_id(ctx["list"]["id"], "i"), "list_item", ctx["list"],
                            [self.span(ln, sec["attrs"]["anchor"])], text=rest, md=md_escape(rest),
                            attrs={"checked": None, "links": [], "marker": marker})
            ctx["item"], ctx["lines"] = item, [ln]
            cur["last"] = ln
            self.stats["list_items"] += 1
            return
        # continuation of the open list item: indented past its marker, and no wide gap or an unfinished sentence
        # (list lines are often spaced more loosely than body text; a page break is fine)
        if lists and lists[-1]["item"] is not None and ln["x0"] > lists[-1]["x0"] + 3 and \
                (not self._gap(prev, ln) or not lists[-1]["item"]["text"].endswith(_CLOSING)
                 and (new_page or self._near(prev, ln))):
            self._extend(lists[-1]["item"], ln)
            lists[-1]["lines"].append(ln)
            cur["last"] = ln
            return
        if lists and ln["x0"] > lists[-1]["x0"] + 3 and lists[-1]["item"] is not None:
            # an indented paragraph after a gap: a further paragraph of the open item
            self._flush(cur)
            cur["para"] = {"parent": lists[-1]["item"], "lines": [ln], "sec": sec}
            cur["last"] = ln
            return
        if para is not None and not mk and self._continues(para, prev, ln, new_page):
            para["lines"].append(ln)
            cur["last"] = ln
            return
        self._flush(cur)
        if lists and ln["x0"] <= lists[0]["x0"] + 3:
            lists.clear()
        cur["para"] = {"parent": lists[-1]["item"] if lists else sec, "lines": [ln], "sec": sec}
        cur["last"] = ln

    def _continues(self, para: dict, prev: dict | None, ln: dict, new_page: bool) -> bool:
        if prev is None:
            return False
        last = para["lines"][-1]
        if new_page:  # a paragraph runs on to the next page when its last line has no closing punctuation
            return not last["text"].endswith(_CLOSING) and abs(ln["x0"] - para["lines"][0]["x0"]) < 20 \
                and abs(ln["size"] - last["size"]) <= 0.6
        if self._gap(prev, ln):  # a loosely spaced line still continues an unfinished sentence that runs on in lower case
            return not last["text"].endswith(_CLOSING) and ln["text"][:1].islower() and self._near(prev, ln) \
                and abs(ln["x0"] - last["x0"]) <= 3
        if last["bold"] != ln["bold"]:  # a bold label line ("Procedures:") and the plain text after it are two blocks
            return False
        first = para["lines"][0]
        same_margin = abs(ln["x0"] - last["x0"]) <= 3
        first_line_indent = len(para["lines"]) == 1 and first["x0"] - ln["x0"] > 8
        return same_margin or first_line_indent

    @staticmethod
    def _join(a: str, b: str) -> str:
        if a.endswith("-") and b[:1].isalpha():
            return a + b  # line ends in a hyphen: joined without a space, hyphen kept
        return f"{a} {b}" if a else b

    def _extend(self, el: dict, ln: dict) -> None:
        if el["text"].endswith("-") and ln["text"][:1].isalpha():
            self.stats["line_end_hyphen_joins"] += 1
        el["text"] = self._join(el["text"], ln["text"])
        el["md"] = md_escape(el["text"])
        el["content_hash"] = _sha(el["text"])
        sp = el["source_spans"]
        if sp and sp[-1]["page"] == ln["page"]:
            b = sp[-1]["bbox"]
            sp[-1]["bbox"] = [round(min(b[0], ln["x0"]), 1), b[1], round(max(b[2], ln["x1"]), 1), round(ln["bottom"], 1)]
        else:
            sp.append(self.span(ln, sp[-1]["anchor"] if sp else "page=1"))

    def _flush(self, cur: dict) -> None:
        para = cur["para"]
        cur["para"] = None
        if not para:
            return
        lines = para["lines"]
        text = ""
        for ln in lines:
            if text.endswith("-") and ln["text"][:1].isalpha():
                self.stats["line_end_hyphen_joins"] += 1
            text = self._join(text, ln["text"])
        anchor = para["sec"]["attrs"]["anchor"]
        spans = []
        for ln in lines:  # one span per page the paragraph is printed on
            if spans and spans[-1]["page"] == ln["page"]:
                b = spans[-1]["bbox"]
                spans[-1]["bbox"] = [round(min(b[0], ln["x0"]), 1), b[1], round(max(b[2], ln["x1"]), 1), round(ln["bottom"], 1)]
            else:
                spans.append(self.span(ln, anchor))
        small = all(ln["size"] < self.body_size - 1.5 for ln in lines)
        bold = all(ln["bold"] for ln in lines) and len(text) <= 120  # a bold label line ("Redistribution Policy")
        self.add(self._next_id(para["parent"]["id"], "p"), "paragraph", para["parent"], spans, text=text,
                 md=f"**{md_escape(text)}**" if bold else md_escape(text),
                 attrs={"links": [], **({"small_print": True} if small else {})})
        self.stats["paragraphs"] += 1

    def _close(self, cur: dict) -> None:
        self._flush(cur)
        cur["lists"].clear()
        cur["last"] = None


def run(articles: list[dict], recipe: dict, meta: dict, log=print) -> dict:
    """``articles``: resolved lock records (hash-verified) with ``unit_id``, ``url``, ``path`` and the recipe's
    document metadata (``title``, ``document_number``, ``version``, ``document_status``, ``document_date``,
    ``landing_page``). The recipe's ``document_status`` (e.g. "published") applies to documents that give none."""
    label_pattern = (recipe.get("printed_labels") or {}).get("pattern")
    articles = [{**a, "document_status": a.get("document_status") or recipe.get("document_status")} for a in articles]
    root = {"id": "doc", "type": "document", "order": 0, "parent_id": None, "children": [], "anchor": None, "text": "",
            "md": "", "attrs": {}, "source_spans": [], "mapping_status": "aggregate", "extraction": None,
            "content_hash": _sha(""), "rights": {"state": "pending_review", "rule": "unassigned"}}
    elements, units, issues = [root], [], []
    totals = collections.Counter()
    for a in articles:
        ex = extract(a["path"], label_pattern)
        d = _Doc(a, ex, issues)
        unit = d.build()
        unit["parent_id"] = "doc"
        root["children"].append(unit["id"])
        elements.extend(d.elements)
        # coverage: every body character is in an element (furniture and table cells are counted separately)
        body = sum(len(ln["text"].replace(" ", "")) for p in ex["pages"] for ln in p["lines"])
        kept = sum(len((e.get("text") or "").replace(" ", "")) for e in d.elements
                   if e["type"] in ("paragraph", "list_item", "heading") and e["id"] != f"{d.unit_id}.h01")
        body -= d.stats["table_line_chars"]  # read as table cells instead
        if body and kept < 0.98 * body:
            issues.append({"severity": "major", "kind": "coverage:text_not_in_elements", "location": d.unit_id,
                           "message": f"{body - kept} of {body} body characters outside tables are in no element"})
        pages_without_label = [p["page"] for p in ex["pages"] if p["label"] is None]
        totals.update(d.stats)
        body_text = "\n".join(ln["text"] for p in ex["pages"] for ln in p["lines"])
        units.append({
            "unit_id": d.unit_id, "title": unit["attrs"]["title"], "url": a["url"], "source_id": a["source_id"],
            "sha256": a["sha256"], "http": a.get("http"), "retrieved_at": a.get("retrieved_at"),
            "git_commit_id": None, "updated_at": a.get("document_date"), "document_id": a.get("document_number"),
            "document_number": a.get("document_number"), "version": a.get("version"),
            "document_status": a.get("document_status"), "landing_page": a.get("landing_page"), "pages": len(ex["pages"]),
            "page_labels": {str(p["page"]): p["label"] for p in ex["pages"]},
            "pages_without_printed_label": pages_without_label, "outline_entries": len(ex["outline"]),
            "body_text_sha256": _sha(body_text), "body_chars": len(body_text),
            "sections": [{"element_id": s["id"], "anchor": s["attrs"]["anchor"], "title": s["attrs"]["title"],
                          "level": s["attrs"]["level"]} for s in d.sections],
            "elements": len(d.elements), "figures_not_captured": d.stats["figures_not_captured"],
            "conversion": dict(d.stats), "body_size": d.body_size, "line_pitch": d.pitch, "left_margin": d.margin,
        })
        log(f"  {d.unit_id}: {len(ex['pages'])} pages, {len(d.sections)} sections, {len(d.elements)} elements, "
            f"{d.stats['tables']} tables, outline {len(ex['outline'])}")
    for n, e in enumerate(elements):
        e["order"] = n
    document = {
        "schema_version": "1.0.0", "document_id": meta["document_id"], "pack_id": meta["pack_id"],
        "pack_version": meta["pack_version"], "edition_id": meta["edition_id"], "title": meta["title"],
        "authors": meta["authors"], "edition_statement": meta["edition_statement"], "route": ROUTE,
        "sources": meta["sources"], "coordinate_convention": "pdf_points_top_left_origin",
        "page_numbering": {"physical": "per document: one-based PDF page", "printed": "per document: read from the page footer"},
        "root_id": "doc", "page_labels": [], "element_count": len(elements), "elements": elements,
    }
    source_map = {"schema_version": "1.0.0", "document_id": meta["document_id"], "route": ROUTE,
                  "snapshot": recipe["inputs"]["snapshot"], "units": units}
    return {"document": document, "source_map": source_map, "issues": issues,
            "qa": {"route-summary.json": {"documents": len(units), "elements": len(elements),
                                          "sections": sum(len(u["sections"]) for u in units), **dict(totals)}},
            "conversion_changes": [
                "Text comes from each PDF's character layer; structure (sections, paragraphs, lists, tables) is "
                "inferred from the page layout and the PDF outline, not from tags. Inline bold and italics are not "
                "carried.",
                "Running headers and footers (the repeated SWGDE banner, the document number, version, cover-page "
                "notice and 'Page N of M') are page furniture: excluded from the reading text; the printed page "
                "number is kept as each element's page label.",
                "Line breaks inside paragraphs and list items are joined with spaces; a line ending in a hyphen is "
                f"joined without a space and the hyphen kept ({totals['line_end_hyphen_joins']} occurrences).",
                "Ruled tables are read cell by cell (pdfplumber line detection); tables drawn without rules are kept "
                "as paragraphs.",
                "Images are not captured: figures keep their page and position; logos repeated on most pages are "
                "furniture.",
                "Text printed before the first section (cover page, disclaimer, redistribution policy) is kept in "
                "each document's 'front-matter' section and excluded from default retrieval.",
            ]}
