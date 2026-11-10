"""Public release candidate: an allowlisted distribution subset of a local build.

Only elements whose rights state is ``redistributable`` keep their content.
Everything else is replaced by an omission record with source locators (a
figure whose image rights are unresolved keeps its caption but not the image).
Markdown, chunks and the search index are regenerated from the filtered
document — the local index is never copied.  The exported bytes are then
checked: allowlisted file types only, no omitted text in any file, no source
PDFs, caches or absolute paths.
"""

from __future__ import annotations

import collections
import json
import re
import shutil
from pathlib import Path

from . import packbuild, packdocs, sources
from .util import PROJECT_ROOT, read_json, read_jsonl, sha256_file, text_key, utc_now, write_json, write_jsonl

PUBLIC_ROOT = PROJECT_ROOT / "dist" / "public"
ALLOWED_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".sqlite", ".png", ".jpg", ".jpeg", ".gif", ".svg"}


def _descendants(els: dict, eid: str) -> list[str]:
    out = []
    for c in els[eid]["children"]:
        out.append(c)
        out += _descendants(els, c)
    return out


def export_public(pack_id: str, log=print) -> dict:
    local = packbuild.BUILD_LOCAL / pack_id
    if not (local / "document.json").exists():
        raise SystemExit(f"build the local pack first: uv run irpacks build {pack_id}")
    pack = sources.load_yaml(local / "pack.yaml")
    pdir = sources.pack_dir(pack_id)
    recipe = sources.load_recipe(pack_id, pack)
    rights_cfg = sources.load_yaml(pdir / pack["rights"])
    review = sources.load_yaml(pdir / "review" / "visual-review.yaml") if (pdir / "review" / "visual-review.yaml").exists() else {"pages": []}
    doc = read_json(local / "document.json")
    page_map = read_json(local / "page-map.json") if (local / "page-map.json").exists() else None
    source_map = read_json(local / "source-map.json") if (local / "source-map.json").exists() else None
    local_assets = {a["element_id"]: a for a in read_json(local / "assets" / "assets.json")["assets"]}
    els = {e["id"]: e for e in doc["elements"]}

    # ---- decide omissions (top-most non-redistributable element) and withheld assets
    omitted: dict[str, dict] = {}
    removed: set[str] = set()
    withheld_assets: dict[str, dict] = {}
    for e in doc["elements"]:
        if e["id"] in removed:
            continue
        st = e["rights"]["state"]
        if st == "redistributable" or e["type"] == "document":  # the root holds no content of its own
            continue
        if e["type"] == "figure" and st == "pending_review":
            withheld_assets[e["id"]] = {"id": e["id"], "state": st, "reason": e["rights"].get("reason"),
                                        "figure_number": e["attrs"].get("number"), "caption": e["attrs"].get("caption"),
                                        "source_spans": [{k: s.get(k) for k in ("page", "page_label", "bbox")}
                                                         for s in e["source_spans"]]}
            continue
        desc = _descendants(els, e["id"])
        omitted[e["id"]] = {"id": e["id"], "type": e["type"], "state": st, "rule": e["rights"].get("rule"),
                            "reason": e["rights"].get("reason"),
                            "source_spans": [{k: s.get(k) for k in ("locator_type", "page", "page_label")}
                                             for s in e["source_spans"]],
                            "omitted_descendants": len(desc)}
        removed.update(desc)

    # ---- filtered document (omitted elements keep identity + locators, lose content)
    pub_elements = []
    for e in doc["elements"]:
        if e["id"] in removed:
            continue
        e2 = json.loads(json.dumps(e))
        if e["id"] in omitted:
            keep = {k: e["attrs"].get(k) for k in ("label", "number", "url", "source_id", "git_commit_id", "updated_at",
                                                   "ms_date", "document_id") if e["attrs"].get(k) is not None}
            if e["type"] in ("part", "chapter", "division", "section"):
                # the title stays as a navigation label (a heading, not the omitted content)
                head = next((els[c] for c in e["children"] if els[c]["type"] == "heading"), None)
                keep["title"] = (head or {}).get("text") or e["attrs"].get("title")
            e2.update({"text": "", "md": "", "children": [], "attrs": keep, "mapping_status": e["mapping_status"]})
            e2["omitted"] = {k: omitted[e["id"]][k] for k in ("state", "rule", "reason")}
        if e["id"] in withheld_assets:
            e2["attrs"].pop("asset", None)
            for k in ("sha256", "bytes", "src_url", "pixel_size"):
                e2["attrs"].pop(k, None)
            e2["asset_withheld"] = {"state": e["rights"]["state"], "reason": e["rights"].get("reason")}
        pub_elements.append(e2)
    kept = {e["id"] for e in pub_elements}
    for i, e in enumerate(pub_elements):
        e["order"] = i
        e["children"] = [c for c in e["children"] if c in kept]
    pdoc = dict(doc)
    pdoc["elements"] = pub_elements
    pdoc["element_count"] = len(pub_elements)
    pdoc["variant"] = "public"
    pdoc["omitted"] = list(omitted.values())
    pdoc["sources"] = {k: ({kk: vv for kk, vv in v.items()} if v else v) for k, v in doc["sources"].items()}

    out = PUBLIC_ROOT / "packs" / pack_id
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    # ---- assets: only for redistributable elements
    pub_assets = {}
    for eid, a in local_assets.items():
        if eid in withheld_assets or eid in omitted or eid in removed or els[eid]["rights"]["state"] != "redistributable":
            continue
        a2 = dict(a)
        for pth in (a.get("paths") or ([a["path"]] if a.get("path") else [])):
            dst = out / pth
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(local / pth, dst)
        pub_assets[eid] = a2
    write_json(out / "assets" / "assets.json", {"document_id": doc["document_id"], "variant": "public",
                                                "assets": list(pub_assets.values()),
                                                "withheld": list(withheld_assets.values())})
    # page map without free-text excerpts of uncovered regions
    pm = json.loads(json.dumps(page_map)) if page_map is not None else None
    for p in (pm or {}).get("pages", []):
        for g in p["uncovered_regions"]:
            g.pop("text", None)
        p["element_ids"] = [x for x in p["element_ids"] if x in kept]
        for im in p["images"]:
            if im.get("element_id") and (im["element_id"] in withheld_assets or im["element_id"] not in kept):
                im["withheld"] = True
    write_json(out / "document.json", pdoc)
    if pm is not None:
        write_json(out / "page-map.json", pm)
    if source_map is not None:
        # omitted articles keep their identity, URL and version metadata, not their section titles or description
        sm = json.loads(json.dumps(source_map))
        for u in sm["units"]:
            if u["unit_id"] in omitted:
                u["sections_omitted"] = len(u["sections"])  # anchors are slugs of the headings, so they go too
                u["sections"] = []
                u["description"] = None
                u["omitted"] = omitted[u["unit_id"]]["state"]
        write_json(out / "source-map.json", sm)
    shutil.copy(local / "pack.yaml", out / "pack.yaml")
    sdir = out / "schemas"
    sdir.mkdir()
    for p in (local / "schemas").glob("*.json"):
        shutil.copy(p, sdir / p.name)
    # ---- regenerate exports from the filtered document
    rights = {e["id"]: e["rights"] for e in pub_elements}
    meta = {"pack_id": pack_id, "pack_version": doc["pack_version"], "document_id": doc["document_id"],
            "edition_id": doc["edition_id"], "edition_short": doc["edition_id"],
            "citable_statuses": sorted(__import__("irpacks.docmodel", fromlist=["CITABLE"]).CITABLE),
            "sources": doc["sources"]}
    em = packbuild.emit_outputs(pdoc, pm, pub_assets, rights, meta, out, variant="public", omitted=omitted,
                                review=review, chunk_cfg=recipe["chunking"],
                                corpus_rules=packbuild.make_corpus_rules(recipe), pack=pack, log=log)
    # ---- docs
    (out / "AGENT_GUIDE.md").write_text(packdocs.agent_guide(pack, meta, out), encoding="utf-8")
    routed_stub = {"qa": {"alignment-report.json": read_json(local / "qa" / "alignment-report.json")}
                   if (local / "qa" / "alignment-report.json").exists() else {},
                   "route": doc["route"]}
    if doc["route"] == "tagged_pdf_structure" and (local / "qa" / "structure-report.json").exists():
        routed_stub["conversion_changes"] = [l[2:] for l in (local / "ATTRIBUTION.md").read_text().split("## Changes made in this conversion")[1].split("\n") if l.startswith("- ")]
    (out / "ATTRIBUTION.md").write_text(packdocs.attribution(pack, rights_cfg, meta, routed_stub), encoding="utf-8")
    counts = {"elements_public": len(pub_elements), "elements_local": len(doc["elements"]),
              "omitted_items": len(omitted), "omitted_descendants": len(removed), "assets_public": len(pub_assets),
              "assets_withheld": len(withheld_assets), "chunks": len(em["chunks"]),
              "chunks_default_retrieval": sum(1 for c in em["chunks"] if c["include_in_default_retrieval"])}
    # ---- validation of the exported bytes
    from .validate import validate_build
    qa = out / "qa"
    qa.mkdir(exist_ok=True)
    vres = validate_build(out)
    leaks = check_public_bytes(out, doc, omitted, removed, withheld_assets, local_assets,
                               publisher=pack["source"].get("publisher") or "")
    vres["checks"] += leaks["checks"]
    vres["issues"] += leaks["issues"]
    vres["errors"] += sum(1 for c in leaks["checks"] if not c["passed"])
    vres["passed"] = vres["passed"] and all(c["passed"] for c in leaks["checks"])
    write_json(qa / "validation.json", vres)
    report = public_report(pack, doc, counts, omitted, withheld_assets, vres, leaks)
    (out / "PUBLIC_DISTRIBUTION_REPORT.md").write_text(report, encoding="utf-8")
    write_json(qa / "omitted.json", {"omitted": list(omitted.values()), "assets_withheld": list(withheld_assets.values())})
    status = {
        "local_source_coverage": "public subset of the local build (see PUBLIC_DISTRIBUTION_REPORT.md)",
        "public_distribution": (f"candidate: {counts['omitted_items']} items omitted, {counts['assets_withheld']} images "
                                f"withheld pending rights review; byte checks {'passed' if all(c['passed'] for c in leaks['checks']) else 'FAILED'}"),
        "extraction_fidelity": "as local build (qa-report.md of the local pack)",
        "retrieval_evaluation": "see eval/ results (run against the local and public indexes)",
        "operational_readiness": "not production-ready; not published",
    }
    (out / "README.md").write_text(packdocs.readme(pack, meta | {"sources": doc["sources"]}, _pub_counts(pdoc, pm, pub_assets, em["chunks"], source_map),
                                                   status, None, variant="public"), encoding="utf-8")
    manifest = {"schema_version": "1.0.0", "pack_id": pack_id, "pack_version": doc["pack_version"],
                "document_id": doc["document_id"], "edition_id": doc["edition_id"], "variant": "public", "scope": None,
                "engine": packbuild.ENGINE,
                "inputs": [{"source_id": doc["sources"]["authoritative"]["source_id"], "role": "authoritative",
                            "sha256": doc["sources"]["authoritative"]["sha256"], "verified": True,
                            "note": "source bytes not included; fetch from the source URL and verify the hash"}],
                "settings": {"derived_from": f"build/local/{pack_id}", "local_manifest_sha256": sha256_file(local / "manifest.json")},
                "tools": packbuild.tool_versions(), "outputs": [], "counts": counts, "status": status,
                "run": {"exported_at": utc_now()}}
    for p in sorted(out.rglob("*")):
        if p.is_file():
            manifest["outputs"].append({"path": p.relative_to(out).as_posix(), "sha256": sha256_file(p), "bytes": p.stat().st_size})
    write_json(out / "manifest.json", manifest)
    log(f"public candidate for {pack_id}: {out}")
    return {"out_dir": str(out), "counts": counts, "validation_passed": vres["passed"], "byte_checks": leaks["checks"]}


