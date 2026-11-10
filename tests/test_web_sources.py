"""Web sources beyond the first Learn pack: the AWS documentation profile, Learn layouts found at scale (tab groups,
FAQ wrappers, <details>, block-holding spans), table-of-contents discovery and TOC drift. Fixtures are synthetic
pages in each site's markup (tests/fixtures/web); no vendor text."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from irpacks import diffdoc, discover, route_learn, sources, validate
from irpacks.util import sha256_file

FIX = Path(__file__).resolve().parent / "fixtures" / "web"
META = {"pack_id": "web-test", "pack_version": "0.0.1", "document_id": "web-test:t", "edition_id": "t",
        "title": "Web test", "authors": [], "edition_statement": "t", "sources": {}}


def _run(name: str, url: str, route: str) -> dict:
    art = {"unit_id": "page", "url": url, "path": str(FIX / name), "source_id": "web-test/page/t",
           "sha256": sha256_file(FIX / name)}
    return route_learn.run([art], {"route": route, "inputs": {"snapshot": "t"}}, META, log=lambda *a: None)


def test_aws_profile_maps_the_article_and_skips_page_chrome():
    r = _run("aws-page.html", "https://docs.example-aws.invalid/svc/latest/ug/rotate.html", "aws_docs_html")
    doc = r["document"]
    els = {e["id"]: e for e in doc["elements"]}
    blob = json.dumps(doc)
    assert els["page"]["attrs"]["title"] == "Rotating example credentials"
    assert doc["route"] == "aws_docs_html" and els["page"]["extraction"]["method"] == "aws_html_dom"
    assert [e["id"] for e in doc["elements"] if e["type"] == "section"] == \
        ["page.identify-the-key", "page.key-fields", "page.key-states"]
    assert "Topics" not in blob and "Other page" not in blob and "tracking" not in blob  # chrome and scripts
    intro = els["page.p01"]
    assert intro["text"] == ("When an example key may be exposed, rotate it and review what it was used for. "
                             "Long lines in the source are wrapped.")  # source line wraps are spaces
    title = next(e for e in doc["elements"] if e["attrs"].get("heading_level") == 6)
    assert title["text"] == "To find the key owner" and title["md"] == "**To find the key owner**"
    code = next(e for e in doc["elements"] if e["type"] == "code")
    assert code["text"] == "example-cli list-keys --user alice" and code["parent_id"].startswith("page.identify-the-key.l")
    adm = next(e for e in doc["elements"] if e["type"] == "admonition")
    assert adm["attrs"]["kind"] == "warning"
    assert [els[c]["text"] for c in adm["children"]] == ["Deleting a key in use stops the workload that uses it."]
    terms = [e for e in doc["elements"] if e["attrs"].get("term")]
    assert [t["text"] for t in terms] == ["keyId", "status"] and terms[0]["md"] == "**`keyId`**"
    assert els[terms[1]["children"][0]]["text"] == "Either Active or Inactive."
    assert any(e["text"] == "Inactive keys cannot sign requests." for e in doc["elements"] if e["type"] == "list_item")
    assert not [i for i in r["issues"] if i["kind"] == "section_count_mismatch"], r["issues"]
    obs = route_learn.observe(FIX / "aws-page.html", "https://docs.aws.amazon.com/svc/latest/ug/rotate.html")
    assert obs["title"] == "Rotating example credentials" and obs["git_commit_id"] is None and obs["body_text_sha256"]


def test_learn_tabs_faq_details_and_wrapped_headings():
    r = _run("learn-structures.html", "https://learn.example.invalid/docs/test/structured", "learn_html_pages")
    doc = r["document"]
    els = {e["id"]: e for e in doc["elements"]}
    secs = [e["id"] for e in doc["elements"] if e["type"] == "section"]
    assert secs == ["page.configure", "page.configure-x", "page.faq-one", "page.wrapped"]
    assert len({els[s]["anchor"] for s in secs}) == len(secs)  # the same published anchor in two tabs stays unique
    tabs = [e["text"] for e in doc["elements"] if e["attrs"].get("tab_label")]
    assert tabs == ["Portal", "Command line"]
    blob = json.dumps(doc)
    assert "tablist" not in blob and "not article text" not in blob
    summary = next(e for e in doc["elements"] if e["attrs"].get("details_summary"))
    assert summary["md"] == "**Show more options**"
    assert any(e["text"] == "Option A keeps logs." for e in doc["elements"])
    assert any(e["type"] == "table" and e["parent_id"] == "page.wrapped" for e in doc["elements"])
    inline_heads = [e for e in doc["elements"] if e["attrs"].get("heading_level")]
    assert {e["text"] for e in inline_heads} == {"Inside a step", "Heading without an anchor"}
    lst = next(e for e in doc["elements"] if e["type"] == "list")
    assert lst["attrs"]["start"] == 3
    intro = els["page.p01"]
    assert "(https://learn.example.invalid/previous/cc12345%28v=ws.10%29)" in intro["md"]  # parentheses encoded
    assert not [i for i in r["issues"] if i["kind"] == "section_count_mismatch"], r["issues"]


def test_markdown_link_checks_ignore_escaped_brackets():
    assert validate._LINK.findall(r"\[Note\](..images/x.png)") == []
    assert validate._LINK.findall("[real](chapters/a.md#x)") == ["chapters/a.md#x"]


def test_toc_entries_select_and_canonical_urls():
    learn = {"items": [{"toc_title": "Root", "href": "./", "children": [
        {"toc_title": "Investigate", "children": [{"toc_title": "Isolate", "href": "isolate-devices"},
                                                  {"toc_title": "Other docset", "href": "/other/page?toc=x"},
                                                  {"toc_title": "External", "href": "https://example.com/x"}]},
        {"toc_title": "Deploy", "children": [{"toc_title": "Install", "href": "install"}]}]}]}
    entries = discover.toc_entries(learn)
    assert entries[1] == ("Root > Investigate > Isolate", "isolate-devices")
    toc_url = "https://learn.microsoft.com/en-us/defender-endpoint/toc.json"
    picked = discover.select(entries, {"include": ["> Investigate( >|$)"]}, toc_url)
    assert picked == [("Root > Investigate > Isolate", "https://learn.microsoft.com/en-us/defender-endpoint/isolate-devices", ""),
                      ("Root > Investigate > Other docset", "https://learn.microsoft.com/en-us/other/page",
                       "outside https://learn.microsoft.com/en-us/defender-endpoint/"),
                      ("Root > Investigate > External", None, "external link")]
    aws = {"contents": [{"title": "Guide", "href": "what-is.html", "contents": [{"title": "Findings", "href": "findings.html"}]}]}
    e2 = discover.toc_entries(aws)
    assert e2 == [("Guide", "what-is.html"), ("Guide > Findings", "findings.html")]
    got = discover.select(e2, {"include": ["Findings"]}, "https://docs.aws.amazon.com/svc/latest/ug/toc-contents.json")
    assert got == [("Guide > Findings", "https://docs.aws.amazon.com/svc/latest/ug/findings.html", "")]
    assert discover._slug("https://docs.aws.amazon.com/svc/latest/ug/guardduty_finding-types-ec2.html") == "guardduty-finding-types-ec2"


def _web_project(tmp_path, monkeypatch, toc_items):
    """A throwaway project with one discovered web pack; returns a curl stand-in serving the TOC and pages."""
    for mod in (sources, discover):
        monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)
    pdir = tmp_path / "packs" / "web-test"
    pdir.mkdir(parents=True)
    (pdir / "pack.yaml").write_text("recipe: recipe.yaml\n")
    (pdir / "recipe.yaml").write_text(yaml.safe_dump({
        "route": "learn_html_pages", "inputs": {"snapshot": "t"}, "artifacts_file": "articles.yaml",
        "discovery": {"base": "https://learn.microsoft.com/en-us/",
                      "rules": [{"toc": "test", "prefix": "tst", "include": [".*"]}]}}))
    pages = {"https://learn.microsoft.com/en-us/test/toc.json": json.dumps({"items": toc_items})}

    def runner(cmd):
        url, dest = cmd[-1], Path(cmd[cmd.index("-o") + 1])
        if url in pages:
            dest.write_text(pages[url])
        elif url.endswith("/landing"):
            dest.write_text('<html><head><meta name="ms.topic" content="landing-page"></head><body></body></html>')
        else:
            shutil.copy(FIX / "learn-structures.html", dest)
        return 0, url
    return pdir, runner


def test_discover_writes_the_article_list_and_reports_toc_drift(tmp_path, monkeypatch):
    items = [{"toc_title": "Docs", "children": [{"toc_title": "A", "href": "page-a"}, {"toc_title": "Hub", "href": "landing"},
                                                {"toc_title": "B", "href": "page-b"}]}]
    pdir, runner = _web_project(tmp_path, monkeypatch, items)
    (pdir / "articles.yaml").write_text(yaml.safe_dump({"artifacts": [{  # an existing article keeps its unit ID
        "source_id": "web-test/test/kept/t", "role": "authoritative", "unit_id": "kept-id",
        "url": "https://learn.microsoft.com/en-us/test/page-a", "local_path": "input/web-test/t/test/kept.html"}]}))
    res = discover.discover("web-test", runner=runner, log=lambda *a: None)
    listed = yaml.safe_load((pdir / "articles.yaml").read_text())
    assert res["articles"] == 2 and res["added"] == 1
    assert [a["unit_id"] for a in listed["artifacts"]] == ["kept-id", "tst-page-b"]
    assert listed["artifacts"][1]["toc"] == "test: Docs > B"
    assert [e["reason"] for e in listed["excluded"]] == ["not an article (ms.topic landing-page)"]
    assert (tmp_path / "input/web-test/t/test/page-b.html").exists() and not (tmp_path / "input/web-test/t/test/landing.html").exists()
    # later, the published TOC gains one article and loses another
    items[0]["children"] = [{"toc_title": "A", "href": "page-a"}, {"toc_title": "C", "href": "page-c"}]
    _, runner2 = _web_project(tmp_path / "again", monkeypatch, items)
    for mod in (sources, discover):
        monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)
    drift = discover.toc_drift("web-test", runner=runner2)
    assert [e["url"] for e in drift["toc_new_articles"]] == ["https://learn.microsoft.com/en-us/test/page-c"]
    assert drift["toc_removed_articles"] == ["https://learn.microsoft.com/en-us/test/page-b"]


def test_discover_keeps_the_unit_id_of_a_moved_article(tmp_path, monkeypatch):
    items = [{"toc_title": "Docs", "children": [{"toc_title": "A", "href": "new-a"}, {"toc_title": "B", "href": "page-b"}]}]
    pdir, serve = _web_project(tmp_path, monkeypatch, items)
    old_url, new_url = "https://learn.microsoft.com/en-us/old/page-a", "https://learn.microsoft.com/en-us/test/new-a"

    def runner(cmd):  # the old URL now redirects to the new one
        rc, out = serve(cmd)
        return rc, new_url if cmd[-1] == old_url else out
    (pdir / "articles.yaml").write_text(yaml.safe_dump({"artifacts": [
        {"source_id": "web-test/old/page-a/t", "role": "authoritative", "unit_id": "kept-a", "url": old_url,
         "local_path": "input/web-test/t/old/page-a.html", "toc": "old: Docs > A"},
        {"source_id": "web-test/old/gone/t", "role": "authoritative", "unit_id": "gone", "toc": "old: Docs > Gone",
         "url": "https://learn.microsoft.com/en-us/old/gone", "local_path": "input/web-test/t/old/gone.html"}]}))
    res = discover.discover("web-test", runner=runner, log=lambda *a: None)
    listed = yaml.safe_load((pdir / "articles.yaml").read_text())["artifacts"]
    assert [(a["unit_id"], a["url"]) for a in listed] == [("kept-a", new_url),
                                                          ("tst-page-b", "https://learn.microsoft.com/en-us/test/page-b")]
    assert listed[0]["moved_from"] == old_url and listed[0]["local_path"] == "input/web-test/t/old/page-a.html"
    assert res["moved"] == {old_url: new_url} and res["removed"] == ["https://learn.microsoft.com/en-us/old/gone"]


def test_fingerprint_report_counts_units_and_names_changed_sections():
    old = {"document_id": "p:1", "units": [{"unit_id": "u", "title": "U", "sections": [{"id": "u.s", "title": "S", "hash": "a"}]}]}
    new = {"document_id": "p:2", "units": [{"unit_id": "u", "title": "U", "sections": [{"id": "u.s", "title": "S", "hash": "b"}]},
                                           {"unit_id": "v", "title": "V", "sections": []}]}
    rep = diffdoc.diff_fingerprints(old, new)
    assert rep["units_compared"] == 1 and rep["units_added"] == ["v"]
    md = diffdoc.render_fingerprint_markdown(rep)
    assert "Units compared: 1; added: 1" in md and "section changed: S" in md
