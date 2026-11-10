"""Ranking v3 building blocks: plain-text indexing, question concepts and synonyms, passage coverage, the coverage
summary, the lexical fallback when dense retrieval is unavailable, and the MCP per-source search."""
from __future__ import annotations

from conftest import build_pack

from irpacks import search


def test_plain_text_drops_link_targets_and_bare_urls():
    md = ("Use [diagnostic settings](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/x) or see "
          "<https://example.org/a/b>; raw https://example.org/c also.")
    assert search.plain_text(md) == "Use diagnostic settings or see  ; raw   also."


def test_concepts_stem_and_share_synonyms():
    cons = search.concepts("What steps confirm that containment actually worked?")
    words = [w for w, _ in cons]
    assert words == ["steps", "confirm", "containment", "actually", "worked"]   # function words dropped
    alts = dict(cons)
    assert search.stem("validate") in alts["confirm"] and search.stem("effectiveness") in alts["worked"]
    assert search.stem("isolation") in alts["containment"]


def test_best_passage_coverage_is_not_diluted_by_long_chunks():
    cons = search.concepts("validate containment effectiveness")
    w = [1.0] * len(cons)
    long_text = ("Unrelated paragraph about something else entirely.\n\n" * 30
                 + "Validate containment effectiveness before moving on.")
    assert search.best_passage_coverage("Step 7", long_text, cons, w) == 1.0
    assert search.best_passage_coverage("Step 7", "Containment only.", cons, w) == 1 / 3


def test_coverage_summary_levels_and_missing_terms():
    cons = search.concepts("Tesla infotainment acquisition")
    res = [{"match": {"coverage": 0.3, "matched_terms": ["acquisition"], "missing_terms": ["Tesla", "infotainment"]}}]
    s = search.coverage_summary(res, cons)
    assert s["level"] == "low" and s["terms_in_no_result"] == ["Tesla", "infotainment"]
    res[0]["match"]["coverage"] = 0.8
    assert search.coverage_summary(res, cons)["level"] == "high"


def test_v3_falls_back_to_lexical_without_vectors_and_reports_signals(tmp_path, mini):
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    res = search.search(r["dir"] / "search.sqlite", "legal public affairs human resources", k=2)
    assert res["ranking"]["version"] == search.RANKING["version"] == "4"
    v3 = search.search(r["dir"] / "search.sqlite", "legal public affairs human resources", k=2, ranking=search.RANKING_V3)
    assert v3["ranking"]["version"] == "3" and not v3["ranking"].get("v4_tokens")   # frozen v3 is reproducible
    assert res["ranking"]["dense_weight"] == 0.0 and res["ranking"]["rerank_weight"] == 0.3  # lexical fallback
    assert res["dense"] and res["dense"] != "on"                                            # says why
    assert res["coverage"]["level"] in ("high", "medium", "low")
    assert "match" in res["results"][0] and 0.0 <= res["results"][0]["match"]["coverage"] <= 1.0
    v2 = search.search(r["dir"] / "search.sqlite", "legal public affairs human resources", k=2, ranking=search.RANKING_V2)
    assert v2["ranking"]["version"] == "2" and v2["dense"] is None


def test_dense_top_is_taken_within_the_allowed_chunks(monkeypatch):
    """A filtered search ranks by similarity within its filter, not by the index-wide dense top."""
    import pytest
    np = pytest.importorskip("numpy")  # the optional `dense` extra
    from irpacks import dense
    ids = ["a1", "a2", "b1", "b2"]
    mat = np.array([[1.0, 0.0], [0.9, 0.1], [0.2, 0.8], [0.1, 0.9]], dtype=np.float32)

    class Fake:
        def encode(self, texts):
            return np.array([[1.0, 0.0]], dtype=np.float32)

    monkeypatch.setattr(dense, "available", lambda: (True, ""))
    monkeypatch.setattr(dense, "vectors", lambda db: (ids, mat))
    monkeypatch.setattr(dense, "encoder", lambda: Fake())
    top_all, _ = dense.query_scores("x", "q", 2)
    assert list(top_all) == ["a1", "a2"]
    top_b, _ = dense.query_scores("x", "q", 2, allowed={"b1", "b2"})
    assert list(top_b) == ["b1", "b2"] and top_b["b1"] > top_b["b2"]
    assert dense.query_scores("x", "q", 5, allowed={"b2"})[0] == {"b2": top_b["b2"]}   # never pads with others


