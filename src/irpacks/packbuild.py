"""Build a reference pack (local variant) from its recipe.

``build/local/<pack-id>/`` receives the complete local build.  Every export is
generated from ``document.json`` by :func:`emit_outputs`, which the public
exporter reuses on a filtered document.
"""

from __future__ import annotations

import collections
import json
import platform
import re
import shutil
import sqlite3
import sys
import time
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import jsonschema

from . import __version__, assets as assets_mod, chunking, mdrender, packdocs, rights as rights_mod, route_learn, search, sources
from .util import (PROJECT_ROOT, canonical_json, read_json, sha256_file, sha256_text, short_hash, utc_now,
                   write_json, write_jsonl)

SCHEMAS = PROJECT_ROOT / "schemas"
BUILD_LOCAL = PROJECT_ROOT / "build" / "local"
# Packs of separately captured units with a source map: web articles (route_learn profiles) and PDF collections
WEB_ROUTES = tuple(route_learn.PROFILES) + ("pdf_collection_layout",)
CACHE = PROJECT_ROOT / "build" / "cache"
ENGINE = {"name": "ir-reference-packs", "version": __version__}


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def validate_pack_contract(pack_id: str) -> dict:
    pack = sources.load_yaml(sources.pack_dir(pack_id) / "pack.yaml")
    jsonschema.validate(pack, load_schema("pack.schema.json"))
    return pack


def tool_versions() -> dict:
    out = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
           "fts5": search.fts5_available(), "platform": platform.platform()}
    for dist in ("pdfplumber", "pdfminer.six", "pypdfium2", "lxml", "jsonschema", "PyYAML", "pillow"):
        try:
            out[dist] = importlib_metadata.version(dist)
        except importlib_metadata.PackageNotFoundError:
            out[dist] = None
    return out


# ---------------------------------------------------------------------------
# meta helpers


def _labels_text(pairs: list[tuple[int, str | None]]) -> tuple[str, str]:
    pages = [p for p, _ in pairs]
    labels = [l for _, l in pairs]

    def rng(xs):
        xs = [str(x) for x in xs if x is not None]
        if not xs:
            return "n/a"
        if len(xs) == 1:
            return xs[0]
        nums = [int(x) for x in xs] if all(x.isdigit() for x in xs) else None
        if nums and nums == list(range(nums[0], nums[0] + len(nums))):
            return f"{xs[0]}–{xs[-1]}"
        return ", ".join(xs)

    return rng(labels), rng(pages)


def make_citer(pack: dict, els_getter: Callable[[], dict]) -> Callable:
    src = pack["source"]
    scheme = pack["citation"]["scheme"]
    if scheme == "section_anchor":
        return _web_citer(pack, els_getter)
    if scheme == "pdf_collection":
        return _collection_citer(pack, els_getter)

    def cite(heading_path: list[str], spans: list[dict], sec_el: dict) -> dict:
        els = els_getter()
        cur, unit = sec_el, None
        while cur is not None:
            if cur["type"] in ("chapter", "division") or (cur["parent_id"] == "doc"):
                unit = cur
                break
            cur = els.get(cur["parent_id"]) if cur["parent_id"] else None
        unit_title = None
        if unit is not None:
            head = next((els[c] for c in unit["children"] if els[c]["type"] == "heading"), None)
            unit_title = head["text"] if head else unit["attrs"].get("title")
        section = heading_path[-1] if heading_path else None
        pairs = sorted({(s["page"], s.get("page_label")) for s in spans if s.get("page")})
        printed, physical = _labels_text(pairs)
        native = sorted({s["native_locator"] for s in spans if s.get("native_locator")})
        ed = f"{src['edition']} (build {src['edition_identifier']})" if src.get("edition_identifier") and src.get("edition") \
            else (src.get("edition") or src.get("edition_identifier") or "edition n/a")
        page_txt = (f"p. {printed}" if "–" not in printed and "," not in printed else f"pp. {printed}") + \
            (f" (PDF {'p.' if '–' not in physical and ',' not in physical else 'pp.'} {physical})" if physical != "n/a" else "")
        parts = [", ".join(src["authors"]) if src["authors"] else src.get("publisher"), src["title"], ed]
        if unit_title:
            parts.append(unit_title)
        if section and section != unit_title:
            parts.append(f"“{section}”")
        if native:
            parts.append(", ".join(native))
        parts.append(page_txt)
        return {
            "source": src["title"],
            "authors": src["authors"],
            "edition": ed,
            "locator_scheme": scheme,
            "unit": unit_title,
            "section": section,
            "pdf_pages": [p for p, _ in pairs],
            "printed_pages": [l for _, l in pairs],
            "native_locators": native,
            "text": ", ".join(p for p in parts if p),
        }

    return cite


