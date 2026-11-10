"""Web-article route (Microsoft Learn markup) and change detection, on synthetic fixture pages (tests/fixtures/learn:
hand-written pages in Learn's markup; no Microsoft text). v2 changes a command, adds a warning to a procedure
step, removes a section, retitles a section and bumps the source commit."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from irpacks import diffdoc, packbuild, route_learn, sources, updates
from irpacks.util import sha256_file, write_json

FIX = Path(__file__).resolve().parent / "fixtures" / "learn"
URL = "https://learn.example.invalid/docs/test/contain-account"
META = {"pack_id": "web-test", "pack_version": "0.0.1", "document_id": "web-test:t", "edition_id": "t",
        "title": "Web test", "authors": [], "edition_statement": "t", "sources": {}}


def _doc(name: str) -> dict:
    art = {"unit_id": "acct", "url": URL, "path": str(FIX / name), "source_id": "web-test/acct/t",
           "sha256": sha256_file(FIX / name)}
    return route_learn.run([art], {"inputs": {"snapshot": "t"}}, META, log=lambda *a: None)


def test_learn_route_maps_the_article_body():
    r = _doc("v1.html")
    els = {e["id"]: e for e in r["document"]["elements"]}
    unit = els["acct"]
    assert unit["attrs"]["title"] == "Contain a compromised test account"
    assert unit["attrs"]["git_commit_id"].startswith("1111") and unit["attrs"]["updated_at"] == "2026-01-20T10:00:00Z"
    secs = [e["id"] for e in r["document"]["elements"] if e["type"] == "section"]
    assert secs == ["acct.prerequisites", "acct.steps", "acct.verify", "acct.legacy-module", "acct.next-steps"]
    assert els["acct.verify"]["parent_id"] == "acct.steps"  # h3 nests under h2
    codes = [e for e in r["document"]["elements"] if e["type"] == "code"]
    assert [c["text"] for c in codes] == ["Disable-ExampleAccount -Identity testuser", "Revoke-ExampleSession -UserId testuser"]
    assert all(c["attrs"]["lang"] == "powershell" for c in codes)
    assert codes[0]["parent_id"].endswith(".i01")  # the command stays with its step
    adm = [e for e in r["document"]["elements"] if e["type"] == "admonition"]
    assert [a["attrs"]["kind"] for a in adm] == ["note"]
    assert els[adm[0]["children"][0]]["text"] == "Sessions end within one hour."  # "Note" label not kept as text
    items = [e for e in r["document"]["elements"] if e["type"] == "list_item" and e["attrs"].get("checked") is False]
    assert len(items) == 2  # checklist
    intro = els["acct.p01"]
    assert "[the overview](https://learn.example.invalid/docs/other/page)" in intro["md"]  # relative link made absolute
    assert "window.analytics" not in json.dumps(r["document"]) and "Navigation that is not" not in json.dumps(r["document"])
    fig = next(e for e in r["document"]["elements"] if e["type"] == "figure")
    assert fig["attrs"]["src_url"] == "https://learn.example.invalid/docs/test/media/flow.png" and fig["attrs"]["captured"] is False
    t = next(e for e in r["document"]["elements"] if e["type"] == "table")
    assert t["attrs"]["rows"][0]["section"] == "thead" and t["attrs"]["rows"][1]["cells"][1]["text"] == "Revocation incomplete"
    for e in r["document"]["elements"]:
        for s in e["source_spans"]:
            assert s["locator_type"] == "section_anchor" and s["url"].startswith(URL)


def test_semantic_diff_reports_what_changed_and_how_much_it_matters():
    rep = diffdoc.diff_documents(_doc("v1.html")["document"], dict(_doc("v2.html")["document"], document_id="web-test:t2"))
    u = rep["units"][0]
    assert u["metadata_changes"]["git_commit_id"]["new"].startswith("2222")
    assert u["metadata_changes"]["updated_at"] == {"old": "2026-01-20T10:00:00Z", "new": "2026-03-02T09:30:00Z"}
    kinds = {(c["change"], c.get("element_type"), c["severity"]) for c in u["changes"]}
    assert ("changed", "code", "high") in kinds                       # a command changed
    assert ("added", "paragraph", "high") in kinds                    # warning text added to a procedure step
    assert any(c["change"] == "section_removed" and c["section_path"][-1] == "Legacy module" for c in u["changes"])
    assert any(c["change"] == "section_retitled" and c["new_title"] == "Verify revocation" for c in u["changes"])
    code = next(c for c in u["changes"] if c["element_type"] == "code")
    assert code["diff"] == ["@@ -1 +1 @@", "-Revoke-ExampleSession -UserId testuser",
                            "+Revoke-ExampleSession -UserId testuser -All"]  # code: line diff, not word edits
    assert u["changes"][0]["severity"] == "high"  # most important first
    md = diffdoc.render_markdown(rep)
    assert "**HIGH** changed code" in md and "-All" in md


def test_identical_content_produces_no_changes():
    rep = diffdoc.diff_documents(_doc("v1.html")["document"], _doc("v1-template-only.html")["document"])
    assert rep["units"] == [] and rep["summary"]["changes_by_severity"] == {"high": 0, "medium": 0, "low": 0}


def _lock_for(tmp_path, monkeypatch, captured: str):
    obs = route_learn.observe(FIX / captured, URL)
    entry = {"source_id": "web-test/acct/t", "pack_id": "web-test", "role": "authoritative", "status": "available",
             "url": URL, "local_path": "input/web-test/t/docs/contain-account.html", "sha256": sha256_file(FIX / captured),
             "observations": obs}
    write_json(tmp_path / "sources.lock.json", {"lock_version": 1, "sources": [entry]})
    monkeypatch.setattr(sources, "LOCK_PATH", tmp_path / "sources.lock.json")
    monkeypatch.setattr(updates, "PROJECT_ROOT", tmp_path)


@pytest.mark.parametrize("served,status", [("v1.html", "unchanged"), ("v1-template-only.html", "page_template_changed"),
                                          ("v2.html", "content_changed")])
def test_update_check_compares_article_content_not_bytes(tmp_path, monkeypatch, served, status):
    _lock_for(tmp_path, monkeypatch, "v1.html")

    def runner(cmd):  # stands in for curl: "downloads" the served fixture to the -o path
        shutil.copy(FIX / served, cmd[cmd.index("-o") + 1])
        return 0, ""
    res = updates.check_updates("web-test", runner=runner, out_dir=tmp_path)
    r = res["results"][0]
    assert r["status"] == status, r
    if status == "content_changed":
        assert {d["field"] for d in r["differences"]} >= {"git_commit_id", "body_text_sha256"}
        assert res["changed"] == ["web-test/acct/t"]


def test_history_fingerprints_redact_uncleared_units_and_still_detect_changes(tmp_path):
    def build(name, rights_state):
        r = _doc(name)
        for e in r["document"]["elements"]:
            e["rights"] = {"state": rights_state, "rule": "t"}
        d = tmp_path / f"{name}-{rights_state}"
        write_json(d / "document.json", r["document"])
        return d
    fp_open = diffdoc.fingerprint(build("v1.html", "redistributable"))
    assert fp_open["units"][0]["sections"][0]["title"] == "Prerequisites"
    fp1 = diffdoc.fingerprint(build("v1.html", "pending_review"))
    fp2 = diffdoc.fingerprint(build("v2.html", "pending_review"))
    blob = json.dumps(fp1)
    assert "Prerequisites" not in blob and "Contain a compromised" not in blob and "prerequisites" not in blob
    rep = diffdoc.diff_fingerprints(fp1, fp2)
    changes = {c["change"] for c in rep["units"][0]["section_changes"]}
    assert {"removed", "changed"} <= changes
    assert rep["units"][0]["metadata_changes"]["git_commit_id"]["new"].startswith("2222")


def test_web_pack_builds_validates_and_cites_urls(tmp_path):
    shutil.copy(packbuild.PROJECT_ROOT / "packs" / "ms-learn-ir" / "pack.yaml", tmp_path / "pack.yaml")
    pack = sources.load_yaml(tmp_path / "pack.yaml")
    recipe = {"route": "learn_html_pages", "inputs": {"snapshot": "t"}, "artifacts": [],
              "chunking": {"min_tokens": 600, "max_tokens": 1000, "overlap_tokens": 100, "overlap_min_tokens": 80,
                           "overlap_max_tokens": 120}, "corpus": {"default": "narrative", "rules": []}}
    rights_cfg = {"default_state": "redistributable", "default_basis": "test", "rules": []}
    arts = [{"source_id": "ms-learn-ir/test/acct/t", "unit_id": "acct", "url": URL, "path": str(FIX / "v1.html"),
             "sha256": sha256_file(FIX / "v1.html"), "size_bytes": 1, "local_path": "input/x/t/docs/acct.html"}]
    res = packbuild._build_web("ms-learn-ir", pack, recipe, rights_cfg, tmp_path, time.time(), lambda *a, **k: None,
                               arts=arts, out_dir=tmp_path / "out")
    assert res["validation"]["passed"], [c for c in res["validation"]["checks"] if not c["passed"]]
    chunks = [json.loads(x) for x in (tmp_path / "out" / "chunks.jsonl").read_text().splitlines()]
    assert len(chunks) == 1  # a short article is one chunk; its citation lists every section it covers
    cit = chunks[0]["citation"]
    assert cit["url"] == URL and cit["pdf_pages"] == []
    assert URL + "#steps" in cit["urls"] and URL + "#verify" in cit["urls"]
    assert {s["url"] for s in chunks[0]["source_spans"]} == set(cit["urls"])
    assert cit["text"] == ("Microsoft, “Contain a compromised test account”, learn.example.invalid "
                           "(updated 2026-01-20; source commit 1111111), " + URL)
    assert (tmp_path / "out" / "source-map.json").exists() and not (tmp_path / "out" / "page-map.json").exists()
