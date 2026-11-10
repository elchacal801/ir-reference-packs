"""End-to-end export path on the offline fixture: Markdown, chunks, FTS5 index, context, validation."""
from __future__ import annotations

import json

from conftest import build_pack

from irpacks import search
from irpacks.context import expand
from irpacks.validate import validate_build


def test_fixture_pack_validates(tmp_path, mini):
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    v = validate_build(r["dir"], log=lambda *a, **k: None)
    assert v["passed"], [c for c in v["checks"] if not c["passed"]]
    assert v["errors"] == 0


def test_every_text_leaf_is_in_a_chunk_with_page_and_label(tmp_path, mini):
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    labels = {p["page"]: p["page_label"] for p in pm["pages"]}
    covered = {e for c in r["chunks"] for e in c["source_element_ids"]}
    leaves = [e["id"] for e in doc["elements"] if e["text"] and e["type"] not in ("heading",)]
    assert set(leaves) <= covered
    for c in r["chunks"]:
        for s in c["source_spans"]:
            if s["page"]:
                assert s["page_label"] == labels[s["page"]]
        assert c["citation"]["pdf_pages"], c["chunk_id"]


def test_table_rows_keep_native_locators(tmp_path, mini):
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    locs = {s["native_locator"] for c in r["chunks"] for s in c["source_spans"] if s.get("native_locator")}
    assert "csf2:RS.MA-01" in locs


def test_search_and_context(tmp_path, mini):
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    db = r["dir"] / "search.sqlite"
    res = search.search(db, "legal experts review incident response plans", k=3)
    assert res["results"], res
    top = res["results"][0]
    assert "sec2.s02" in top["chunk_id"]
    ex = expand(db, top["chunk_id"], budget=3000)
    assert ex["required_context_complete"] is True
    assert ex["seed"] == top["chunk_id"]
    assert ex["items"][0]["chunk_id"] == top["chunk_id"]


def test_chunk_ids_are_stable_across_rebuilds(tmp_path, mini):
    doc, pm = mini
    a = build_pack(tmp_path / "a", doc, pm)
    b = build_pack(tmp_path / "b", doc, pm)
    assert [c["chunk_id"] for c in a["chunks"]] == [c["chunk_id"] for c in b["chunks"]]
    assert [c["content_hash"] for c in a["chunks"]] == [c["content_hash"] for c in b["chunks"]]
    assert (a["dir"] / "book.md").read_bytes() == (b["dir"] / "book.md").read_bytes()
    assert json.loads((a["dir"] / "chunks.jsonl").read_text().splitlines()[0])["chunk_id"].startswith("nist-sp800-61r3:")


def test_bookmark_ranges_detect_misplaced_elements(mini):
    from irpacks.validate import bookmark_ranges
    doc, pm = mini
    els = {e["id"]: e for e in doc["elements"]}
    p2 = els["sec2.h01"]["source_spans"][0]["page"]
    p3 = els["sec3.h01"]["source_spans"][0]["page"]
    bms = [{"level": 0, "title": els["sec2.h01"]["text"], "page": p2},
           {"level": 0, "title": els["sec3.h01"]["text"], "page": p3}]
    ok = bookmark_ranges(doc, bms, pm["page_count"])
    assert ok["matched"] == 2 and not ok["unmatched"] and not ok["outside"]
    els["sec2.s02.p03"]["source_spans"][0]["page"] = p3 + 5  # an element placed after the next chapter starts
    bad = bookmark_ranges(doc, bms, pm["page_count"])
    assert [o[0] for o in bad["outside"]] == ["sec2.s02.p03"]
    assert bookmark_ranges(doc, [{"level": 0, "title": "Chapter 9. Nonexistent", "page": 1}], 50)["unmatched"]