def _web_citer(pack: dict, els_getter: Callable[[], dict]) -> Callable:
    """Citations for unpaginated web articles: article, section, update time, source commit and URL#anchor."""
    src = pack["source"]

    def cite(heading_path: list[str], spans: list[dict], sec_el: dict) -> dict:
        els = els_getter()
        cur = sec_el
        while cur is not None and cur["type"] != "chapter":
            cur = els.get(cur["parent_id"]) if cur["parent_id"] else None
        art = (cur or {}).get("attrs", {})
        section = heading_path[-1] if heading_path and heading_path[-1] != art.get("title") else None
        urls = list(dict.fromkeys(s["url"] for s in spans if s.get("url")))
        anchor_url = (sec_el["source_spans"][0].get("url") if sec_el.get("source_spans") else None) or art.get("url")
        updated = (art.get("updated_at") or "")[:10] or None
        commit = (art.get("git_commit_id") or "")[:7] or None
        captured = None if updated else src.get("retrieval_date")  # no published update time: say when it was captured
        host = urlsplit(anchor_url or "").hostname or "web"
        site = next((p["site"] for p in route_learn.PROFILES.values() if p["host"] == host), host)
        when = "; ".join(x for x in (f"updated {updated}" if updated else None,
                                     f"captured {captured}" if captured else None,
                                     f"source commit {commit}" if commit else None) if x)
        parts = [src.get("publisher") or ", ".join(src["authors"]), f"“{art.get('title')}”" if art.get("title") else None,
                 f"“{section}”" if section else None, f"{site} ({when})" if when else site, anchor_url]
        return {
            "source": src["title"], "authors": src["authors"], "edition": src.get("edition"),
            "locator_scheme": "section_anchor", "unit": art.get("title"), "section": section,
            "pdf_pages": [], "printed_pages": [], "native_locators": [],
            "url": anchor_url, "urls": urls, "updated_at": art.get("updated_at"), "ms_date": art.get("ms_date"),
            "git_commit_id": art.get("git_commit_id"), "document_id": art.get("document_id"),
            "text": ", ".join(p for p in parts if p),
        }

    return cite


def _collection_citer(pack: dict, els_getter: Callable[[], dict]) -> Callable:
    """Citations for a collection of PDF documents (route ``pdf_collection_layout``): document title, number,
    version, status (published or draft) and date, section, printed and PDF pages, and the document URL opened at the
    section's page."""
    src = pack["source"]

    def cite(heading_path: list[str], spans: list[dict], sec_el: dict) -> dict:
        els = els_getter()
        cur = sec_el
        while cur is not None and cur["type"] != "chapter":
            cur = els.get(cur["parent_id"]) if cur["parent_id"] else None
        art = (cur or {}).get("attrs", {})
        section = heading_path[-1] if heading_path and heading_path[-1] != art.get("title") else None
        pairs = sorted({(s["page"], s.get("page_label")) for s in spans if s.get("page")})
        printed, physical = _labels_text(pairs)
        urls = list(dict.fromkeys(s["url"] for s in spans if s.get("url")))
        anchor_url = (sec_el["source_spans"][0].get("url") if sec_el.get("source_spans") else None) or art.get("url")
        ident = ", ".join(x for x in (art.get("document_number"), f"version {art['version']}" if art.get("version") else None,
                                      art.get("document_status"), art.get("updated_at")) if x)
        multi = lambda t: "–" in t or "," in t  # noqa: E731
        page_txt = (f"{'pp.' if multi(printed) else 'p.'} {printed} " if printed != "n/a" else "") + \
            (f"(PDF {'pp.' if multi(physical) else 'p.'} {physical})" if physical != "n/a" else "")
        parts = [src.get("publisher"), f"“{art.get('title')}”" if art.get("title") else None,
                 f"({ident})" if ident else None, f"“{section}”" if section else None, page_txt.strip() or None, anchor_url]
        return {
            "source": src["title"], "authors": src["authors"], "edition": art.get("version") or src.get("edition"),
            "locator_scheme": "pdf_collection", "unit": art.get("title"), "section": section,
            "pdf_pages": [p for p, _ in pairs], "printed_pages": [lab for _, lab in pairs], "native_locators": [],
            "url": anchor_url, "urls": urls, "updated_at": art.get("updated_at"), "ms_date": None,
            "git_commit_id": None, "document_id": art.get("document_number"),
            "text": ", ".join(p for p in parts if p),
        }

    return cite