def test_mcp_search_per_source_returns_one_list_per_named_pack(tmp_path, mini):
    import pytest
    pytest.importorskip("mcp")
    from irpacks import mcp_server
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    server = mcp_server.create_server(r["dir"] / "search.sqlite")
    tool = next(t for t in server._tool_manager.list_tools() if t.name == "search") if hasattr(server, "_tool_manager") else None
    if tool is None:
        pytest.skip("tool registry not exposed by this MCP SDK version")
    pack = next(iter(mcp_server.PackStore(r["dir"] / "search.sqlite").chunk_counts))
    out = tool.fn(query="legal public affairs", packs=[pack, pack], per_source=True)
    assert out["per_source"] and [g["pack"] for g in out["by_source"]] == [pack, pack]


def test_v4_compound_tokens_and_comparison_words():
    q = "What do the TLP standard and CISA each say about AMBER+STRICT?"
    passage = "TLP:AMBER+STRICT restricts sharing to the recipient organization only."
    v3 = search.concepts(q)
    v4 = search.concepts(q, v4=True)
    assert "say" in [w for w, _ in v3]                                         # v3 counts it as content
    assert "say" not in [w for w, _ in v4]                                     # v4 does not
    amber = [c for c in v4 if c[0] == "AMBER+STRICT"]
    w = [1.0] * len(amber)
    assert search.best_passage_coverage("", passage, [c for c in v3 if c[0] == "AMBER+STRICT"], w) == 0.0
    assert search.best_passage_coverage("", passage, amber, w, v4=True) == 1.0   # tlp:amber+strict holds its parts


def test_similarity_levels_need_both_words_and_meaning():
    cons = search.concepts("validate containment effectiveness")
    res = [{"match": {"coverage": 0.9, "matched_terms": ["validate"], "missing_terms": []}}]
    assert search.coverage_summary(res, cons)["level"] == "high"                         # words only (no dense)
    assert search.coverage_summary(res, cons, best_sim=0.70)["level"] == "low"           # far in meaning
    assert search.coverage_summary(res, cons, best_sim=0.76)["level"] == "medium"
    out = search.coverage_summary(res, cons, best_sim=0.81)
    assert out["level"] == "high" and out["best_similarity"] == 0.81


def test_navigation_sections_leave_default_retrieval():
    from irpacks.packbuild import make_corpus_rules
    rules = make_corpus_rules({"route": "learn_html_pages"})["classify"]
    els = {"a": {"id": "a", "type": "chapter", "parent_id": None, "attrs": {"title": "Article"}},
           "a.related": {"id": "a.related", "type": "section", "parent_id": "a", "attrs": {"title": "Related content"}},
           "a.steps": {"id": "a.steps", "type": "section", "parent_id": "a", "attrs": {"title": "Steps"}}}
    para = [{"type": "paragraph"}]
    assert rules({"_section": "a.related", "_els": para}, els)[:2] == ("navigation", False)
    assert rules({"_section": "a.steps", "_els": para}, els)[:2] == ("narrative", True)
    assert make_corpus_rules({"route": "tagged_pdf_structure"})["classify"]({"_section": "a.related", "_els": para}, els)[0] == "narrative"


def test_dense_index_rebuild_reuses_unchanged_vectors(tmp_path, mini, monkeypatch):
    import pytest
    np = pytest.importorskip("numpy")
    from irpacks import dense
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    calls = []

    class Fake:
        def encode(self, texts):
            calls.append(len(texts))
            return np.ones((len(texts), dense.DIM), dtype=np.float32)

    monkeypatch.setattr(dense, "available", lambda: (True, ""))
    monkeypatch.setattr(dense, "encoder", lambda: Fake())
    monkeypatch.setattr(dense, "model_label", lambda: "fake-model")
    db = tmp_path / "combined.sqlite"
    search.build_index(db, [r["dir"] / "chunks.jsonl"], dense=True, log=lambda *a: None)
    first = calls[-1]
    search.build_index(db, [r["dir"] / "chunks.jsonl"], dense=True, log=lambda *a: None)
    assert first > 0 and len(calls) == 1                     # the second build embedded nothing new
    monkeypatch.setattr(dense, "model_label", lambda: "another-model")
    search.build_index(db, [r["dir"] / "chunks.jsonl"], dense=True, log=lambda *a: None)
    assert calls[-1] == first                                # a different model re-embeds everything
