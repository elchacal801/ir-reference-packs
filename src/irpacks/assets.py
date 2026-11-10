"""Figure, formula and complex-table assets with source locations.

* Figures: the original image bytes embedded in the structured candidate are
  written unchanged when the matching PDF image object has identical pixel
  dimensions; a downscaled pixel comparison against the PDF's own image object
  is recorded as additional evidence.  Otherwise the PDF region is rendered.
* Formulas (rendered as images in the PDF) and complex tables: the PDF region
  is rendered so the printed visual remains available next to the text form.
"""

from __future__ import annotations

import io
from pathlib import Path

from .pdfsource import render_page
from .util import sha256_bytes, sha256_file

EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif", "image/svg+xml": "svg"}


def _pdf_image_pixels(pdf, page_no: int, bbox: list[float]):
    """Return a PIL image of the PDF image object whose bbox matches (pypdfium2)."""
    import pypdfium2 as pdfium

    page = pdf[page_no - 1]
    h = page.get_height()
    best = None
    for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=4):
        l, b, r, t = obj.get_bounds()
        cand = [l, h - t, r, h - b]
        d = sum(abs(x - y) for x, y in zip(cand, bbox))
        if best is None or d < best[0]:
            best = (d, obj)
    if best is None or best[0] > 4:
        return None
    try:
        return best[1].get_bitmap(render=False).to_pil()
    except Exception:
        return None


def _similarity(a, b) -> float | None:
    """1 - mean absolute difference of 32x32 grayscale thumbnails (0..1)."""
    try:
        from PIL import Image
        x = a.convert("L").resize((32, 32), Image.BILINEAR)
        y = b.convert("L").resize((32, 32), Image.BILINEAR)
        px, py = list(x.getdata()), list(y.getdata())
        return round(1 - sum(abs(p - q) for p, q in zip(px, py)) / (255 * len(px)), 4)
    except Exception:
        return None


def write_assets(doc: dict, images: dict[str, tuple[str, bytes]], pdf_path: str, out_dir: Path,
                 log=print) -> dict[str, dict]:
    """Write assets under ``out_dir/assets`` and return {element_id: asset record}."""
    import pypdfium2 as pdfium
    from PIL import Image

    adir = out_dir / "assets"
    (adir / "figures").mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(pdf_path)
    out: dict[str, dict] = {}
    for el in doc["elements"]:
        a = el["attrs"]
        if el["type"] == "figure":
            num = a.get("number")
            stem = f"fig-{num:03d}" if num else el["id"].replace(".", "-")
            img = a.get("pdf_image")
            data = images.get(a.get("sha256") or "")
            rec = {"asset_id": stem, "element_id": el["id"], "kind": "figure", "figure_number": num,
                   "caption": a.get("caption"), "alt": a.get("alt"), "source": None}
            if img:
                rec["source"] = {"pdf_page": img["page"], "page_label": next(
                    (s["page_label"] for s in el["source_spans"] if s["page"] == img["page"]), None),
                    "bbox": img["bbox"], "coords": doc["coordinate_convention"],
                    "pdf_image_srcsize": img.get("srcsize"), "pdf_image_raw_sha256": img.get("raw_sha256")}
            if data and img and img.get("dims_match"):
                mime, raw = data
                path = f"assets/figures/{stem}.{EXT.get(mime, 'bin')}"
                (out_dir / path).write_bytes(raw)
                sim = None
                pdf_px = _pdf_image_pixels(pdf, img["page"], img["bbox"])
                if pdf_px is not None:
                    with Image.open(io.BytesIO(raw)) as im:
                        sim = _similarity(im, pdf_px)
                rec.update({"path": path, "mime": mime, "bytes": len(raw), "sha256": sha256_bytes(raw),
                            "pixel_size": a.get("pixel_size"), "origin": "original_bytes_from_structured_candidate",
                            "pdf_pixel_similarity": sim,
                            "evidence": "pixel dimensions equal the PDF image object; similarity of 32x32 grayscale "
                                        "thumbnails recorded"})
            elif img:
                path = f"assets/figures/{stem}-pdf-render.png"
                info = render_page(pdf_path, img["page"], out_dir / path, scale=2.0, crop=img["bbox"])
                rec.update({"path": path, "mime": "image/png", "sha256": sha256_file(out_dir / path),
                            "origin": "rendered_from_pdf_region", "render": info})
            else:
                rec.update({"path": None, "origin": "missing"})
            out[el["id"]] = rec
        elif el["type"] == "math" and a.get("pdf_image"):
            img = a["pdf_image"]
            (adir / "formulas").mkdir(parents=True, exist_ok=True)
            stem = el["id"].replace(".", "-")
            path = f"assets/formulas/{stem}.png"
            info = render_page(pdf_path, img["page"], out_dir / path, scale=3.0, crop=img["bbox"])
            out[el["id"]] = {"asset_id": stem, "element_id": el["id"], "kind": "formula_rendering", "path": path,
                             "mime": "image/png", "sha256": sha256_file(out_dir / path),
                             "origin": "rendered_from_pdf_region", "render": info,
                             "source": {"pdf_page": img["page"], "bbox": img["bbox"],
                                        "coords": doc["coordinate_convention"]}}
        elif el["type"] == "table" and a.get("complex"):
            (adir / "tables").mkdir(parents=True, exist_ok=True)
            num = a.get("number")
            stem = f"table-{num:03d}" if num else el["id"].replace(".", "-")
            paths = []
            spans = [s for s in el["source_spans"] if s["role"] in ("text", "aggregate")]
            for s in spans:
                bb = s["bbox"]
                crop = [max(0, bb[0] - 6), max(0, bb[1] - 6), bb[2] + 6, bb[3] + 6]
                path = f"assets/tables/{stem}-p{s['page']}.png"
                render_page(pdf_path, s["page"], out_dir / path, scale=2.0, crop=crop)
                paths.append(path)
            out[el["id"]] = {"asset_id": stem, "element_id": el["id"], "kind": "table_rendering",
                             "path": paths[0] if paths else None, "paths": paths, "mime": "image/png",
                             "origin": "rendered_from_pdf_region",
                             "source": [{"pdf_page": s["page"], "page_label": s["page_label"], "bbox": s["bbox"]}
                                        for s in spans]}
    pdf.close()
    return out
