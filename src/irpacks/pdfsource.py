"""PDF inventory and positioned text stream.

Extraction uses pdfplumber (pdfminer.six, MIT) for characters, words and image
objects, and pypdfium2 (Apache-2.0/BSD-3-Clause) for page labels, bookmarks and
rendering.  Nothing here interprets or executes document content.

Coordinate convention for every bbox produced by this module:
``[x0, top, x1, bottom]`` in PDF points, origin at the top-left of the page
(pdfplumber convention), y increasing downwards.  Page width/height are stored
with each page record.
"""

from __future__ import annotations

import collections
import re
import time
from pathlib import Path
from typing import Any

from .util import nfkc, read_json, sha256_bytes, write_json

COORDS = "pdf_points_top_left_origin"
CACHE_VERSION = 5


def _round(v: float) -> float:
    return round(float(v), 2)


def bookmarks(pdf_path: str) -> list[dict]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    out = []
    for i, item in enumerate(pdf.get_toc()):
        dest = item.get_dest()
        idx = dest.get_index() if dest is not None else None
        out.append(
            {
                "order": i,
                "level": item.level,
                "title": item.get_title(),
                "page": (idx + 1) if idx is not None else None,
            }
        )
    pdf.close()
    return out


def page_count(pdf_path: str) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    n = len(pdf)
    pdf.close()
    return n


def page_labels(pdf_path: str) -> list[str | None]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    labels = []
    for i in range(len(pdf)):
        lab = pdf.get_page_label(i)
        labels.append(lab if lab else None)
    pdf.close()
    return labels


