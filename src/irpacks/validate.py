"""Validation of a built pack directory (local or public variant).

Checks are deterministic and offline.  Passing them does not prove fidelity:
agreement between the structured candidate and the PDF, or equal word counts,
is diagnostic evidence only; visual review results are recorded separately.
"""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path
from typing import Any

import jsonschema

from .util import PROJECT_ROOT, read_json, read_jsonl, sha256_file, text_key

ABS_PATH = re.compile(r"(/home/[\w.-]+/|/root/|/tmp/claude|/Users/[\w.-]+/)")
_LINK = re.compile(r"(?<!\\)\]\(([^)\s]+)\)")  # an escaped "\]" is literal text, not a link
_ID = re.compile(r'<a id="([^"]+)"></a>')


def _schema(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "schemas" / name).read_text(encoding="utf-8"))


def validate_build(out_dir: str | Path, *, pack: dict | None = None, routed: dict | None = None,
                   chunk_max: int | None = None, log=print) -> dict[str, Any]:
    out_dir = Path(out_dir)
    checks: list[dict] = []
    issues: list[dict] = []

    def check(name: str, ok: bool, detail: Any = None, severity: str = "error", count: int | None = None):
        checks.append({"check": name, "passed": bool(ok), "severity": severity if not ok else "ok",
                       "detail": detail, "count": count})
        if not ok:
            issues.append({"severity": "critical" if severity == "error" else ("minor" if severity == "warning" else "info"),
                           "kind": f"validation:{name}", "location": str(out_dir.name), "message": str(detail)[:500]})

    doc = read_json(out_dir / "document.json")
    paged = (out_dir / "page-map.json").exists()
    pm = read_json(out_dir / "page-map.json") if paged else {"pages": [], "page_count": 0}
    sm = read_json(out_dir / "source-map.json") if (out_dir / "source-map.json").exists() else None
    chunks = read_jsonl(out_dir / "chunks.jsonl")
    variant = doc.get("variant", "local")
    els = {e["id"]: e for e in doc["elements"]}

    # 1. schemas
    maps = [("schema:page-map", pm, "page-map.schema.json")] if paged else \
        ([("schema:source-map", sm, "source-map.schema.json")] if sm is not None else [])
    if not paged and sm is None:
        check("maps:page_or_source_map_present", False, "neither page-map.json nor source-map.json exists")
    for name, obj, sch in [("schema:document", doc, "document.schema.json")] + maps:
        try:
            jsonschema.validate(obj, _schema(sch))
            check(name, True)
        except jsonschema.ValidationError as exc:
            check(name, False, f"{exc.message} at {list(exc.absolute_path)[:6]}")
    csch = _schema("chunk.schema.json")
    v = jsonschema.Draft202012Validator(csch)
    bad = [(c["chunk_id"], next(iter(v.iter_errors(c))).message) for c in chunks if not v.is_valid(c)]
    check("schema:chunks", not bad, bad[:5], count=len(bad))
    if (out_dir / "pack.yaml").exists():
        import yaml
        try:
            jsonschema.validate(yaml.safe_load((out_dir / "pack.yaml").read_text()), _schema("pack.schema.json"))
            check("schema:pack.yaml", True)
        except jsonschema.ValidationError as exc:
            check("schema:pack.yaml", False, exc.message)

    # 2. duplicate IDs / anchors
    ids = [e["id"] for e in doc["elements"]]
    dup = [k for k, n in collections.Counter(ids).items() if n > 1]
    check("unique:element_ids", not dup, dup[:10], count=len(dup))
    anchors = [e["anchor"] for e in doc["elements"] if e.get("anchor")]
    dupa = [k for k, n in collections.Counter(anchors).items() if n > 1]
    check("unique:anchors", not dupa, dupa[:10], count=len(dupa))
    cids = [c["chunk_id"] for c in chunks]
    dupc = [k for k, n in collections.Counter(cids).items() if n > 1]
    check("unique:chunk_ids", not dupc, dupc[:10], count=len(dupc))

    # 3. tree integrity
    bad_parent = [e["id"] for e in doc["elements"] if e["parent_id"] and e["parent_id"] not in els]
    bad_child = [e["id"] for e in doc["elements"] if any(c not in els for c in e["children"])]
    check("tree:parents_and_children", not bad_parent and not bad_child, (bad_parent + bad_child)[:10])
    order_ok = all(doc["elements"][i]["order"] == i for i in range(len(doc["elements"])))
    check("tree:order_contiguous", order_ok)

    # 4. pages and locators
    pages = {p["page"]: p for p in pm["pages"]}
    n_total = pm["page_count"]
    if not paged and sm is not None:
        _web_locator_checks(doc, els, sm, check, out_dir, variant)
    if paged and variant == "local" and not (out_dir / "qa" / "pilot-scope.json").exists():
        missing = [p for p in range(1, n_total + 1) if p not in pages]
        check("pages:all_accounted", not missing, missing[:20], count=len(missing))
    unresolved = [p["page"] for p in pm["pages"] if p["coverage_status"] == "unresolved"]
    if paged:
        check("pages:no_unresolved_coverage", not unresolved, unresolved[:30], severity="warning", count=len(unresolved))
    bad_span = []
    for e in doc["elements"]:
        for s in e["source_spans"]:
            if s["locator_type"] != "pdf_page":
                continue
            p = pages.get(s["page"])
            if p is None:
                bad_span.append((e["id"], s["page"], "page not in page map"))
                continue
            if s.get("page_label") != p["page_label"]:
                bad_span.append((e["id"], s["page"], f"label {s.get('page_label')} != {p['page_label']}"))
            bb = s.get("bbox")
            if bb and not (-1 <= bb[0] <= bb[2] <= p["width"] + 1 and -1 <= bb[1] <= bb[3] <= p["height"] + 1):
                bad_span.append((e["id"], s["page"], f"bbox out of page {bb}"))
    if paged:
        check("locators:valid_page_label_bbox", not bad_span, bad_span[:10], count=len(bad_span))

    # 5. leaf mapping status
    # text-bearing elements: leaves plus containers with their own text (list items with nested blocks)
    leaves = [e for e in doc["elements"] if e["type"] not in ("document", "part", "chapter", "division", "section")
              and (not e["children"] or e.get("text"))]
    unver = [e["id"] for e in leaves if e["mapping_status"] == "unverified"]
    check("elements:no_unverified_leaves", not unver, unver[:20], severity="warning", count=len(unver))
    nospan = [e["id"] for e in leaves if not e["source_spans"] and e["type"] not in ("thematic_break", "omitted")
              and e["mapping_status"] not in ("omitted",)]
    check("elements:leaves_have_locators", not nospan, nospan[:20], severity="warning", count=len(nospan))

    # 5b. hierarchy vs the PDF outline: elements of a bookmarked unit lie within the bookmark's page range
    # outline entries that point at table rows (a checklist's phase headings) are not structural units
    bms = [b for b in pm.get("bookmarks") or [] if b.get("page") and b.get("role") != "table_row_heading"]
    if bms:
        res = bookmark_ranges(doc, bms, n_total)
        check("hierarchy:bookmarks_matched", not res["unmatched"], res["unmatched"][:10], severity="warning",
              count=len(res["unmatched"]))
        check("hierarchy:elements_within_bookmark_ranges", not res["outside"], res["outside"][:10],
              count=len(res["outside"]))
    elif variant == "local" and paged:
        check("hierarchy:bookmarks_matched", False, "page map has no PDF outline", severity="info")

    # 6. furniture leakage into element text
    furn_pat = set()
    for p in pm["pages"]:
        for f in p["furniture"]:
            t = f["text"].strip()
            if len(t) > 12:
                furn_pat.add(t)
    leaked = []
    for e in leaves:
        txt = e.get("text") or ""
        for t in list(furn_pat)[:2000]:
            if t in txt:
                leaked.append((e["id"], t))
                break
    if paged:
        check("text:no_running_furniture", not leaked, leaked[:10], count=len(leaked))

    # 7. markdown: anchors, links, fences, assets
    book = (out_dir / "book.md").read_text(encoding="utf-8")
    md_files = {"book.md": book}
    for f in sorted((out_dir / "chapters").glob("*.md")):
        md_files[f"chapters/{f.name}"] = f.read_text(encoding="utf-8")
    ids_in = {name: set(_ID.findall(t)) for name, t in md_files.items()}
    broken = []
    for name, t in md_files.items():
        stripped = re.sub(r"(?s)(`{3,}).*?\1", "", t).replace("\\\\", "\x00")  # ignore code; neutralise escaped "\\"
        for link in _LINK.findall(stripped):
            if re.match(r"^[a-z]+:", link):
                continue
            path, _, frag = link.partition("#")
            base = (Path(name).parent / path).as_posix() if path else name
            base = re.sub(r"(^|/)[^/]+/\.\./", r"\1", base)
            if path and not (out_dir / base).exists():
                broken.append((name, link, "missing file"))
                continue
            if frag and frag not in ids_in.get(base, set()):
                broken.append((name, link, "missing anchor"))
    check("markdown:links_and_anchors_resolve", not broken, broken[:15], count=len(broken))
    fence_issue = []
    for name, t in md_files.items():
        # CommonMark: a fence closes only on a backtick run at least as long as the one that opened it, so code that
        # itself contains ``` lines (inside a longer fence) does not count as a fence
        open_len = 0
        for ln in t.split("\n"):
            m = re.match(r"^(?:\s*>)*\s*(`{3,})(.*)$", ln)
            if not m:
                continue
            if open_len == 0:
                open_len = len(m.group(1))
            elif len(m.group(1)) >= open_len and not m.group(2).strip():
                open_len = 0
        if open_len:
            fence_issue.append(name)
    check("markdown:code_fences_balanced", not fence_issue, fence_issue)
    # code round-trip: each code element's text equals the body of a fenced block (after removing the
    # block's own list-indent / blockquote prefix)
    bodies = set()
    blines = book.split("\n")
    i = 0
    while i < len(blines):
        m = re.match(r"^((?:\s*>\s?)*\s*)(`{3,})(\S*)\s*$", blines[i])
        if m:
            prefix, fence = m.group(1), m.group(2)
            j, body = i + 1, []
            while j < len(blines):
                ln = blines[j]
                if ln.startswith(prefix) and ln[len(prefix):].strip() == fence:
                    break
                body.append(ln[len(prefix):] if ln.startswith(prefix) else ln.lstrip("> ").rstrip() if not ln.strip(" >") else ln)
                j += 1
            bodies.add("\n".join(body))
            i = j + 1
            continue
        i += 1
    code_missing = [e["id"] for e in leaves if e["type"] == "code" and e["id"] not in _omitted_ids(doc)
                    and e["text"] not in bodies]
    check("markdown:code_verbatim", not code_missing, code_missing[:10], count=len(code_missing))
    # assets
    amiss = []
    aj = out_dir / "assets" / "assets.json"
    if aj.exists():
        for a in read_json(aj)["assets"]:
            for pth in (a.get("paths") or ([a["path"]] if a.get("path") else [])):
                if not (out_dir / pth).exists():
                    amiss.append(pth)
                elif a.get("sha256") and pth == a.get("path") and sha256_file(out_dir / pth) != a["sha256"]:
                    amiss.append(pth + " (hash mismatch)")
    check("assets:files_exist_and_hash", not amiss, amiss[:10], count=len(amiss))

    # 8. tables
    tbad = []
    for e in leaves:
        if e["type"] != "table":
            continue
        widths = {sum(c["colspan"] for c in r["cells"]) for r in e["attrs"]["rows"]}
        if not e["attrs"].get("complex") and len(widths) > 1:
            tbad.append((e["id"], sorted(widths)))
        if not any(r["section"] == "thead" for r in e["attrs"]["rows"]):
            tbad.append((e["id"], "no header row"))
    check("tables:structure", not tbad, tbad[:10], severity="warning", count=len(tbad))

    # 9. element text coverage in book.md (monotonic key containment)
    import html as _html
    norm = re.sub(r"<a id=\"[^\"]*\"></a>|<!--.*?-->|</?(?:table|thead|tbody|tfoot|tr|td|th)\b[^>]*>|<br>", " ", book)
    norm = _html.unescape(norm)
    norm = re.sub(r"\[\^[a-z0-9-]*?fn0*(\d+)\]", r"[\1]", norm)          # footnote refs -> printed label
    norm = norm.replace("\\\\", "\x00")  # an escaped backslash must not hide the bracket after it
    norm = re.sub(r"(?<!\\)\[((?:[^\[\]]|\\\[|\\\])*)(?<!\\)\]\([^)\s]*\)", r"\1", norm)  # links -> link text
    norm = norm.replace("\x00", "\\")
    bkey = text_key(norm)
    cur, misses = 0, []
    om = _omitted_ids(doc)
    for e in leaves:
        if e["id"] in om or e["type"] in ("figure", "thematic_break", "math"):
            continue
        k = text_key(e.get("text") or "")
        if len(k) < 12:
            continue
        probe = k[:60]
        pos = bkey.find(probe, cur)
        if pos < 0:
            pos = bkey.find(probe)
            if pos < 0:
                misses.append(e["id"])
                continue
        cur = pos
    check("markdown:every_element_rendered", not misses, misses[:10], count=len(misses))

    # 10. chunks: coverage, links, token limits, citations
    chunk_ids = set(cids)
    in_chunks = set()
    for c in chunks:
        in_chunks.update(c["source_element_ids"])
    not_chunked = [e["id"] for e in leaves if e["id"] not in in_chunks and e["id"] not in om
                   and e["type"] not in ("thematic_break",) and (e.get("text") or e["type"] == "figure")]
    check("chunks:every_leaf_in_a_chunk", not not_chunked, not_chunked[:10], count=len(not_chunked))
    link_bad = []
    for i, c in enumerate(chunks):
        if c["previous_chunk_id"] != (chunks[i - 1]["chunk_id"] if i else None):
            link_bad.append((c["chunk_id"], "previous"))
        if c["next_chunk_id"] != (chunks[i + 1]["chunk_id"] if i + 1 < len(chunks) else None):
            link_bad.append((c["chunk_id"], "next"))
        for r in c["required_context_ids"]:
            if r not in chunk_ids:
                link_bad.append((c["chunk_id"], f"required {r}"))
        if c["parent_section_id"] not in els:
            link_bad.append((c["chunk_id"], "parent section"))
        for sid in c["source_element_ids"]:
            if sid not in els:
                link_bad.append((c["chunk_id"], f"element {sid}"))
        if c.get("overlap") and c["overlap"]["from_chunk_id"] not in chunk_ids:
            link_bad.append((c["chunk_id"], "overlap source"))
    check("chunks:context_links_resolve", not link_bad, link_bad[:10], count=len(link_bad))
    mx = chunk_max or (max((c["token_count"] for c in chunks if not c["oversize"]), default=0))
    over = [c["chunk_id"] for c in chunks if c["token_count"] > mx and not c["oversize"]]
    check("chunks:no_unflagged_overflow", not over, over[:10], count=len(over))
    cite_bad = []
    for c in chunks if not paged else []:
        if not c["citation"].get("url") or not str(c["citation"]["url"]).startswith("https://"):
            cite_bad.append(c["chunk_id"] + " (no citation URL)")
        span_urls = list(dict.fromkeys(s["url"] for s in c["source_spans"] if s.get("url")))
        if not span_urls or c["citation"].get("urls") != span_urls:
            cite_bad.append(c["chunk_id"] + " (citation does not list the section URLs it covers)")
        if c["include_in_default_retrieval"] and not c["extraction"]["validated_locators"]:
            cite_bad.append(c["chunk_id"] + " (included without validated locators)")
    for c in chunks if paged else []:
        sp_pages = sorted({s["page"] for s in c["source_spans"] if s.get("page")})
        if c["citation"]["pdf_pages"] and sorted(c["citation"]["pdf_pages"]) != sp_pages:
            cite_bad.append(c["chunk_id"])
        for p, lab in zip(c["citation"]["pdf_pages"], c["citation"]["printed_pages"]):
            if pages.get(p) and pages[p]["page_label"] != lab:
                cite_bad.append(c["chunk_id"])
        if c["include_in_default_retrieval"] and not c["extraction"]["validated_locators"]:
            cite_bad.append(c["chunk_id"] + " (included without validated locators)")
    check("chunks:citations_match_page_map" if paged else "chunks:citations_have_urls", not cite_bad, cite_bad[:10],
          count=len(cite_bad))

    # 11. absolute developer paths in metadata and path fields (source text may legitimately contain
    #     example paths such as C:\\Users\\..., so element/chunk text is not scanned).
    abs_hits = []
    meta_files = [out_dir / n for n in ("manifest.json", "pack.yaml", "README.md", "AGENT_GUIDE.md", "ATTRIBUTION.md",
                                       "qa-report.md", "assets/assets.json", "PUBLIC_DISTRIBUTION_REPORT.md")]
    for p in meta_files:
        if p.exists():
            t = p.read_text(encoding="utf-8", errors="replace")
            m = ABS_PATH.search(t)
            if m:
                abs_hits.append((p.relative_to(out_dir).as_posix(), m.group(0)))

    def walk_paths(obj, where):
        if isinstance(obj, dict):
            for k, val in obj.items():
                if k in ("path", "local_path", "paths", "file") and val:
                    for x in (val if isinstance(val, list) else [val]):
                        if isinstance(x, str) and (x.startswith("/") or re.match(r"^[A-Za-z]:\\\\", x)):
                            abs_hits.append((where, x))
                else:
                    walk_paths(val, where)
        elif isinstance(obj, list):
            for x in obj:
                walk_paths(x, where)

    walk_paths(doc.get("sources"), "document.json:sources")
    walk_paths([e["attrs"].get("asset") for e in doc["elements"]], "document.json:assets")
    walk_paths([c["asset_refs"] for c in chunks], "chunks.jsonl:asset_refs")
    check("outputs:no_absolute_paths", not abs_hits, abs_hits[:10], count=len(abs_hits))

    # 12. private-marker / markers leak
    leak = [e["id"] for e in doc["elements"] if re.search("[-]", e.get("text", "") + e.get("md", ""))]
    check("text:no_internal_markers", not leak, leak[:10], count=len(leak))

    passed = all(c["passed"] or c["severity"] != "error" for c in checks)
    return {"out_dir": out_dir.name, "variant": variant, "passed": passed,
            "errors": sum(1 for c in checks if not c["passed"] and c["severity"] == "error"),
            "warnings": sum(1 for c in checks if not c["passed"] and c["severity"] == "warning"),
            "checks": checks, "issues": issues}


