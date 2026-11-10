"""Tagged-PDF route, outline mode (Word-tagged government PDFs such as the CISA playbooks): hierarchy from the PDF
outline, table captions and full-width checklist rows as headings, Word list quirks, hidden-text exclusion. The PDF
reader is replaced by a synthetic structure tree and character layer, so no PDF is needed."""
from __future__ import annotations

from pathlib import Path

from irpacks import pdfsource, route_tagged

_mcid = iter(range(10_000))
CHARS: list[dict] = []


def _text(s: str, y: float, x: float = 72, size: float = 11, font: str = "Body") -> list:
    """Characters for one line of text under a fresh marked-content ID; returns the MCR [page, mcid]."""
    m = next(_mcid)
    for i, ch in enumerate(s):
        CHARS.append({"text": ch, "x0": x + i * 5, "top": y, "x1": x + i * 5 + 4.5, "bottom": y + size, "size": size,
                      "mcid": m, "font": font})
    return [1, m]


def n(typ: str, *children, text: str | None = None, y: float = 100, x: float = 72, font: str = "Body") -> dict:
    refs = [_text(text, y, x, font=font)] if text is not None else []
    kids = [{"mc": r} for r in refs] + [{"node": i} for i in range(len(children))]  # /K order, as _struct_tree records it
    return {"type": typ, "mcrefs": refs, "children": list(children), "kids": kids, "alt_text": None, "attributes": {}}


def _run(monkeypatch, tree, marks, recipe_extra=None):
    page = {"page": 1, "width": 612, "height": 792, "rotation": 0, "chars": CHARS, "images": [], "links": [],
            "n_rects": 0, "n_curves": 0, "n_lines": 0}
    monkeypatch.setattr(route_tagged, "_extract", lambda *a, **k: {"pages": [page], "tree": tree, "from_cache": True})
    monkeypatch.setattr(pdfsource, "bookmarks", lambda *a, **k: marks)
    recipe = {"route": "tagged_pdf_structure", "structure": "outline", **(recipe_extra or {})}
    meta = {"document_id": "t:1", "pack_id": "t", "pack_version": "0", "edition_id": "1", "title": "T", "authors": [],
            "edition_statement": "1", "route": "tagged_pdf_structure", "sources": {"authoritative": {"sha256": "0"}}}
    return route_tagged.run("unused.pdf", recipe, meta, Path("unused"), log=lambda *a: None)


def test_outline_mode_builds_units_from_bookmarks_captions_and_checklist_rows(monkeypatch):
    CHARS.clear()
    tree = [n("Part",
              n("H1", text="Example Guide", y=60),                         # tagged heading not in the outline
              n("Sect", n("P", text="Introduction", y=90),                 # Word tags many headings as P
                        n("P", text="Body text of the introduction.", y=110)),
              n("P", text="Note: read the checklist below.", y=300),       # tagged before the title, printed below it
              n("Table",
                n("Caption", n("P", text="APPENDIX A: CHECKLIST", y=280)),
                n("TR", n("TH", text="Step", y=320), n("TH", text="Action", y=320, x=200)),
                n("TR", n("TD", text="Phase One", y=340), n("TD")),       # full-width row = outline entry
                n("TR", n("TD", text="1a.", y=360), n("TD", text="Declare the incident.", y=360, x=200))),
              n("L",
                n("LI", n("LBody", text=" Keep offline backups", y=400)),
                n("L", n("LI", n("LBody", text="o Test restores", y=420, x=90))),   # nested list as a child of L
                n("LI", n("LBody", text="of critical data.", y=440))),             # unbulleted: continues the item
              n("Table",
                n("TR", n("TD", text="Old change record", y=700)),        # hidden under artwork (recipe region)
                n("TR", n("TD", text="1.0", y=720)))),
            ]
    marks = [{"order": 0, "level": 0, "title": "Introduction", "page": 1},
             {"order": 1, "level": 0, "title": "Appendix A: Checklist", "page": 1},
             {"order": 2, "level": 0, "title": "Phase One", "page": 1}]
    r = _run(monkeypatch, tree, marks, {"exclude_hidden_text": [{"page": 1, "bbox": [0, 690, 612, 740], "reason": "Hidden."}]})
    els = {e["id"]: e for e in r["document"]["elements"]}
    units = [e["id"] for e in r["document"]["elements"] if e["type"] in ("chapter", "division")]
    assert units == ["fm-front-matter", "introduction", "appendix-a-checklist"]
    assert els["fm-front-matter.h01"]["attrs"]["discrete"] is True          # the cover title stays a heading in place
    assert els["introduction.h01"]["text"] == "Introduction"
    assert els["introduction.p01"]["text"] == "Body text of the introduction."
    app = els["appendix-a-checklist"]
    assert [els[c]["type"] for c in app["children"]][:3] == ["heading", "paragraph", "section"]
    assert els[app["children"][1]]["text"] == "Note: read the checklist below."   # moved below the title
    phase = els["appendix-a-checklist.s01"]
    assert phase["attrs"]["title"] == "Phase One"
    table = next(els[c] for c in phase["children"] if els[c]["type"] == "table")
    assert [[c["text"] for c in row["cells"]] for row in table["attrs"]["rows"]] == [["Step", "Action"], ["1a.", "Declare the incident."]]
    assert r["bookmark_roles"] == {2: "table_row_heading"}
    item = next(e for e in r["document"]["elements"] if e["type"] == "list_item" and e["text"].startswith("Keep"))
    assert item["text"] == "Keep offline backups of critical data."      # bullet glyph dropped, continuation joined
    nested = [els[c] for c in item["children"]]
    assert nested and nested[0]["type"] == "list" and els[nested[0]["children"][0]]["text"] == "Test restores"
    blob = " ".join(e.get("text") or "" for e in r["document"]["elements"])
    assert "Old change record" not in blob
    assert [i["kind"] for i in r["issues"]].count("source_hidden_text_excluded") == 1
    assert not [i for i in r["issues"] if i["kind"].startswith("coverage:")]  # every marked character accounted for


def test_table_title_row_becomes_caption_not_header(monkeypatch):
    CHARS.clear()
    tree = [n("Part",
              n("P", text="Contacts", y=60),
              n("Table",
                n("TR", n("TD", text="Response Contacts:", y=100)),       # narrower title row
                n("TR", n("TD", text="Contact", y=120), n("TD", text="Phone", y=120, x=200), n("TD", text="Role", y=120, x=350)),
                n("TR", n("TD", text="Insurer", y=140), n("TD", text="555-0100", y=140, x=200), n("TD", text="Claims", y=140, x=350))))]
    r = _run(monkeypatch, tree, [{"order": 0, "level": 0, "title": "Contacts", "page": 1}])
    table = next(e for e in r["document"]["elements"] if e["type"] == "table")
    assert table["attrs"]["caption"] == "Response Contacts:"
    assert [[c["text"] for c in row["cells"]] for row in table["attrs"]["rows"]] == [
        ["Contact", "Phone", "Role"], ["Insurer", "555-0100", "Claims"]]
    assert not [i for i in r["issues"] if i["kind"] == "table_row_unplaced"]
