"""Routes added for Google Cloud (DevSite profile), first.org (h1-section profile) and PDF collections converted
from the page layout (route_layout), plus the table-cell line separation shared by every web profile. Fixtures are
synthetic; the layout route is driven by synthetic line records, so no PDF is needed."""
from __future__ import annotations

import lxml.html

from irpacks import route_layout, route_learn
from irpacks.util import sha256_file
from test_web_sources import FIX, META, _run


def test_gcp_profile_alerts_tabs_cards_and_chrome():
    r = _run("gcp-page.html", "https://docs.cloud.google.com/example/rotate", "gcp_docs_html")
    doc = r["document"]
    els = {e["id"]: e for e in doc["elements"]}
    blob = " ".join(e.get("text") or "" for e in doc["elements"])
    assert els["page"]["attrs"]["title"] == "Rotate example keys"            # bookmark widget not in the title
    assert els["page"]["attrs"]["updated_at"] == "2026-10-01"
    assert els["page"]["attrs"]["page_license"] == "Creative Commons Attribution 4.0 License"
    assert not [i for i in r["issues"] if i["kind"] in ("page_license_unexpected", "section_count_mismatch")], r["issues"]
    adms = [e for e in doc["elements"] if e["type"] == "admonition"]
    assert [a["attrs"]["kind"] for a in adms] == ["note", "important"]
    assert [els[c]["text"] for a in adms for c in a["children"]] == [
        "Rotation does not revoke issued tokens.", "Keep the old key until clients switch."]   # labels dropped
    assert [e["id"] for e in doc["elements"] if e["type"] == "section"] == ["page.rotate", "page.checklist"]
    tabs = [e["text"] for e in doc["elements"] if e["attrs"].get("tab_label")]
    assert tabs == ["Console", "gcloud", "REST"]
    assert any(e["text"] == "Go to the Keys page." for e in doc["elements"])    # icon ligature dropped
    code = next(e for e in doc["elements"] if e["type"] == "code")
    assert code["attrs"]["lang"] == "bash" and code["text"] == "example keys rotate --key=k1"
    card = next(e for e in doc["elements"] if e["type"] == "table")
    assert card["attrs"]["rows"][0]["cells"][0]["text"] == "Subscribe to bulletins\nGet notified.\nReview weekly."
    items = [e["text"] for e in doc["elements"] if e["type"] == "list_item"]
    assert items == ["Install the CLI."] and any(e["text"] == "Sign in." for e in doc["elements"])  # malformed <ol>
    fig = next(e for e in doc["elements"] if e["type"] == "figure")
    assert fig["attrs"]["caption"] == "Figure 1. Key rotation flow"
    for chrome in ("Stay organized", "Try it for yourself", "Free credits", "Was this helpful", "One"):
        assert chrome not in blob


def test_first_profile_h1_sections_and_case_preserving_anchors():
    r = _run("first-page.html", "https://www.first.org/example", "first_org_html")
    doc = r["document"]
    secs = [(e["id"], e["attrs"]["anchor"], e["attrs"]["level"]) for e in doc["elements"] if e["type"] == "section"]
    assert secs == [("page.introduction", "Introduction", 2), ("page.1-1-definitions", "1-1-Definitions", 3),
                    ("page.usage", "Usage", 2)]
    els = {e["id"]: e for e in doc["elements"]}
    assert els["page"]["attrs"]["title"] == "Example Protocol"
    assert els["page.introduction"]["source_spans"][0]["url"] == "https://www.first.org/example#Introduction"
    assert "Copyright" not in " ".join(e.get("text") or "" for e in doc["elements"])  # outside div#c1
    assert not [i for i in r["issues"] if i["kind"] == "section_count_mismatch"], r["issues"]


def test_learn_table_cells_keep_paragraph_and_item_breaks():
    art = route_learn._Article.__new__(route_learn._Article)
    cell = lxml.html.fragment_fromstring("<td><p>First.</p><p>Second.</p><ul><li>a</li><li>b</li></ul></td>")
    assert route_learn._Article.inline(art, cell)[0] == "First.\nSecond.\na\nb"


def _line(page, top, text, x0=72.0, size=12.0, bold=False):
    return {"kind": "line", "page": page, "text": text, "x0": x0, "x1": x0 + 6 * len(text), "top": top,
            "bottom": top + size, "size": size, "n": len(text.replace(" ", "")), "bold": bold}