# Web article sections that are link lists to other articles ("Related content", "Next steps", ...) or a tutorial's
# housekeeping ("Clean up resources"): kept in the pack and its Markdown, left out of default retrieval, where their
# titles and link text outrank the sections that hold the steps. Measured over all web packs: these sections had a
# median of 18-39 words, nearly all of them link text. A recipe can override the pattern (corpus.navigation_titles,
# or null to switch it off).
NAVIGATION_TITLES = (r"^(related content|related articles|related resources|see also|next steps|additional resources|"
                     r"learn more|clean up resources|(extra|additional) incident response playbooks)$")


def make_corpus_rules(recipe: dict) -> dict:
    rules = recipe.get("corpus", {}).get("rules", [])
    default = recipe.get("corpus", {}).get("default", "narrative")
    from .route_learn import PROFILES
    nav = recipe.get("corpus", {}).get("navigation_titles", NAVIGATION_TITLES if recipe.get("route") in PROFILES else None)
    nav_re = re.compile(nav, re.I) if nav else None

    def classify(c: dict, els: dict) -> tuple[str, bool, str | None]:
        cur = els[c["_section"]]
        chain = []
        while cur is not None:
            chain.append(cur["id"])
            cur = els.get(cur["parent_id"]) if cur["parent_id"] else None
        for r in rules:  # within: an element ID on the chain; within_pattern: a regex over those IDs
            if r.get("within") in chain or r.get("within_pattern") and any(re.search(r["within_pattern"], x) for x in chain):
                return r["corpus"], bool(r["include"]), r.get("reason")
        sec = els[c["_section"]]
        if nav_re and sec["type"] == "section" and nav_re.match((sec["attrs"].get("title") or "").strip()):
            return "navigation", False, "links to other articles or a tutorial's clean-up steps (web navigation section)"
        if all(e["type"] == "footnote" for e in c["_els"]):
            return "notes", True, None
        return default, True, None

    return {"classify": classify}


def review_lookup(review: dict) -> Callable:
    pages = {int(r["page"]): r for r in review.get("pages", [])}

    def status_of_pages(pp: list[int]) -> str:
        if not pp:
            return "automated_checks_only"
        n = sum(1 for p in pp if p in pages)
        if n == len(pp):
            return "visually_reviewed_pages"
        return "partially_visually_reviewed" if n else "automated_checks_only"

    return status_of_pages


# ---------------------------------------------------------------------------


