"""Command-line interface: ``irpacks <command>`` (run as ``uv run irpacks ...``).

All commands are local and read-only with respect to sources, except
``sources fetch`` (opt-in download) and ``check-updates`` (opt-in, writes a
proposal report only).  No command executes text found in a source.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .util import PROJECT_ROOT, read_json


def _out(obj, as_json: bool, text_fn=None):
    if as_json or text_fn is None:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(text_fn(obj))


def _resolve_index(args) -> Path:
    if getattr(args, "index", None):
        return Path(args.index)
    if getattr(args, "pack", None):
        for root in (PROJECT_ROOT / "build" / "local", PROJECT_ROOT / "dist" / "public" / "packs"):
            cand = root / args.pack / "search.sqlite"
            if cand.exists():
                return cand
        cand = Path(args.pack) / "search.sqlite"
        if cand.exists():
            return cand
        raise SystemExit(f"no search.sqlite for pack {args.pack!r}; build it first")
    default = PROJECT_ROOT / "build" / "local" / "index.sqlite"
    if default.exists():
        return default
    raise SystemExit("no index: pass --pack <pack-dir-name> or --index PATH, or run `irpacks index`")


def _fmt_search(res: dict) -> str:
    lines = [f"query: {res['query']}  (ranking v{res['ranking']['version']}"
             + (f", dense: {res['dense']}" if res.get("dense") else "") + ")"]
    cov = res.get("coverage")
    if cov:
        lines.append(f"coverage: {cov['level']} (best passage {cov['best_coverage']})"
                     + (f"; in no result: {', '.join(cov['terms_in_no_result'])}" if cov["terms_in_no_result"] else ""))
        if cov["level"] != "high":
            lines.append(f"  {cov['advice']}")
    lines.append("")
    for r in res["results"]:
        lines.append(f"{r['rank']}. [{r['score']}] {r['chunk_id']}")
        lines.append(f"   {' > '.join(r['heading_path'])}")
        if r.get("match"):
            lines.append(f"   match: {r['match']['coverage']}" + (f" (missing: {', '.join(r['match']['missing_terms'])})"
                                                                  if r["match"]["missing_terms"] else ""))
        lines.append(f"   cite: {r['citation']['text']}")
        if r["required_context_ids"]:
            lines.append(f"   required context: {', '.join(r['required_context_ids'])}")
        body = r["text"].split("\n", 2)[-1].replace("\n", " ")
        lines.append(f"   {body[:300]}{'…' if len(body) > 300 else ''}")
        lines.append("")
    if not res["results"]:
        lines.append("no results")
    return "\n".join(lines)


def _fmt_context(res: dict) -> str:
    if "error" in res:
        return res["error"]
    lines = [f"seed {res['seed']} · {res['tokens_used']}/{res['budget']} tokens · required context complete: "
             f"{res['required_context_complete']}"]
    for it in res["items"]:
        lines += ["", f"--- {it['role']} · {it['chunk_id']}", f"cite: {it['citation']['text']}", it["text"]]
    if res["omitted"]:
        lines += ["", "omitted (budget): " + ", ".join(f"{o['chunk_id']} ({o['reason']})" for o in res["omitted"])]
    if res["visual_assets"]:
        lines += ["", "visual assets: " + ", ".join(a.get("path") or a["element_id"] for a in res["visual_assets"])]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="irpacks", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sources", help="source registry (sources.lock.json)")
    s.add_argument("action", choices=["lock", "verify", "fetch", "discover"],
                   help="discover: web packs: select articles from the published tables of contents and capture them")
    s.add_argument("pack", nargs="?", help="pack id (required for fetch and discover; optional filter for verify)")
    s.add_argument("--pack", dest="pack_opt", help=argparse.SUPPRESS)  # earlier spelling, still accepted
    s.add_argument("--force", action="store_true")

    b = sub.add_parser("build", help="build a pack into build/local/<pack-id>")
    b.add_argument("pack")
    b.add_argument("--pilot", action="store_true", help="build only the recipe's pilot scope")
    b.add_argument("--authoritative", help="registered source_id to use instead of the recipe default")

    v = sub.add_parser("validate", help="validate a built pack directory")
    v.add_argument("path")
    v.add_argument("--json", action="store_true")

    ix = sub.add_parser("index", help="build a combined search index over built packs")
    ix.add_argument("dirs", nargs="*", help="pack directories (default: all full local builds)")
    ix.add_argument("--out", default=str(PROJECT_ROOT / "build" / "local" / "index.sqlite"))
    ix.add_argument("--index-text", choices=["plain", "markdown"], default="plain",
                    help="index chunk text without link targets (plain, rankings v3 and v4) or as stored Markdown (v2)")
    ix.add_argument("--dense", action="store_true",
                    help="also embed every chunk for ranking v3's semantic component (needs `uv sync --extra dense` "
                         "and `irpacks models fetch`)")
    mo = sub.add_parser("models", help="the pinned local embedding model for dense retrieval (opt-in download)")
    mo.add_argument("action", choices=["fetch", "verify"])

    q = sub.add_parser("search", help="keyword search (SQLite FTS5)")
    q.add_argument("query")
    q.add_argument("--pack", help="pack directory name under build/local or dist/public/packs")
    q.add_argument("--index", help="explicit search.sqlite / index.sqlite path")
    q.add_argument("--k", type=int, default=5)
    q.add_argument("--json", action="store_true")
    q.add_argument("--filter-pack", action="append", dest="filter_packs")
    q.add_argument("--pack-version")
    q.add_argument("--corpus", action="append")
    q.add_argument("--authority", action="append")
    q.add_argument("--review-status", action="append")
    q.add_argument("--applicability", action="append")
    q.add_argument("--content-type", action="append")
    q.add_argument("--rights-state", action="append")
    q.add_argument("--include-excluded", action="store_true", help="also search excluded corpora (index, TOC, ...)")
    q.add_argument("--mode", choices=["auto", "and", "or"], default="auto")

    c = sub.add_parser("context", help="expand a chunk with required context within a token budget")
    c.add_argument("chunk_id")
    c.add_argument("--pack")
    c.add_argument("--index")
    c.add_argument("--budget", type=int, default=3000)
    c.add_argument("--no-section", action="store_true")
    c.add_argument("--json", action="store_true")

    se = sub.add_parser("section", help="print the Markdown of one element/section")
    se.add_argument("element_id")
    se.add_argument("--pack", required=True)

    ep = sub.add_parser("export-public", help="build the allowlisted public candidate for a pack")
    ep.add_argument("pack")

    ev = sub.add_parser("eval", help="run the retrieval evaluation")
    ev.add_argument("--split", default="all", help="tuning, heldout, all, or a split named in --questions")
    ev.add_argument("--questions", help="question file (default: eval/questions.yaml)")
    ev.add_argument("--index")
    ev.add_argument("--k", type=int, default=5)
    ev.add_argument("--out")
    ev.add_argument("--results-file", help="score another backend's ranked chunk IDs ({backend, details, results})")
    ev.add_argument("--label")
    ev.add_argument("--ranking", choices=["v2", "v3", "v4"], default="v4",
                    help="frozen ranking to evaluate (v2 needs an index built with --index-text markdown)")
    ev.add_argument("--json", action="store_true")

    pk = sub.add_parser("package", help="create local and public-candidate archives under dist/")
    pk.add_argument("--version", default=None)

    cu = sub.add_parser("check-updates", help="opt-in: compare upstream source with the lock and propose a diff")
    cu.add_argument("pack")
    cu.add_argument("--download", action="store_true", help="fetch changed artifacts into input/<pack>/candidates/")
    cu.add_argument("--json", action="store_true", help="print the full report (default: a summary)")

    ip = sub.add_parser("inspect-pdf", help="print PDF inventory (pages, labels, bookmarks, images)")
    ip.add_argument("path")

    rp = sub.add_parser("review-page", help="render a PDF page and list the extracted elements on it (visual review aid)")
    rp.add_argument("pack", help="pack directory name under build/local")
    rp.add_argument("page", type=int)
    rp.add_argument("--scale", type=float, default=1.3)
    rp.add_argument("--chars", type=int, default=160)

    cm = sub.add_parser("countermap-example", help="run the synthetic Countermap integration example")
    cm.add_argument("--countermap-db", default=None, help="Countermap database (default: $COUNTERMAP_DB)")
    cm.add_argument("--countermap-bin", default=None, help="countermap executable (default: $COUNTERMAP_BIN or PATH)")
    cm.add_argument("--json", action="store_true")

    vm = sub.add_parser("validate-mappings", help="validate a book-to-framework mapping file")
    vm.add_argument("path")
    vm.add_argument("--countermap-db", default=None, help="resolve IDs against this Countermap database")
    vm.add_argument("--countermap-bin", default="countermap")

    df = sub.add_parser("diff", help="semantic change report between two builds (or a history record and a build)")
    df.add_argument("old", help="build directory, document.json, or history/<pack>/<snapshot>.json")
    df.add_argument("new", help="build directory, document.json, or history record")
    df.add_argument("--out", help="write <out>.md and <out>.json (default: print Markdown)")

    sn = sub.add_parser("snapshot", help="record a text-free history fingerprint of a built pack under history/")
    sn.add_argument("pack")

    rf = sub.add_parser("refresh", help="web packs: capture today's pages, build a candidate, report changes")
    rf.add_argument("pack")
    rf.add_argument("--date", help="snapshot label (default: today, YYYY-MM-DD)")
    rf.add_argument("--adopt", action="store_true", help="also rewrite recipe/pack/lock to the new snapshot")

    sv = sub.add_parser("serve", help="run the read-only MCP server over stdio (needs the `mcp` extra)")
    sv.add_argument("--index", help="index to serve (default: build/local/index.sqlite)")
    sv.add_argument("--pack", help="serve one pack's own search.sqlite instead")

    args = ap.parse_args(argv)

    if args.cmd == "diff":
        from . import diffdoc
        ko, old = diffdoc.load_any(args.old)
        kn, new = diffdoc.load_any(args.new)
        if "fingerprint" in (ko, kn):
            fo = old if ko == "fingerprint" else diffdoc.fingerprint(args.old)
            fn = new if kn == "fingerprint" else diffdoc.fingerprint(args.new)
            rep = diffdoc.diff_fingerprints(fo, fn)
            md = diffdoc.render_fingerprint_markdown(rep)
        else:
            rep = diffdoc.diff_documents(old, new)
            md = diffdoc.render_markdown(rep)
        if args.out:
            from .util import write_json
            write_json(args.out + ".json", rep)
            Path(args.out + ".md").write_text(md, encoding="utf-8")
            print(json.dumps(rep.get("summary") or {"units_with_changes": len(rep["units"])}, indent=2))
        else:
            print(md)
        return 0
    if args.cmd == "snapshot":
        from . import diffdoc
        from .util import write_json
        fp = diffdoc.fingerprint(PROJECT_ROOT / "build" / "local" / args.pack)
        dest = PROJECT_ROOT / "history" / args.pack / f"{fp['edition_id']}.json"
        write_json(dest, fp)
        print(f"wrote {dest.relative_to(PROJECT_ROOT)} ({len(fp['units'])} units, "
              f"{sum(len(u['sections']) for u in fp['units'])} section fingerprints)")
        return 0
    if args.cmd == "refresh":
        from .refresh import refresh
        print(json.dumps(refresh(args.pack, date=args.date, adopt=args.adopt, log=lambda *a, **k: None), indent=2))
        return 0
    if args.cmd == "serve":
        from .mcp_server import serve
        serve(_resolve_index(args))  # stdout carries the protocol: nothing else may print
        return 0
    if args.cmd == "sources":
        from . import sources
        if args.action == "lock":
            lock = sources.generate_lock()
            print(f"wrote sources.lock.json with {len(lock['sources'])} sources")
        pack = args.pack or args.pack_opt
        if args.action == "verify":
            res = sources.verify_all(pack)
            notes = {"changed": "reference capture differs from the lock; not a build input (pages can embed "
                                "per-request tokens)",
                     "not_supplied": "registered identity that was never supplied; not needed for the build",
                     "absent": "not fetched yet: run `irpacks sources fetch <pack>`",
                     "MISMATCH": "build input differs from the lock: do not build; register the file as a new "
                                 "source identity if it is intended"}
            by_pack: dict[str, list] = {}
            for r in res:
                by_pack.setdefault(r["source_id"].split("/", 1)[0], []).append(r)
            blocking = []
            for pid, recs in by_pack.items():
                if all(r["status"] in ("absent", "not_supplied") for r in recs):  # never fetched: not a problem
                    print(f"{'not fetched':>12}  {pid} ({len(recs)} sources): run `irpacks sources fetch {pid}` to use it")
                    continue
                for r in recs:
                    if r["status"] != "ok" or len(recs) <= 20:
                        note = notes.get(r["status"]) if r["status"] != "absent" or r["build_input"] else None
                        print(f"{r['status']:>12}  {r['source_id']}" + (f"  ({note})" if note else ""))
                if len(recs) > 20:
                    print(f"{'ok':>12}  {pid}: {sum(r['status'] == 'ok' for r in recs)} of {len(recs)} sources")
                blocking += [r["source_id"] for r in recs if r["blocking"]]
            print(f"\n{'FAILED' if blocking else 'ok'}: {len(blocking)} build-input problem(s)")
            return 1 if blocking else 0
        elif args.action == "fetch":
            if not pack:
                raise SystemExit("usage: irpacks sources fetch <pack-id>")
            res = sources.fetch(pack, force=args.force)
            errors = [r for r in res if r["status"] == "error"]
            for r in errors:
                print(f"       error  {r['source_id']}: {r['detail']}")
            for r in res:
                if r["status"] == "supply_manually":
                    print(f"supply manually  {r['source_id']}: no direct download URL; get the file from {r['url']}"
                          f" and place it at {r['local_path']}, then run `irpacks sources verify`")
            counts = {s: sum(1 for r in res if r["status"] == s)
                      for s in ("downloaded", "present", "supply_manually", "error")}
            print(json.dumps(counts))
            return 1 if errors else 0
        elif args.action == "discover":
            if not pack:
                raise SystemExit("usage: irpacks sources discover <pack-id>")
            from .discover import discover
            res = discover(pack)
            print(json.dumps(res, indent=2))
            print("next: review the article list, then `uv run irpacks sources lock` and `uv run irpacks build " + pack + "`")
        return 0
    if args.cmd == "build":
        from .packbuild import build_pack
        res = build_pack(args.pack, scope="pilot" if args.pilot else None, authoritative=args.authoritative)
        print(json.dumps({"out_dir": res["out_dir"], "status": res["status"], "timings": res["timings"],
                          "validation_passed": res["validation"]["passed"]}, indent=2))
        return 0 if res["validation"]["passed"] else 2
    if args.cmd == "validate":
        from .validate import validate_build
        res = validate_build(args.path)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            for ch in res["checks"]:
                print(f"{'PASS' if ch['passed'] else ch['severity'].upper():>7}  {ch['check']}"
                      + (f"  ({ch['count']})" if ch.get("count") else ""))
            print(f"\n{'passed' if res['passed'] else 'FAILED'}: {res['errors']} errors, {res['warnings']} warnings")
        return 0 if res["passed"] else 2
    if args.cmd == "index":
        from . import search
        dirs = [Path(d) for d in args.dirs] or sorted(p for p in (PROJECT_ROOT / "build" / "local").iterdir()
                                                      if (p / "chunks.jsonl").exists() and not p.name.endswith("-pilot"))
        import yaml
        aliases = {}
        for d in dirs:
            if (d / "pack.yaml").exists():
                py = yaml.safe_load((d / "pack.yaml").read_text(encoding="utf-8"))
                aliases[py["pack_id"]] = (py.get("retrieval") or {}).get("source_aliases", [])
        info = search.build_index(args.out, [d / "chunks.jsonl" for d in dirs], {"variant": "local-combined"},
                                  pack_aliases=aliases, index_text=args.index_text, dense=args.dense)
        print(json.dumps(info, indent=2))
        return 0
    if args.cmd == "search":
        from . import search
        res = search.search(_resolve_index(args), args.query, k=args.k, packs=args.filter_packs, corpus=args.corpus,
                            include_excluded=args.include_excluded, review_status=args.review_status,
                            authority=args.authority, applicability=args.applicability,
                            pack_version=args.pack_version, content_type=args.content_type,
                            rights_state=args.rights_state, mode=args.mode)
        _out(res, args.json, _fmt_search)
        return 0
    if args.cmd == "context":
        from .context import expand
        res = expand(_resolve_index(args), args.chunk_id, budget=args.budget, include_section=not args.no_section)
        _out(res, args.json, _fmt_context)
        return 0 if "error" not in res else 1
    if args.cmd == "section":
        from .mdrender import Renderer
        base = None
        for root in (PROJECT_ROOT / "build" / "local", PROJECT_ROOT / "dist" / "public" / "packs"):
            if (root / args.pack / "document.json").exists():
                base = root / args.pack
                break
        if base is None:
            raise SystemExit(f"pack {args.pack} not built")
        doc = read_json(base / "document.json")
        assets = {a["element_id"]: a for a in read_json(base / "assets" / "assets.json")["assets"]} \
            if (base / "assets" / "assets.json").exists() else {}
        els = {e["id"]: e for e in doc["elements"]}
        if args.element_id not in els:
            raise SystemExit(f"unknown element {args.element_id}")
        om = {o["id"]: o for o in doc.get("omitted", [])}
        r = Renderer(doc, assets, omitted=om)
        print("\n".join(r.render(els[args.element_id], "unit")))
        return 0
    if args.cmd == "export-public":
        from .public import export_public
        res = export_public(args.pack)
        print(json.dumps(res, indent=2))
        return 0 if res["validation_passed"] else 2
    if args.cmd == "models":
        from . import dense
        res = dense.fetch_model() if args.action == "fetch" else dense.verify_model()
        for r in res:
            print(f"{r['status']:>10}  {r['file']}" + (f": {r['detail']}" if r.get("detail") else ""))
        return 0 if all(r["status"] == "ok" for r in res) else 1
    if args.cmd == "eval":
        from .evaluate import run_eval
        res = run_eval(split=args.split, index=args.index, k=args.k, out=args.out, results_file=args.results_file,
                       ranking=args.ranking,
                       label=args.label, questions=args.questions)
        _out(res if args.json else res["summary"], True)
        return 0
    if args.cmd == "package":
        from .package import package_all
        print(json.dumps(package_all(version=args.version), indent=2))
        return 0
    if args.cmd == "check-updates":
        from .updates import check_updates
        rep = check_updates(args.pack, download=args.download)
        if args.json:
            print(json.dumps(rep, indent=2))
            return 0
        by = {}
        for r in rep["results"]:
            by[r["status"]] = by.get(r["status"], 0) + 1
        print(f"{args.pack}: {rep['sources_checked']} sources checked: "
              + ", ".join(f"{v} {k}" for k, v in sorted(by.items())))
        if by.get("page_template_changed"):  # article text, title and dates unchanged: counted, not listed
            print(f"  {'page_template_changed':>21}  {by['page_template_changed']} pages: the article is unchanged; "
                  f"only the page around it differs (site template, script nonces)")
        for r in rep["results"]:
            if r["status"] not in ("unchanged", "unknown", "page_template_changed"):
                what = ", ".join(d.get("field") or d.get("header") for d in r.get("differences", []))
                print(f"  {r['status']:>21}  {r['source_id']}" + (f"  ({what})" if what else "")
                      + (f"  {r['detail']}" if r.get("detail") else ""))
        for e in rep.get("toc_new_articles", []):
            print(f"  {'new in TOC':>21}  {e['url']}  ({e['toc']}: {e['toc_path']})")
        for u in rep.get("toc_removed_articles", []):
            print(f"  {'no longer in TOC':>21}  {u}")
        for e in rep.get("toc_errors", []):
            print(f"  {'TOC error':>21}  {e.get('toc', '')} {e['detail']}")
        print(f"report: {rep['report']}")
        return 0
    if args.cmd == "inspect-pdf":
        from . import pdfsource
        labels = pdfsource.page_labels(args.path)
        marks = pdfsource.bookmarks(args.path)
        print(json.dumps({"pages": len(labels), "labels_first": labels[:30], "bookmarks": len(marks),
                          "bookmark_list": marks}, indent=2))
        return 0
    if args.cmd == "review-page":
        from . import pdfsource
        base = PROJECT_ROOT / "build" / "local" / args.pack
        doc = read_json(base / "document.json")
        pm = read_json(base / "page-map.json")
        page = next((p for p in pm["pages"] if p["page"] == args.page), None)
        if page is None:
            raise SystemExit(f"page {args.page} not in page map")
        src = PROJECT_ROOT / doc["sources"]["authoritative"]["local_path"]
        out = PROJECT_ROOT / "build" / "review" / args.pack / f"p{args.page:03d}.png"
        pdfsource.render_page(str(src), args.page, out, scale=args.scale)
        els = {e["id"]: e for e in doc["elements"]}
        print(f"render: {out.relative_to(PROJECT_ROOT)}")
        print(f"page {args.page} (printed {page['page_label']}) class={page['content_class']} "
              f"coverage={page['coverage_status']} furniture={[f['text'] for f in page['furniture']]}")
        for g in page["uncovered_regions"]:
            if g["class"] not in ("list_or_callout_marker",):
                print(f"  uncovered[{g['class']}]: {g.get('text', '')[:120]}")
        for eid in page["element_ids"]:
            e = els[eid]
            t = (e["text"] or e["attrs"].get("caption") or "").replace("\n", " / ")
            print(f"  {eid:<34} {e['type']:<10} {e['mapping_status']:<26} {t[:args.chars]}")
        return 0
    if args.cmd == "countermap-example":
        sys.path.insert(0, str(PROJECT_ROOT / "examples" / "countermap"))
        from run_example import main as ex_main  # type: ignore
        argv2 = (["--countermap-db", args.countermap_db] if args.countermap_db else []) + \
                (["--countermap-bin", args.countermap_bin] if args.countermap_bin else []) + \
                (["--json"] if args.json else [])
        return ex_main(argv2)
    if args.cmd == "validate-mappings":
        from . import mappings
        data = mappings.load(args.path)
        doc = read_json(PROJECT_ROOT / "build" / "local" / data["pack"]["pack_id"] / "document.json")
        resolver = versions = None
        if args.countermap_db:
            resolver = mappings.countermap_resolver(args.countermap_db, args.countermap_bin)
            versions = mappings.countermap_versions(args.countermap_db, args.countermap_bin)
        res = mappings.validate(data, {e["id"]: e for e in doc["elements"]}, resolver, versions)
        res["framework_ids_checked"] = "against Countermap" if resolver else "NOT checked (no --countermap-db)"
        res["resolved"] = {k: v["name"] for k, v in res["resolved"].items()}
        print(json.dumps(res, indent=2))
        return 0 if res["passed"] else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