def _pub_counts(pdoc, pm, assets, chunks, source_map=None) -> dict:
    return packbuild._counts(pdoc, pm, assets, chunks, source_map=source_map)


def check_public_bytes(out: Path, doc: dict, omitted: dict, removed: set, withheld: dict, local_assets: dict,
                       publisher: str = "") -> dict:
    checks, issues = [], []

    def check(name, ok, detail=None):
        checks.append({"check": f"public:{name}", "passed": bool(ok), "severity": "ok" if ok else "error", "detail": detail})
        if not ok:
            issues.append({"severity": "critical", "kind": f"public:{name}", "location": out.name, "message": str(detail)[:400]})

    files = [p for p in out.rglob("*") if p.is_file()]
    bad_type = [p.relative_to(out).as_posix() for p in files if p.suffix.lower() not in ALLOWED_SUFFIXES]
    check("allowlisted_file_types", not bad_type, bad_type[:10])
    pdfs = [p.name for p in files if p.suffix.lower() in (".pdf", ".epub", ".html")]
    check("no_source_documents", not pdfs, pdfs)
    # withheld images: none of their bytes present
    hashes = {sha256_file(p) for p in files if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".svg")}
    leaked_imgs = [eid for eid in withheld if local_assets.get(eid, {}).get("sha256") in hashes]
    check("withheld_images_absent", not leaked_imgs, leaked_imgs[:10])
    # omitted text: no omitted/removed element text (>= 40 key chars) appears in any text file
    els = {e["id"]: e for e in doc["elements"]}
    # text that also occurs in a redistributable element (e.g. a name in an attribution line that the author
    # also uses in a paragraph) is not a leak of the omitted item
    allowed_key = "\x00".join(text_key(e.get("text") or "") for e in doc["elements"]
                               if e["rights"]["state"] == "redistributable" and e["id"] not in removed)
    # an omitted division/article keeps its title as a navigation stub on purpose: its title heading is not a leak
    stub_titles = {c for o in omitted for c in els[o]["children"]
                   if els[o]["type"] in ("part", "chapter", "division", "section") and els[c]["type"] == "heading"}
    # text that is (part of) a stub title is public by design, e.g. a "related content" link naming another article;
    # so is citation metadata: the document's publisher followed by a unit title (a reference list entry, or a cover
    # line repeating the title, can coincide with it)
    publisher = text_key(publisher)
    units = [e for e in doc["elements"] if e["type"] in ("chapter", "division")]
    unit_titles = [text_key(e["attrs"].get("title") or "") for e in units]
    unit_urls = [text_key(e["attrs"].get(k) or "") for e in units for k in ("url", "landing_page")]  # URL slugs
    stub_blob = "\x00".join([text_key(els[c].get("text") or "") for c in stub_titles] +
                             [publisher + t for t in unit_titles if publisher] + [u for u in unit_urls if u])
    probes = []
    for eid in [x for x in list(omitted) + list(removed) if x not in stub_titles]:
        k = text_key(els[eid].get("text") or "")
        if len(k) >= 40 and k[:80] not in allowed_key and k[:80] not in stub_blob:
            probes.append((eid, k[:80]))
    text_files = [p for p in files if p.suffix in (".md", ".json", ".jsonl", ".yaml")]
    hits = []
    for p in text_files:
        key = text_key(p.read_text(encoding="utf-8", errors="replace"))
        for eid, pr in probes:
            if pr in key:
                hits.append((p.relative_to(out).as_posix(), eid))
    # the sqlite index is binary: check its stored text through SQL
    import sqlite3
    if (out / "search.sqlite").exists():
        con = sqlite3.connect(out / "search.sqlite")
        rows = con.execute("SELECT text FROM chunks").fetchall()
        n_idx = len(rows)
        idx_key = text_key(" ".join(r[0] for r in rows))
        con.close()
        for eid, pr in probes:
            if pr in idx_key:
                hits.append(("search.sqlite", eid))
        n_jsonl = len(read_jsonl(out / "chunks.jsonl"))
        check("index_rebuilt_from_public_chunks", n_idx == n_jsonl, {"index_rows": n_idx, "chunks": n_jsonl})
    check("no_omitted_text_in_files", not hits, hits[:10])
    # no local-only artefacts
    local_only = [p.relative_to(out).as_posix() for p in files if p.name in ("annotations.jsonl", "alignment-report.json",
                                                                             "issues.jsonl")]
    check("no_local_only_artifacts", not local_only, local_only)
    return {"checks": checks, "issues": issues}