def emit_outputs(document: dict, page_map: dict, asset_map: dict, rights: dict, meta: dict, out_dir: Path,
                 *, variant: str, omitted: dict | None = None, review: dict | None = None,
                 chunk_cfg: dict, corpus_rules: dict, pack: dict, log=print) -> dict:
    """Write Markdown, chunks, search index from a canonical document."""
    els = {e["id"]: e for e in document["elements"]}
    review = review or {"pages": []}
    page_status = review_lookup(review)
    elem_pages = {e["id"]: sorted({s["page"] for s in e["source_spans"] if s.get("page")}) for e in document["elements"]}

    def review_status_of(ids: list[str]) -> str:
        pp = sorted({p for i in ids for p in elem_pages.get(i, [])})
        return page_status(pp)

    cmeta = {
        "pack_id": meta["pack_id"], "pack_version": meta["pack_version"], "document_id": meta["document_id"],
        "edition_id": meta["edition_id"], "edition_short": meta["edition_short"],
        "citable_statuses": meta["citable_statuses"], "applicability": pack["applicability"],
        "authority_class": pack["authority_class"], "superseded": pack["superseded"]["state"],
        "distribution_policy": f"{variant}:{pack['distribution']['local_policy'] if variant == 'local' else pack['distribution']['public_policy']}",
        "cite": make_citer(pack, lambda: els), "review_status_of": review_status_of,
    }
    # Markdown
    header = packdocs.book_header(document, pack, meta, variant)
    (out_dir / "book.md").write_text(mdrender.render_book(document, asset_map, omitted=omitted, header=header),
                                     encoding="utf-8")
    cdir = out_dir / "chapters"
    if cdir.exists():
        shutil.rmtree(cdir)
    cdir.mkdir(parents=True)
    unit_md = mdrender.render_units(document, asset_map, omitted=omitted)
    for fname, md in unit_md.items():
        (cdir / fname).write_text(md, encoding="utf-8")
    # Chunks
    chunks = chunking.build_chunks(document, chunk_cfg, assets=asset_map, rights=rights, meta=cmeta,
                                   corpus_rules=corpus_rules, omitted=omitted, variant=variant)
    write_jsonl(out_dir / "chunks.jsonl", chunks)
    # Search
    idx_info = None
    if search.fts5_available():
        idx_info = search.build_index(out_dir / "search.sqlite", [out_dir / "chunks.jsonl"],
                                      {"variant": variant, "pack_id": meta["pack_id"]},
                                      pack_aliases={meta["pack_id"]: (pack.get("retrieval") or {}).get("source_aliases", [])})
    else:
        log("SQLite FTS5 unavailable: search.sqlite not built; use `irpacks search --jsonl` fallback.")
    return {"chunks": chunks, "unit_files": sorted(unit_md), "index": idx_info}