def render_page(pdf_path: str, page: int, out_path: str | Path, scale: float = 1.5,
                crop: list[float] | None = None) -> dict:
    """Render a 1-based page (optionally cropped to a top-left-origin bbox)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    pg = pdf[page - 1]
    w, h = pg.get_size()
    if crop:
        x0, top, x1, bottom = crop
        # pypdfium2 crop is (left, bottom, right, top) margins in points.
        img = pg.render(scale=scale, crop=(max(0, x0), max(0, h - bottom), max(0, w - x1), max(0, top))).to_pil()
    else:
        img = pg.render(scale=scale).to_pil()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    pdf.close()
    return {"width_px": img.size[0], "height_px": img.size[1], "scale": scale}


def _lines_from_chars(chars: list[dict]) -> list[dict]:
    """Group characters into visual lines (by vertical overlap), left-to-right."""
    rows: list[list[dict]] = []
    for ch in sorted(chars, key=lambda c: (round(c["top"], 1), c["x0"])):
        if rows:
            last = rows[-1]
            ref = last[0]
            mid = (ch["top"] + ch["bottom"]) / 2
            if ref["top"] - 1 <= mid <= ref["bottom"] + 1:
                last.append(ch)
                continue
        rows.append([ch])
    lines = []
    for row in rows:
        row.sort(key=lambda c: c["x0"])
        text = ""
        prev = None
        for ch in row:
            if prev is not None:
                gap = ch["x0"] - prev["x1"]
                if gap > 0.12 * max(ch["size"], 1) and not text.endswith(" ") and ch["text"] != " ":
                    text += " "
            text += ch["text"]
            prev = ch
        lines.append(
            {
                "text": text.strip(),
                "bbox": [
                    _round(min(c["x0"] for c in row)),
                    _round(min(c["top"] for c in row)),
                    _round(max(c["x1"] for c in row)),
                    _round(max(c["bottom"] for c in row)),
                ],
                "size": _round(max(c["size"] for c in row)),
            }
        )
    return lines


def extract_pages(pdf_path: str, cache_path: str | Path | None = None, pages: list[int] | None = None,
                  log=print) -> dict:
    """Extract a per-page record with positioned characters, lines and images.

    Returns ``{"pages": [...], "fonts": [...], "elapsed_s": float}``.  When
    ``cache_path`` exists and matches CACHE_VERSION it is reused.
    """
    if cache_path and Path(cache_path).exists():
        data = read_json(cache_path)
        if data.get("cache_version") == CACHE_VERSION and (pages is None or data.get("pages_requested") == pages):
            _add_lines(data)
            data["from_cache"] = True
            return data
    import pdfplumber

    labels = page_labels(pdf_path)
    t0 = time.time()
    fonts: dict[str, int] = {}
    out_pages = []
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        sel = pages or list(range(1, n + 1))
        for pno in sel:
            page = pdf.pages[pno - 1]
            chars = []
            for c in page.chars:
                fi = fonts.setdefault(c.get("fontname", ""), len(fonts))
                chars.append(
                    {
                        "text": c["text"],
                        "x0": _round(c["x0"]),
                        "top": _round(c["top"]),
                        "x1": _round(c["x1"]),
                        "bottom": _round(c["bottom"]),
                        "size": _round(c.get("size", 0)),
                        "font": fi,
                        "upright": bool(c.get("upright", True)),
                    }
                )
            images = []
            for im in page.images:
                try:
                    raw = im["stream"].get_rawdata() or b""
                except Exception:  # pragma: no cover - defensive
                    raw = b""
                images.append(
                    {
                        "bbox": [_round(im["x0"]), _round(im["top"]), _round(im["x1"]), _round(im["bottom"])],
                        "srcsize": list(im.get("srcsize") or []),
                        "name": str(im.get("name", "")),
                        "raw_sha256": sha256_bytes(raw) if raw else None,
                        "raw_bytes": len(raw),
                    }
                )
            links = []
            for h in getattr(page, "hyperlinks", []) or []:
                uri = h.get("uri")
                if uri:
                    links.append({"uri": uri, "bbox": [_round(h["x0"]), _round(h["top"]), _round(h["x1"]), _round(h["bottom"])]})
            rec = {
                "links": links,
                "page": pno,
                "label": labels[pno - 1],
                "width": _round(page.width),
                "height": _round(page.height),
                "rotation": int(page.rotation or 0),
                "chars": chars,
                "images": images,
                "n_rects": len(page.rects),
                "n_curves": len(page.curves),
                "n_lines": len(page.lines),
            }
            out_pages.append(rec)
            if hasattr(page, "flush_cache"):
                page.flush_cache()
            if pno % 100 == 0:
                log(f"  extracted page {pno}/{n}")
    data = {
        "cache_version": CACHE_VERSION,
        "coords": COORDS,
        "pages_requested": pages,
        "fonts": [f for f, _ in sorted(fonts.items(), key=lambda kv: kv[1])],
        "pages": out_pages,
        "elapsed_s": round(time.time() - t0, 1),
    }
    if cache_path:
        write_json(cache_path, data)
    _add_lines(data)
    data["from_cache"] = False
    return data


def _add_lines(data: dict) -> None:
    """Derive visual lines from cached characters (not cached: cheap, tunable)."""
    for rec in data["pages"]:
        rec["lines"] = _lines_from_chars([c for c in rec["chars"] if c["text"].strip()])


# ---------------------------------------------------------------------------
# Running header/footer ("furniture") detection.

def _furniture_pattern(text: str, label: str | None) -> str:
    t = nfkc(text)
    if label:
        t = re.sub(rf"(?<![\w]){re.escape(label)}(?![\w])", "{L}", t)
    t = re.sub(r"\d+", "#", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def detect_furniture(pages: list[dict], band: float = 0.075, min_repeat: int = 3) -> dict[int, list[dict]]:
    """Return {page: [furniture line dicts]} for repeated header/footer lines.

    A line is furniture when it lies in the top or bottom ``band`` of the page
    and either (a) its digit/label-normalised pattern repeats on at least
    ``min_repeat`` pages, or (b) it consists only of the page's printed label.
    """
    cand: dict[int, list[tuple[dict, str]]] = {}
    freq: collections.Counter[str] = collections.Counter()
    for p in pages:
        h = p["height"]
        for ln in p["lines"]:
            top, bottom = ln["bbox"][1], ln["bbox"][3]
            if bottom <= h * band or top >= h * (1 - band):
                pat = _furniture_pattern(ln["text"], p["label"])
                cand.setdefault(p["page"], []).append((ln, pat))
                freq[pat] += 1
    result: dict[int, list[dict]] = {}
    for p in pages:
        for ln, pat in cand.get(p["page"], []):
            t = ln["text"].strip()
            lab = p["label"]
            only_label = lab is not None and t == lab
            # running footer carrying the folio, e.g. "24 | Chapter 2 | Introduction"
            folio = lab is not None and (re.match(rf"^{re.escape(lab)}\s*[|·•\-–—]", t) is not None or
                                         re.search(rf"[|·•\-–—]\s*{re.escape(lab)}$", t) is not None)
            if freq[pat] >= min_repeat or only_label or folio:
                result.setdefault(p["page"], []).append(
                    {"text": ln["text"], "bbox": ln["bbox"], "pattern": pat}
                )
    return result


def char_in_bbox(ch: dict, bbox: list[float], tol: float = 0.5) -> bool:
    cx = (ch["x0"] + ch["x1"]) / 2
    cy = (ch["top"] + ch["bottom"]) / 2
    return bbox[0] - tol <= cx <= bbox[2] + tol and bbox[1] - tol <= cy <= bbox[3] + tol


def classify_page(p: dict, furniture: list[dict]) -> dict:
    """Coverage class for the page ledger (not a content judgement)."""
    body_chars = sum(
        1 for c in p["chars"] if c["text"].strip() and not any(char_in_bbox(c, f["bbox"]) for f in furniture)
    )
    has_images = bool(p["images"])
    vector = p["n_rects"] + p["n_curves"] + p["n_lines"]
    if body_chars == 0 and not has_images and vector == 0:
        cls = "no_extractable_content"  # must be visually confirmed before calling it blank
    elif body_chars == 0 and (has_images or vector):
        cls = "visual_only"
    elif has_images:
        cls = "text_and_images"
    else:
        cls = "text"
    return {"body_chars": body_chars, "class": cls, "vector_objects": vector}