def test_layout_route_outline_front_matter_lists_and_page_runover():
    pages = [
        {"page": 1, "lines": [_line(1, 100, "Example Guide", size=14, bold=True),
                              _line(1, 130, "Disclaimer Regarding Use", bold=True),
                              _line(1, 150, "Use this guide at your own risk.")]},
        {"page": 2, "lines": [_line(2, 100, "1. Purpose", bold=True),
                              _line(2, 120, "This guide explains how to collect"),
                              _line(2, 134, "evidence from a remote host and"),
                              _line(2, 148, "verify it before analysis begins")],
         },
        {"page": 3, "lines": [_line(3, 100, "and storage."),
                              _line(3, 130, "2. Steps", bold=True),
                              _line(3, 150, "• Isolate the host", x0=90),
                              _line(3, 166, "o Disconnect the network cable", x0=126),
                              _line(3, 184, "or disable the interface", x0=144),
                              _line(3, 200, "• Image the disk", x0=90),
                              _line(3, 230, "2.1 Verification", bold=True),
                              _line(3, 250, "Hash the image.")]},
    ]
    for p in pages:
        p.update(width=612.0, height=792.0, tables=[], images=[], furniture=[], label=str(p["page"] - 1) if p["page"] > 1 else None)
    ex = {"pages": pages, "outline": [{"order": 0, "level": 0, "title": "Example Guide", "page": 1},
                                      {"order": 1, "level": 0, "title": "1. Purpose", "page": 2},
                                      {"order": 2, "level": 0, "title": "2. Steps", "page": 3}]}
    art = {"unit_id": "doc1", "url": "https://example.invalid/guide.pdf", "source_id": "t/doc1/1", "title": "Example Guide",
           "version": "1.0", "document_date": "2026-01-01"}
    issues: list = []
    d = route_layout._Doc(art, ex, issues)
    d.build()
    els = {e["id"]: e for e in d.elements}
    secs = [(e["id"], e["attrs"]["level"]) for e in d.elements if e["type"] == "section"]
    assert secs == [("doc1.front-matter", 2), ("doc1.s1", 2), ("doc1.s2", 2), ("doc1.s2-1", 3)]
    fm_text = [els[c]["text"] for c in els["doc1.front-matter"]["children"]]
    assert fm_text == ["Example Guide", "Disclaimer Regarding Use", "Use this guide at your own risk."]
    p = els["doc1.s1.p01"]
    assert p["text"] == ("This guide explains how to collect evidence from a remote host and verify it before "
                         "analysis begins and storage.")                          # runs over onto page 3
    assert [s["page"] for s in p["source_spans"]] == [2, 3] and p["source_spans"][1]["page_label"] == "2"
    assert p["source_spans"][0]["url"] == "https://example.invalid/guide.pdf#page=2"
    items = [(e["id"], e["text"]) for e in d.elements if e["type"] == "list_item"]
    assert items == [("doc1.s2.l01.i01", "Isolate the host"),
                     ("doc1.s2.l01.i01.l01.i01", "Disconnect the network cable or disable the interface"),
                     ("doc1.s2.l01.i02", "Image the disk")]
    assert els["doc1.s2-1.p01"]["text"] == "Hash the image." and not issues


def test_layout_furniture_is_repeated_text_in_the_margins():
    pages = []
    for n in range(1, 5):
        lines = [_line(n, 40, "Example Working Group"), _line(n, 300, f"Body text {n}"),
                 _line(n, 745 if n > 1 else 760, f"Page {n} of 4")]  # the cover prints its footer lower
        pages.append({"page": n, "height": 792.0, "lines": lines})
    furn = route_layout._furniture(pages)
    assert furn == {(n, i) for n in range(1, 5) for i in (0, 2)}


def test_layout_tables_ignore_white_text_backgrounds_and_sliver_columns():
    """Word draws white boxes behind runs of text (a cover page's justified paragraph with links became a 'table');
    a band's edge next to a rule makes a sliver column, which kept the bands of one table apart."""
    rect = {"object_type": "rect", "fill": True, "stroke": False}
    assert not route_layout._drawn(dict(rect, non_stroking_color=(1.0,)))         # white, DeviceGray
    assert not route_layout._drawn(dict(rect, non_stroking_color=(1, 1, 1)))      # white, DeviceRGB
    assert not route_layout._drawn(dict(rect, non_stroking_color=(0, 0, 0, 0)))   # white, DeviceCMYK
    assert route_layout._drawn(dict(rect, non_stroking_color=(0.949,)))          # a light grey band
    assert route_layout._drawn(dict(rect, non_stroking_color=(0.0,)))            # a rule drawn as a filled box
    assert route_layout._drawn(dict(rect, stroke=True, non_stroking_color=(1.0,)))  # a stroked frame
    assert route_layout._drawn({"object_type": "char", "non_stroking_color": (1.0,)})

    class Row:
        def __init__(self, cells):
            self.cells = cells

    class Table:
        def __init__(self, rows):
            self.rows = [Row(r) for r in rows]
    band = Table([[(85, 111, 244, 231), (244, 111, 250, 231), (250, 111, 535, 231), (535, 111, 541, 231)]])
    plain = Table([[(85, 272, 244, 298), (244, 272, 541, 298)]])
    assert route_layout._cols(band) == [(85, 250), (250, 541)]
    assert route_layout._same_cols(route_layout._cols(band), route_layout._cols(plain))
    assert not route_layout._same_cols([(85, 250), (250, 541)], [(85, 160), (160, 541)])
