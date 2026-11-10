"""Regenerate tests/fixtures/nist-mini/ from a local NIST SP 800-61r3 build.

The fixture is a small, redistributable slice of the canonical NIST document (U.S. government work, not subject
to copyright in the United States; attribution: NIST SP 800-61r3, April 2025): the document root, chapters 2 and
3 with their headings, section 2.2 (roles and responsibilities, a list), and section 3.2 (a CSF profile table with
native locators). Tests add synthetic elements to copies of it; no other source text is checked in.

    uv run irpacks build nist-sp800-61r3 && uv run python tests/fixtures/make_nist_mini.py
"""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "build" / "local" / "nist-sp800-61r3"
OUT = pathlib.Path(__file__).resolve().parent / "nist-mini"
KEEP_SUBTREES = ["sec2.s02", "sec3.s02"]


def main() -> None:
    doc = json.loads((SRC / "document.json").read_text(encoding="utf-8"))
    pm = json.loads((SRC / "page-map.json").read_text(encoding="utf-8"))
    els = {e["id"]: e for e in doc["elements"]}

    def subtree(i):
        out = [i]
        for c in els[i]["children"]:
            out += subtree(c)
        return out
    keep = {doc["root_id"]}
    for s in KEEP_SUBTREES:
        keep.update(subtree(s))
        p = els[s]["parent_id"]
        while p:
            keep.add(p)
            keep.update(c for c in els[p]["children"] if els[c]["type"] == "heading")
            p = els[p]["parent_id"]
    out_els = []
    for e in doc["elements"]:
        if e["id"] in keep:
            e = dict(e, children=[c for c in e["children"] if c in keep], order=len(out_els))
            out_els.append(e)
    pages = sorted({s["page"] for e in out_els for s in e["source_spans"] if s.get("page")})
    doc.update(elements=out_els, element_count=len(out_els),
               page_labels=[p for p in doc["page_labels"] if p["page"] in pages])
    pm["pages"] = [dict(p, element_ids=[i for i in p["element_ids"] if i in keep]) for p in pm["pages"] if p["page"] in pages]
    OUT.mkdir(exist_ok=True)
    (OUT / "document.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (OUT / "page-map.json").write_text(json.dumps(pm, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(out_els)} elements, pages {pages}")


if __name__ == "__main__":
    main()
