"""Search ranking features and the evaluation scorer, on the offline fixture."""
from __future__ import annotations

from conftest import build_pack

from irpacks import evaluate, search


def test_named_source_alias_is_stripped_and_detected():
    q, named = search.named_sources("According to NIST SP 800-61r3, who takes part?",
                                    {"nist-sp800-61r3": ["NIST SP 800-61r3", "SP 800-61r3"], "dynamic-ir": ["Dynamic Incident Response"]})
    assert named == ["nist-sp800-61r3"]
    assert "800" not in q and "takes part" in q


def test_query_terms_quote_everything():
    terms, pairs = search.query_terms('T1539 "attacker designated" incident-handlers')
    assert '"attacker designated"' in terms and '"T1539"' in terms and '"incident handlers"' in terms
    assert all(t.startswith('"') and t.endswith('"') for t in terms + pairs)


def test_ranking_config_is_reported(tmp_path, mini):
    doc, pm = mini
    r = build_pack(tmp_path / "p", doc, pm)
    res = search.search(r["dir"] / "search.sqlite", "legal public affairs human resources", k=2)
    assert res["ranking"]["version"] == search.RANKING["version"]
    assert res["results"][0]["citation"]["pdf_pages"]


def _chunk(cid, els, pages, labels, spans=None):
    return {"chunk_id": cid, "pack_id": "p", "source_element_ids": els, "source_spans": spans or [],
            "citation": {"pdf_pages": pages, "printed_pages": labels}, "required_context_ids": [], "asset_refs": []}


def test_scorer_requires_all_groups_and_checks_citations(tmp_path):
    q = {"id": "x", "split": "tuning", "answerable": True,
         "evidence": [[{"pack": "p", "element_id": "a", "locators": [{"pdf_page": 3, "page_label": "1"}]}],
                      [{"pack": "p", "element_id": "b", "locators": [{"pdf_page": 4, "page_label": "2"}]}]]}
    chunks = {"c1": _chunk("c1", ["a"], [3], ["1"]), "c2": _chunk("c2", ["b"], [4], ["X"])}
    labels = {("p", 3): "1", ("p", 4): "2"}
    one = evaluate.score_question(q, ["c1"], chunks, tmp_path / "none.sqlite", 5, labels, 3000)
    assert one["evidence_complete"] is False and one["group_recall"] == 0.5
    both = evaluate.score_question(q, ["c1", "c2"], chunks, tmp_path / "none.sqlite", 5, labels, 3000)
    assert both["evidence_complete"] is True
    assert both["citation_valid"] is False, "printed label X disagrees with the page map"


def test_scorer_table_row_needs_native_locator(tmp_path):
    q = {"id": "y", "split": "tuning", "answerable": True,
         "evidence": [[{"pack": "p", "element_id": "t", "native_locator": "csf2:RS.MA-01",
                        "locators": [{"pdf_page": 9, "page_label": "5"}]}]]}
    wrong = {"c": _chunk("c", ["t"], [9], ["5"], [{"native_locator": "csf2:RS.MA-02"}])}
    right = {"c": _chunk("c", ["t"], [9], ["5"], [{"native_locator": "csf2:RS.MA-01"}])}
    labels = {("p", 9): "5"}
    assert evaluate.score_question(q, ["c"], wrong, tmp_path / "n", 5, labels, 3000)["evidence_complete"] is False
    assert evaluate.score_question(q, ["c"], right, tmp_path / "n", 5, labels, 3000)["evidence_complete"] is True
