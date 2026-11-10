"""Synthetic Countermap integration example: framework context from Countermap + citable book passages.

    uv run irpacks countermap-example --countermap-db /path/to/cm.db [--countermap-bin /path/to/countermap]
    # or: uv run python examples/countermap/run_example.py --countermap-db /path/to/cm.db

For each step of scenario.yaml (synthetic) the report has three separately labelled parts:

1. **Countermap (official data)**: the technique as the local Countermap database resolves it, and the D3FEND
   countermeasures Countermap returns, with Countermap's own provenance field. These come from Countermap, read
   only through its CLI; this project never writes to Countermap or re-ingests ATT&CK, D3FEND, ATLAS or F3.
2. **Book mappings (this repository)**: entries from mappings/dynamic-ir-attack.yaml for the step's technique or
   its parent, each labelled source_explicit / analyst_mapping / derived. These are NOT Countermap edges.
3. **Keyword retrieval (not a mapping)**: top pack passages for the step description from the local index.

Without a Countermap database the example still runs in pack-only mode and says that Countermap lookups were not
performed. Nothing is fabricated in their place. The report is written under build/examples/countermap/ because
it quotes book text.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from irpacks import mappings, search  # noqa: E402
from irpacks.util import read_json, utc_now, write_json  # noqa: E402

PACK = "dynamic-ir"


def _cm(binary: str, db: str, *args: str) -> dict:
    p = subprocess.run([binary, "--db", db, *args], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"countermap {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return json.loads(p.stdout)


def _cite(el: dict) -> dict:
    pages = sorted({s["page"] for s in el["source_spans"] if s.get("page")})
    labels = []
    for s in el["source_spans"]:
        if s.get("page_label") and s["page_label"] not in labels:
            labels.append(s["page_label"])
    return {"pdf_pages": pages, "printed_pages": labels}


def run(db: str | None, binary: str | None, index: Path, out_dir: Path, k: int = 3) -> dict:
    scenario = yaml.safe_load((HERE / "scenario.yaml").read_text(encoding="utf-8"))
    mset = mappings.load(HERE / "mappings" / "dynamic-ir-attack.yaml")
    doc = read_json(ROOT / "build" / "local" / PACK / "document.json")
    els = {e["id"]: e for e in doc["elements"]}
    chunk_of: dict[str, str] = {}
    for c in search.all_chunks(index, PACK):
        for eid in c["source_element_ids"]:
            chunk_of.setdefault(eid, c["chunk_id"])

    cm_status: dict = {"available": False}
    resolver = None
    versions = None
    if db and binary and Path(db).exists() and shutil.which(binary):
        info = _cm(binary, db, "info")
        versions = {s["key"]: s["version"] for s in info["sources"] if s.get("status") == "loaded"}
        cm_status = {"available": True, "db": "provided", "built_at": info.get("built_at"),
                     "degraded": info.get("degraded"), "datasets": versions}
        resolver = mappings.countermap_resolver(db, binary)
    else:
        cm_status["reason"] = "no Countermap database/binary given; Countermap lookups were NOT performed"

    check = mappings.validate(mset, els, resolver, versions)
    by_fid = mappings.by_framework_id(mset)
    steps_out = []
    for st in scenario["steps"]:
        tid = st["technique"]
        parent = tid.split(".")[0] if "." in tid else None
        entry: dict = {"step": st["id"], "technique": tid, "description": st.get("description")}
        if cm_status["available"]:
            look = _cm(binary, db, "lookup", tid)
            entry["countermap"] = {
                "resolved_id": look["ext_id"], "name": look["name"], "url": look.get("url"),
                "dataset": look.get("source"), "warnings": look.get("warnings", []),
                "countermeasures": [
                    {"d3fend_id": m["d3fend_id"], "name": m["name"], "tactic": m["tactic"],
                     "countermap_provenance": m["provenance"], "via": m.get("via")}
                    for m in _cm(binary, db, "counter", tid)["countermeasures"][:scenario.get("max_countermeasures_per_step", 3)]
                ],
            }
        else:
            entry["countermap"] = None
        maps = []
        for fid in (tid, parent):
            for m in by_fid.get(fid, []) if fid else []:
                el = els[m["element_id"]]
                maps.append({"mapping_id": m["id"], "framework_id": m["framework_id"], "provenance": m["provenance"],
                             "relation": m["relation"], "review_status": m.get("review_status"),
                             "element_id": m["element_id"], "chunk_id": chunk_of.get(m["element_id"]),
                             "citation": _cite(el), "excerpt": " ".join((el.get("text") or "").split())[:240],
                             "rationale": m.get("rationale"), "derived_from": m.get("derived_from")})
        entry["book_mappings"] = maps
        res = search.search(index, st.get("description") or tid, k=k, packs=[PACK])
        entry["keyword_retrieval"] = [
            {"rank": r["rank"], "chunk_id": r["chunk_id"], "heading": " > ".join(r["heading_path"][-2:]),
             "printed_pages": r["citation"].get("printed_pages"), "pdf_pages": r["citation"].get("pdf_pages")}
            for r in res["results"]]
        steps_out.append(entry)

    report = {"generated_at": utc_now(), "synthetic": True, "scenario": scenario["title"], "countermap": cm_status,
              "mapping_validation": {k: check[k] for k in ("passed", "errors", "warnings")},
              "mapping_counts": {p: sum(1 for m in mset["mappings"] if m["provenance"] == p) for p in mappings.PROVENANCE},
              "steps": steps_out}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(render_md(report), encoding="utf-8")
    return report


def render_md(r: dict) -> str:
    L = [f"# {r['scenario']}", "",
         "> **Synthetic example.** Fictional organization and events. Generated " + r["generated_at"] + ".", "",
         "Three kinds of result are kept apart in every step:", "",
         "* **Countermap (official data)**: from the local Countermap database via its read-only CLI.",
         "* **Book mappings (this repository)**: `examples/countermap/mappings/dynamic-ir-attack.yaml`, labelled "
         "`source_explicit`, `analyst_mapping` or `derived`. *Not* Countermap or MITRE edges.",
         "* **Keyword retrieval (not a mapping)**: local FTS5 search over the pack for the step description.", ""]
    cm = r["countermap"]
    L.append("Countermap: " + (f"available; datasets {json.dumps(cm['datasets'])}" if cm["available"]
                               else f"**not used**: {cm['reason']}."))
    v = r["mapping_validation"]
    L += ["", f"Mapping file validation: {'passed' if v['passed'] else 'FAILED'}"
          f" ({len(v['errors'])} errors, {len(v['warnings'])} warnings); entries by provenance: "
          + ", ".join(f"{k} {n}" for k, n in r["mapping_counts"].items()), ""]
    for e in v["errors"]:
        L.append(f"* error: {e}")
    for s in r["steps"]:
        L += ["", f"## {s['step']} · {s['technique']}", "", s["description"] or "", ""]
        c = s["countermap"]
        if c:
            L.append(f"**Countermap (official data):** {c['resolved_id']} {c['name']} "
                     f"(ATT&CK {c['dataset'].get('version') if c.get('dataset') else '?'})")
            for w in c["warnings"]:
                L.append(f"* warning: {w}")
            for m in c["countermeasures"]:
                L.append(f"* D3FEND {m['d3fend_id']} {m['name']} ({m['tactic']}; Countermap provenance: "
                         f"{m['countermap_provenance']}{', via ' + m['via'] if m.get('via') else ''})")
        else:
            L.append("**Countermap (official data):** not looked up (no database).")
        L += ["", "**Book mappings (this repository):**"]
        if not s["book_mappings"]:
            L.append("* none recorded for this technique or its parent. See keyword retrieval below.")
        for m in s["book_mappings"]:
            pg = ", ".join(m["citation"]["printed_pages"])
            L.append(f"* `{m['provenance']}` {m['framework_id']} → `{m['element_id']}` (p. {pg}; PDF p. "
                     f"{', '.join(map(str, m['citation']['pdf_pages']))})"
                     + (f"; review: {m['review_status']}" if m.get("review_status") else "")
                     + (f"; derived from {', '.join(m['derived_from'])}" if m.get("derived_from") else ""))
            if m.get("rationale"):
                L.append(f"  * rationale: {m['rationale']}")
            L.append(f"  * excerpt: “{m['excerpt']}…”")
        L += ["", "**Keyword retrieval (not a mapping):**"]
        for h in s["keyword_retrieval"]:
            L.append(f"{h['rank']}. {h['heading']} (p. {', '.join(h['printed_pages'] or [])}) `{h['chunk_id']}`")
    L += ["", "---", "Book text: *Dynamic Incident Response: A Framework for Security Teams*, Joshua Wright, "
          "CC BY 4.0 (see the pack's ATTRIBUTION.md). MITRE ATT&CK® and D3FEND™ data via Countermap under their "
          "respective terms.", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--countermap-db", default=os.environ.get("COUNTERMAP_DB"))
    ap.add_argument("--countermap-bin", default=os.environ.get("COUNTERMAP_BIN", "countermap"))
    ap.add_argument("--index", default=str(ROOT / "build" / "local" / "index.sqlite"))
    ap.add_argument("--out", default=str(ROOT / "build" / "examples" / "countermap"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = run(a.countermap_db, a.countermap_bin, Path(a.index), Path(a.out))
    if a.json:
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(f"wrote {Path(a.out) / 'report.md'}; Countermap {'used' if rep['countermap']['available'] else 'NOT used'}; "
              f"mapping validation {'passed' if rep['mapping_validation']['passed'] else 'FAILED'}")
    return 0 if rep["mapping_validation"]["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