def build_pack(pack_id: str, *, scope: str | None = None, authoritative: str | None = None,
               log=print) -> dict:
    t0 = time.time()
    pack = validate_pack_contract(pack_id)
    pdir = sources.pack_dir(pack_id)
    recipe = sources.load_recipe(pack_id, pack)
    rights_cfg = sources.load_yaml(pdir / pack["rights"])
    review = sources.load_yaml(pdir / "review" / "visual-review.yaml") if (pdir / "review" / "visual-review.yaml").exists() else {"pages": []}
    if recipe["route"] in WEB_ROUTES:
        return _build_web(pack_id, pack, recipe, rights_cfg, pdir, t0, log)
    auth_id = authoritative or recipe["inputs"]["authoritative"]
    auth = sources.resolve(auth_id)
    cand = sources.resolve(recipe["inputs"]["structured_candidate"]) if recipe["inputs"].get("structured_candidate") else None
    edition_short = (pack["source"].get("edition_identifier") or auth["sha256"][:12])
    if authoritative and authoritative != recipe["inputs"]["authoritative"]:
        edition_short = auth.get("edition_stamp") or auth["sha256"][:12]
    out_dir = BUILD_LOCAL / (pack_id + ("-pilot" if scope == "pilot" else ""))
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    cache_dir = CACHE / pack_id / auth["sha256"][:12]
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "pack_id": pack_id, "pack_version": pack["pack_version"],
        "document_id": f"{pack_id}:{edition_short}", "edition_id": edition_short, "edition_short": edition_short,
        "title": pack["source"]["title"], "authors": pack["source"]["authors"],
        "edition_statement": f"{pack['source'].get('edition')} ({edition_short})",
        "route": recipe["route"],
        "sources": {"authoritative": {k: auth[k] for k in ("source_id", "sha256", "size_bytes", "url", "edition_stamp", "local_path")},
                    "structured_candidate": ({k: cand[k] for k in ("source_id", "sha256", "size_bytes", "url", "edition_stamp", "local_path")}
                                             if cand else None)},
        "citable_statuses": sorted(__import__("irpacks.docmodel", fromlist=["CITABLE"]).CITABLE),
    }
    timings = {}
    # ---- route
    t = time.time()
    if recipe["route"] == "asciidoctor_html_verified_against_pdf":
        from . import route_adoc
        routed = route_adoc.run(auth["path"], cand["path"], recipe, meta, cache_dir, scope=scope, log=log)
    elif recipe["route"] == "tagged_pdf_structure":
        from . import route_tagged
        routed = route_tagged.run(auth["path"], recipe, meta, cache_dir, scope=scope, log=log)
    else:
        raise ValueError(f"unknown route {recipe['route']}")
    timings["route_s"] = round(time.time() - t, 1)
    document, page_map = routed["document"], routed["page_map"]
    document["variant"] = "local"
    # ---- rights
    rights = rights_mod.apply_rights(document["elements"], rights_cfg)
    for e in document["elements"]:
        r = rights[e["id"]]
        e["rights"] = {"state": r["state"], "rule": r["rule"], "basis": r.get("basis"), "reason": r.get("reason")}
    # ---- page-map review + coverage status; element-level visual confirmations
    _merge_review(page_map, review)
    # PDF outline, kept so validation can check that every element lies inside its bookmarked unit
    from .pdfsource import bookmarks as pdf_bookmarks
    roles = routed.get("bookmark_roles") or {}  # e.g. outline entries that point at table rows (checklist headings)
    page_map["bookmarks"] = [dict({k: b[k] for k in ("level", "title", "page")},
                                  **({"role": roles[b["order"]]} if b.get("order") in roles else {}))
                             for b in pdf_bookmarks(auth["path"])]
    erev = {r["element_id"]: r for r in review.get("elements", [])}
    for e in document["elements"]:
        r = erev.get(e["id"])
        if r and r.get("result") == "matches" and e["mapping_status"] in ("position_inferred", "unverified"):
            e["mapping_status"] = "visually_verified"
            e["review"] = {k: r.get(k) for k in ("reviewer", "date", "method", "notes")}
    # ---- assets
    t = time.time()
    asset_map = assets_mod.write_assets(document, routed.get("images", {}), auth["path"], out_dir, log=log)
    timings["assets_s"] = round(time.time() - t, 1)
    for e in document["elements"]:
        if e["id"] in asset_map:
            e["attrs"]["asset"] = {k: asset_map[e["id"]].get(k) for k in ("asset_id", "path", "paths", "sha256", "origin")
                                  if asset_map[e["id"]].get(k) is not None}
    write_json(out_dir / "assets" / "assets.json", {"document_id": meta["document_id"], "assets": list(asset_map.values())})
    # ---- canonical files
    write_json(out_dir / "document.json", document)
    write_json(out_dir / "page-map.json", page_map)
    if routed.get("annotations") is not None:
        write_jsonl(out_dir / "annotations.jsonl", routed["annotations"])
    qa_dir = out_dir / "qa"
    qa_dir.mkdir(exist_ok=True)
    for name, obj in routed.get("qa", {}).items():
        write_json(qa_dir / name, obj)
    # ---- exports
    t = time.time()
    em = emit_outputs(document, page_map, asset_map, rights, meta, out_dir, variant="local", review=review,
                      chunk_cfg=recipe["chunking"], corpus_rules=make_corpus_rules(recipe), pack=pack, log=log)
    timings["exports_s"] = round(time.time() - t, 1)
    # ---- schemas copy
    sdir = out_dir / "schemas"
    sdir.mkdir(exist_ok=True)
    for name in ("document.schema.json", "page-map.schema.json", "chunk.schema.json", "manifest.schema.json",
                 "pack.schema.json"):
        shutil.copy(SCHEMAS / name, sdir / name)
    # pack.yaml copy (validated)
    shutil.copy(pdir / "pack.yaml", out_dir / "pack.yaml")
    # ---- validation + QA
    from . import validate as validate_mod
    t = time.time()
    vres = validate_mod.validate_build(out_dir, pack=pack, routed=routed, log=log)
    timings["validate_s"] = round(time.time() - t, 1)
    write_json(qa_dir / "validation.json", vres)
    issues = routed.get("issues", []) + vres["issues"] + _source_defects(pdir)
    write_jsonl(qa_dir / "issues.jsonl", [dict(i, id=f"{pack_id}-I{n:04d}", status=i.get("status", "open"))
                                         for n, i in enumerate(issues, 1)])
    counts = _counts(document, page_map, asset_map, em["chunks"])
    status = packdocs.status_block(pack, counts, vres, issues, scope)
    # ---- docs
    packdocs.write_pack_docs(out_dir, pack, recipe, rights_cfg, meta, counts, vres, issues, status, scope=scope,
                             routed=routed, review=review)
    # ---- manifest (last: hashes every other output)
    timings["total_s"] = round(time.time() - t0, 1)
    import resource
    timings["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    manifest = _manifest(out_dir, pack, recipe, meta, auth, cand, counts, status, timings, scope, routed)
    jsonschema.validate(manifest, load_schema("manifest.schema.json"))
    write_json(out_dir / "manifest.json", manifest)
    log(f"built {pack_id} ({scope or 'full'}) in {timings['total_s']}s -> {out_dir}")
    return {"out_dir": str(out_dir), "counts": counts, "validation": vres, "status": status, "timings": timings}


def _source_defects(pdir: Path) -> list[dict]:
    sd = pdir / "review" / "source-defects.yaml"
    if not sd.exists():
        return []
    return [{"severity": "info", "kind": "suspected_source_defect_preserved",
             "location": d["element_id"] or (f"page:{d['pdf_page']}" if d.get("pdf_page") else "document"),
             "message": d["description"], "status": d.get("status", "documented"),
             "pdf_page": d.get("pdf_page"), "page_label": d.get("page_label"),
             "reviewer": d.get("reviewer"), "remediation": d.get("remediation")}
            for d in sources.load_yaml(sd).get("defects", [])]


def _build_web(pack_id: str, pack: dict, recipe: dict, rights_cfg: dict, pdir: Path, t0: float, log, *,
               arts: list[dict] | None = None, out_dir: Path | None = None) -> dict:
    """Build a pack of unpaginated web articles (route ``learn_html_pages``): one hash-verified input per article,
    a ``source-map.json`` instead of a page map, URL#anchor citations.

    ``arts``/``out_dir`` build a *candidate* snapshot (``irpacks refresh``) from fresh captures into a separate
    directory; the lock, the recipe and ``build/local`` are not touched."""
    from . import route_learn
    candidate = arts is not None
    if arts is None:
        arts = []
        for a in recipe["artifacts"]:
            if a["role"] != "authoritative":
                continue
            rec = sources.resolve(a["source_id"])  # refuses a file whose hash differs from the lock
            arts.append({**{k: v for k, v in a.items() if k not in rec}, **rec, "unit_id": a["unit_id"], "url": a["url"]})
    snapshot = recipe["inputs"]["snapshot"]
    edition_short = snapshot if candidate else (pack["source"].get("edition_identifier") or snapshot)
    out_dir = out_dir or BUILD_LOCAL / pack_id
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    composite = {  # one record standing for the snapshot set, so single-source code paths keep working
        "source_id": f"{pack_id}/snapshot/{snapshot}",
        "sha256": sha256_text("\n".join(f"{a['source_id']} {a['sha256']}" for a in arts)),
        "size_bytes": sum(a.get("size_bytes") or 0 for a in arts), "url": pack["source"].get("url"),
        "edition_stamp": snapshot, "local_path": str(Path(arts[0]["local_path"]).parent.parent) + "/",
        "hash_basis": "sha256 over the sorted lines '<source_id> <sha256>' of the articles below",
        "articles": [{k: a.get(k) for k in ("source_id", "unit_id", "url", "sha256", "size_bytes", "local_path")}
                     for a in arts],
    }
    meta = {
        "pack_id": pack_id, "pack_version": pack["pack_version"], "document_id": f"{pack_id}:{edition_short}",
        "edition_id": edition_short, "edition_short": edition_short, "title": pack["source"]["title"],
        "authors": pack["source"]["authors"], "edition_statement": pack["source"].get("edition") or edition_short,
        "route": recipe["route"], "sources": {"authoritative": composite, "structured_candidate": None},
        "citable_statuses": sorted(__import__("irpacks.docmodel", fromlist=["CITABLE"]).CITABLE),
    }
    timings: dict[str, Any] = {}
    t = time.time()
    if recipe["route"] == "pdf_collection_layout":
        from . import route_layout
        routed = route_layout.run(arts, recipe, meta, log=log)
    else:
        routed = route_learn.run(arts, recipe, meta, log=log)
    timings["route_s"] = round(time.time() - t, 1)
    document, source_map = routed["document"], routed["source_map"]
    document["variant"] = "local"
    rights = rights_mod.apply_rights(document["elements"], rights_cfg)
    for e in document["elements"]:
        r = rights[e["id"]]
        e["rights"] = {"state": r["state"], "rule": r["rule"], "basis": r.get("basis"), "reason": r.get("reason")}
    asset_map: dict = {}
    write_json(out_dir / "assets" / "assets.json", {"document_id": meta["document_id"], "assets": []})
    write_json(out_dir / "document.json", document)
    write_json(out_dir / "source-map.json", source_map)
    qa_dir = out_dir / "qa"
    qa_dir.mkdir(exist_ok=True)
    for name, obj in routed.get("qa", {}).items():
        write_json(qa_dir / name, obj)
    t = time.time()
    em = emit_outputs(document, None, asset_map, rights, meta, out_dir, variant="local", review={"pages": []},
                      chunk_cfg=recipe["chunking"], corpus_rules=make_corpus_rules(recipe), pack=pack, log=log)
    timings["exports_s"] = round(time.time() - t, 1)
    sdir = out_dir / "schemas"
    sdir.mkdir(exist_ok=True)
    for name in ("document.schema.json", "source-map.schema.json", "chunk.schema.json", "manifest.schema.json",
                 "pack.schema.json"):
        shutil.copy(SCHEMAS / name, sdir / name)
    shutil.copy(pdir / "pack.yaml", out_dir / "pack.yaml")
    from . import validate as validate_mod
    t = time.time()
    vres = validate_mod.validate_build(out_dir, pack=pack, routed=routed, log=log)
    timings["validate_s"] = round(time.time() - t, 1)
    write_json(qa_dir / "validation.json", vres)
    issues = routed.get("issues", []) + vres["issues"] + _source_defects(pdir)
    write_jsonl(qa_dir / "issues.jsonl", [dict(i, id=f"{pack_id}-I{n:04d}", status=i.get("status", "open"))
                                         for n, i in enumerate(issues, 1)])
    counts = _counts(document, None, asset_map, em["chunks"], source_map=source_map)
    status = packdocs.status_block(pack, counts, vres, issues, None)
    packdocs.write_pack_docs(out_dir, pack, recipe, rights_cfg, meta, counts, vres, issues, status, scope=None,
                             routed=routed, review={"pages": []})
    timings["total_s"] = round(time.time() - t0, 1)
    import resource
    timings["max_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    manifest = _manifest(out_dir, pack, recipe, meta, None, None, counts, status, timings, None, routed,
                         inputs=[{"source_id": a["source_id"], "role": "authoritative", "sha256": a["sha256"],
                                  "verified": True, "local_path": a["local_path"]} for a in arts])
    jsonschema.validate(manifest, load_schema("manifest.schema.json"))
    write_json(out_dir / "manifest.json", manifest)
    log(f"built {pack_id} in {timings['total_s']}s -> {out_dir}")
    return {"out_dir": str(out_dir), "counts": counts, "validation": vres, "status": status, "timings": timings}


def _merge_review(page_map: dict, review: dict) -> None:
    rev = {int(r["page"]): r for r in review.get("pages", [])}
    for p in page_map["pages"]:
        r = rev.get(p["page"])
        p["visual_review"] = ({k: r.get(k) for k in ("reviewer", "date", "method", "result", "notes", "elements_checked")}
                              if r else None)
        unc = [g for g in p["uncovered_regions"] if g.get("class") == "unclassified"]
        if p["content_class"] == "no_extractable_content":
            p["coverage_status"] = "intentionally_blank" if (r and r.get("result") == "blank_confirmed") else "unresolved"
        elif p["content_class"] == "visual_only":
            p["coverage_status"] = "visual_content" if p["images"] and all(i.get("element_id") for i in p["images"]) \
                or (r and r.get("result") in ("matches", "visual_confirmed")) else "unresolved"
        elif unc:
            p["coverage_status"] = "unresolved"
        elif p["uncovered_key_chars"]:
            p["coverage_status"] = "text_with_explained_gaps"
        else:
            p["coverage_status"] = "text_covered"


def _counts(document, page_map, asset_map, chunks, source_map: dict | None = None) -> dict:
    els = document["elements"]
    tc = collections.Counter(e["type"] for e in els)
    ms = collections.Counter(e["mapping_status"] for e in els if not e["children"])
    inc = [c for c in chunks if c["include_in_default_retrieval"]]
    paged = page_map is not None
    web = {"articles": len(source_map["units"]), "snapshot": source_map.get("snapshot"),
           "figures_not_captured": sum(u.get("figures_not_captured", 0) for u in source_map["units"])} \
        if source_map is not None else {}
    return {
        "pages": page_map["page_count"] if paged else None,
        "pages_by_coverage_status": dict(collections.Counter(p["coverage_status"] for p in page_map["pages"])) if paged else {},
        "pages_by_content_class": dict(collections.Counter(p["content_class"] for p in page_map["pages"])) if paged else {},
        **web,
        "chapters": tc.get("chapter", 0),
        "parts": tc.get("part", 0),
        "divisions": tc.get("division", 0),
        "sections": tc.get("section", 0),
        "elements": len(els),
        "elements_by_type": dict(sorted(tc.items())),
        "leaf_mapping_status": dict(sorted(ms.items())),
        "figures": tc.get("figure", 0),
        "tables": tc.get("table", 0),
        "code_blocks": tc.get("code", 0),
        "footnotes": tc.get("footnote", 0),
        "assets": len(asset_map),
        "asset_files": sum(len(a.get("paths") or ([a["path"]] if a.get("path") else [])) for a in asset_map.values()),
        "chunks": len(chunks),
        "chunks_default_retrieval": len(inc),
        "chunks_by_corpus": dict(collections.Counter(c["corpus"] for c in chunks)),
        "chunk_tokens": {"min": min((c["token_count"] for c in inc), default=0),
                         "max": max((c["token_count"] for c in inc), default=0),
                         "mean": round(sum(c["token_count"] for c in inc) / max(1, len(inc)), 1),
                         "oversize": sum(1 for c in chunks if c["oversize"])},
        "fragments": sum(1 for c in chunks if c["fragment"]),
        "overlap_chunks": sum(1 for c in chunks if c["overlap"]),
    }


def _manifest(out_dir: Path, pack, recipe, meta, auth, cand, counts, status, timings, scope, routed,
              inputs: list[dict] | None = None) -> dict:
    outputs = []
    for p in sorted(out_dir.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            outputs.append({"path": p.relative_to(out_dir).as_posix(), "sha256": sha256_file(p), "bytes": p.stat().st_size})
    settings = {"recipe": recipe, "chunking": recipe.get("chunking")}
    inputs = inputs or [{"source_id": auth["source_id"], "role": "authoritative", "sha256": auth["sha256"],
                         "verified": True, "local_path": auth["local_path"]}]
    if cand:
        inputs.append({"source_id": cand["source_id"], "role": "structured_candidate", "sha256": cand["sha256"],
                       "verified": True, "local_path": cand["local_path"]})
    return {
        "schema_version": "1.0.0",
        "pack_id": meta["pack_id"], "pack_version": meta["pack_version"], "document_id": meta["document_id"],
        "edition_id": meta["edition_id"], "variant": "local", "scope": {"name": scope} if scope else None,
        "engine": ENGINE, "inputs": inputs, "settings": settings,
        "settings_hash": sha256_text(canonical_json(settings)),
        "tools": tool_versions(),
        "tokenizer": __import__("irpacks.tokens", fromlist=["identity"]).identity(),
        "outputs": outputs, "counts": counts, "status": status,
        "run": {"built_at": utc_now(), "timings_s": timings, "pdf_extract_cached": routed.get("from_cache"),
                "python_executable": Path(sys.executable).name},
    }
