"""Source-registry CLI behaviour found during the pilot setup dry run (docs/pilot-dryrun-2026-10-25.md)."""
from __future__ import annotations

import pytest

from irpacks import cli, sources
from irpacks.util import sha256_file, write_json


@pytest.mark.parametrize("argv", [["sources", "fetch", "dynamic-ir"], ["sources", "fetch", "--pack", "dynamic-ir"]])
def test_fetch_accepts_the_documented_positional_pack(monkeypatch, argv):
    seen = []
    monkeypatch.setattr(sources, "fetch", lambda pack, force=False: seen.append(pack) or [])
    assert cli.main(argv) == 0
    assert seen == ["dynamic-ir"]


def test_fetch_without_pack_is_a_usage_error(monkeypatch):
    monkeypatch.setattr(sources, "fetch", lambda *a, **k: pytest.fail("must not fetch"))
    with pytest.raises(SystemExit, match="usage"):
        cli.main(["sources", "fetch"])


def test_fetch_skips_artifacts_without_a_direct_download_url(tmp_path, monkeypatch, capsys):
    art = {"source_id": "x/pdf", "role": "authoritative", "url": "https://example.org/landing-page", "download": False,
           "local_path": "input/x/guide.PDF"}
    monkeypatch.setattr(sources, "load_recipe", lambda pid: {"artifacts": [art]})
    monkeypatch.setattr(sources, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sources.subprocess, "run", lambda *a, **k: pytest.fail("must not download a landing page"))
    assert cli.main(["sources", "fetch", "x"]) == 0
    out = capsys.readouterr().out
    assert "supply manually  x/pdf" in out and "input/x/guide.PDF" in out
    assert not (tmp_path / "input/x/guide.PDF").exists()


def _lock(tmp_path, monkeypatch, entries):
    for e in entries:
        p = tmp_path / e["local_path"]
        if e.pop("_content", None) is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"original")
            e.setdefault("sha256", sha256_file(p))
        e.setdefault("sha256", "0" * 64)
    write_json(tmp_path / "sources.lock.json", {"lock_version": 1, "sources": entries})
    monkeypatch.setattr(sources, "LOCK_PATH", tmp_path / "sources.lock.json")
    monkeypatch.setattr(sources, "PROJECT_ROOT", tmp_path)


def test_verify_blocks_only_on_build_inputs(tmp_path, monkeypatch, capsys):
    _lock(tmp_path, monkeypatch, [
        {"source_id": "x/pdf", "pack_id": "x", "role": "authoritative", "status": "available",
         "local_path": "input/x.pdf", "_content": True},
        {"source_id": "x/site", "pack_id": "x", "role": "landing_page", "status": "available",
         "local_path": "input/site.html", "_content": True},
        {"source_id": "x/reader-copy", "pack_id": "x", "role": "authoritative", "status": "not_available_locally",
         "local_path": "input/reader.pdf"},
    ])
    (tmp_path / "input/site.html").write_bytes(b"page with a fresh per-request token")
    res = {r["source_id"]: r for r in sources.verify_all()}
    assert res["x/site"]["status"] == "changed" and not res["x/site"]["blocking"]
    assert res["x/reader-copy"]["status"] == "not_supplied" and not res["x/reader-copy"]["blocking"]
    assert cli.main(["sources", "verify"]) == 0
    (tmp_path / "input/x.pdf").write_bytes(b"tampered")
    assert sources.verify_all("x")[0]["status"] == "MISMATCH"
    assert cli.main(["sources", "verify", "x"]) == 1
    assert "FAILED" in capsys.readouterr().out