def _web_locator_checks(doc: dict, els: dict, sm: dict, check, out_dir: Path, variant: str) -> None:
    """Unpaginated sources: every unit in the source map is in the document with matching metadata; every element
    locator is the unit's URL plus an anchor that exists in that unit."""
    units = {u["unit_id"]: u for u in sm["units"]}
    missing = [u for u in units if u not in els]
    check("sources:all_units_present", not missing, missing[:10], count=len(missing))
    meta_bad = [u for u, rec in units.items() if u in els and (
        els[u]["attrs"].get("url") != rec["url"] or els[u]["attrs"].get("git_commit_id") != rec["git_commit_id"])]
    check("sources:unit_metadata_matches_source_map", not meta_bad, meta_bad[:10], count=len(meta_bad))
    anchors = {u: {s["anchor"] for s in rec["sections"]} for u, rec in units.items()}
    bad = []
    for e in doc["elements"]:
        for s in e["source_spans"]:
            if s["locator_type"] != "section_anchor":
                bad.append((e["id"], "locator type " + s["locator_type"]))
                continue
            unit = units.get(s.get("unit"))
            if unit is None:
                bad.append((e["id"], f"unknown unit {s.get('unit')}"))
                continue
            want = unit["url"] + (f"#{s['anchor']}" if s.get("anchor") else "")
            if s.get("url") != want:
                bad.append((e["id"], f"url {s.get('url')} != {want}"))
            if s.get("anchor") and s["anchor"] not in anchors[unit["unit_id"]]:
                bad.append((e["id"], f"anchor {s['anchor']} not a section of {unit['unit_id']}"))
    check("locators:urls_and_anchors_resolve", not bad, bad[:10], count=len(bad))