def public_report(pack, doc, counts, omitted, withheld, vres, leaks) -> str:
    by_reason = collections.Counter((o["state"], o["reason"]) for o in omitted.values())
    lines = [f"# Public distribution report — `{pack['pack_id']}`", "",
             "This directory is a **public release candidate**: an allowlisted subset of the local build. It is "
             "**not the complete converted source** where items are omitted. Nothing has been published.", "",
             "## Source", "", f"- {doc['title']} — `{doc['sources']['authoritative']['source_id']}`",
             (f"- Source pages are not included. Fetch the {len(doc['sources']['authoritative']['articles'])} article "
              f"captures with `uv run irpacks sources fetch {pack['pack_id']}` and verify them with "
              f"`uv run irpacks sources verify {pack['pack_id']}` (per-article SHA-256 in `source-map.json`)."
              if doc['sources']['authoritative'].get('articles') else
              f"- Source bytes are not included. Obtain them from {doc['sources']['authoritative'].get('url')} and verify "
              f"SHA-256 `{doc['sources']['authoritative']['sha256']}`; place them at "
              f"`{doc['sources']['authoritative'].get('local_path')}` relative to the project root."), "",
             "## Counts", ""]
    lines += [f"- {k.replace('_', ' ')}: {v}" for k, v in counts.items()]
    lines += ["", "## Omitted items (content removed, locator kept)", ""]
    if by_reason:
        for (st, reason), n in by_reason.most_common():
            lines.append(f"- {n} × `{st}` — {reason}")
        lines += ["", "| Element | Type | State | Printed page(s) | PDF page(s) |", "|---|---|---|---|---|"]
        for o in omitted.values():
            pp = sorted({str(s.get('page_label')) for s in o["source_spans"]})
            ph = sorted({s.get('page') for s in o["source_spans"] if s.get('page')})
            lines.append(f"| `{o['id']}` | {o['type']} | {o['state']} | {', '.join(pp)[:40]} | {', '.join(map(str, ph))[:40]} |")
    else:
        lines.append("None.")
    lines += ["", "## Images withheld (captions kept)", "",
              f"{len(withheld)} figure image(s) withheld because their rights basis is unresolved "
              "(third-party screenshots and graphics are excluded from the source license; authorship not yet reviewed). "
              "Each figure keeps its caption and PDF locator; see `qa/omitted.json`."]
    lines += ["", "## Byte-level checks", "", "| Check | Result | Detail |", "|---|---|---|"]
    for c in leaks["checks"]:
        lines.append(f"| {c['check']} | {'pass' if c['passed'] else 'FAIL'} | {json.dumps(c['detail'])[:100] if c['detail'] else ''} |")
    lines += ["", f"Pack validation: {'passed' if vres['passed'] else 'FAILED'} ({vres['errors']} errors, {vres['warnings']} warnings).",
              "", "## To include withheld items", "",
              "A maintainer records a reviewed decision in `packs/<pack-id>/rights.yaml` under `overrides` "
              "(element id, state, basis, reviewer, date), rebuilds, and re-exports. Rights decisions are not made by the pipeline.", ""]
    return "\n".join(lines)