def _omitted_ids(doc: dict) -> set[str]:
    return {o["id"] for o in doc.get("omitted", [])}


def bookmark_ranges(doc: dict, bookmarks: list[dict], page_count: int) -> dict:
    """Match PDF bookmarks to structural units by title; return unmatched bookmarks and out-of-range elements.

    A bookmark's range runs from its page through the page where the next bookmark at the same or a higher level
    starts (units may share that boundary page), or the last page."""
    els = {e["id"]: e for e in doc["elements"]}
    units = [e for e in doc["elements"] if e["type"] in ("part", "chapter", "division", "section")]

    def title_keys(e: dict) -> set[str]:
        head = next((els[c] for c in e["children"] if els[c]["type"] == "heading"), None)
        return {text_key(t) for t in ((head or {}).get("text"), e["attrs"].get("title")) if t}

    def bookmark_keys(title: str) -> set[str]:
        # "Chapter 1. Title" is printed as "1. Title" in the heading; "Appendix B. Glossary" stays as is
        t = title.strip()
        return {text_key(t), text_key(re.sub(r"^chapter\s+", "", t, flags=re.I)),
                text_key(re.sub(r"^(chapter|appendix)\s+[0-9a-z]+[.:]\s*", "", t, flags=re.I))}
    by_key: dict[str, list[dict]] = collections.defaultdict(list)
    for u in units:
        for k in title_keys(u):
            by_key[k].append(u)
    def head_page(u: dict) -> int | None:
        head = next((els[c] for c in u["children"] if els[c]["type"] == "heading"), None)
        spans = (head or u)["source_spans"]
        return spans[0].get("page") if spans else None

    used: set[str] = set()
    unmatched, outside, matched = [], [], 0
    for i, b in enumerate(bookmarks):
        end = page_count
        for nb in bookmarks[i + 1:]:
            if nb["level"] <= b["level"]:
                end = max(b["page"], nb["page"])
                break
        cands = list({u["id"]: u for k in bookmark_keys(b["title"]) for u in by_key.get(k, [])
                      if u["id"] not in used}.values())
        # a title can repeat (e.g. "Containment"); the unit whose heading starts on the bookmarked page wins
        at = [u for u in cands if head_page(u) == b["page"]] or [u for u in cands if b["page"] <= (head_page(u) or 0) <= end]
        if not at:
            if text_key(b["title"]) not in ("untitled", ""):
                unmatched.append(b["title"])
            continue
        u = at[0]
        used.add(u["id"])
        matched += 1
        stack = [u["id"]]
        while stack:
            e = els[stack.pop()]
            stack.extend(c for c in e["children"] if c in els)
            for s in e["source_spans"]:
                p = s.get("page")
                if p and not (b["page"] <= p <= end) and s.get("role") != "aggregate":
                    outside.append((e["id"], p, f"{b['title']!r} spans {b['page']}-{end}"))
    return {"matched": matched, "unmatched": unmatched, "outside": outside}
