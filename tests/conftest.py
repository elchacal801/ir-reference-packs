"""Shared offline fixtures.

Tests never need the book PDF, the network or external tools. ``mini_pack`` builds a small pack from
tests/fixtures/nist-mini (a redistributable slice of the NIST SP 800-61r3 canonical document) through the real
export path (Markdown, chunks, FTS5 index) and validates it. Tests can add synthetic elements first.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from irpacks import packbuild
from irpacks.util import PROJECT_ROOT, write_json

FIX = Path(__file__).resolve().parent / "fixtures"
NIST = PROJECT_ROOT / "packs" / "nist-sp800-61r3"


def load_mini() -> tuple[dict, dict]:
    doc = json.loads((FIX / "nist-mini" / "document.json").read_text(encoding="utf-8"))
    pm = json.loads((FIX / "nist-mini" / "page-map.json").read_text(encoding="utf-8"))
    return doc, pm


def add_element(doc: dict, parent_id: str, eid: str, etype: str, text: str, *, page: int, attrs: dict | None = None,
                after: str | None = None, mapping_status: str = "native_structure", rights_state: str = "redistributable",
                children: list[str] | None = None) -> dict:
    """Insert a synthetic element shaped like the route's output, keeping order contiguous."""
    els = {e["id"]: e for e in doc["elements"]}
    parent = els[parent_id]
    tmpl = next(e for e in doc["elements"] if e["type"] == "paragraph" and e["source_spans"])
    span = dict(tmpl["source_spans"][0], page=page, bbox=[72.0, 300.0, 540.0, 320.0], mapping_status=mapping_status,
                n_key_chars=len(text),
                page_label=next((p["label"] for p in doc["page_labels"] if p["page"] == page), None))
    el = {"id": eid, "type": etype, "order": 0, "parent_id": parent_id, "children": list(children or []), "anchor": None,
          "text": text, "md": text, "attrs": attrs if attrs is not None else {"links": []}, "source_spans": [span],
          "mapping_status": mapping_status, "extraction": copy.deepcopy(tmpl["extraction"]),
          "content_hash": hashlib.sha256(text.encode()).hexdigest(),
          "rights": {"state": rights_state, "rule": "test", "basis": None, "reason": "synthetic test element"}}
    idx = parent["children"].index(after) + 1 if after else len(parent["children"])
    prev = parent["children"][idx - 1] if idx > 0 else None
    parent["children"].insert(idx, eid)

    def last_desc(i):
        kids = [k for k in els[i]["children"] if k in els]
        return last_desc(kids[-1]) if kids else i
    anchor_id = last_desc(prev) if prev else parent_id
    pos = next(n for n, e in enumerate(doc["elements"]) if e["id"] == anchor_id) + 1
    doc["elements"].insert(pos, el)
    for n, e in enumerate(doc["elements"]):
        e["order"] = n
    doc["element_count"] = len(doc["elements"])
    return el


def build_pack(out: Path, doc: dict, pm: dict, *, variant: str = "local", omitted: dict | None = None,
               edition: str | None = None) -> dict:
    pack = yaml.safe_load((NIST / "pack.yaml").read_text(encoding="utf-8"))
    recipe = yaml.safe_load((NIST / "recipe.yaml").read_text(encoding="utf-8"))
    doc = copy.deepcopy(doc)
    if edition:
        doc["edition_id"] = edition
        doc["document_id"] = f"{doc['pack_id']}:{edition}"
    doc["variant"] = variant
    out.mkdir(parents=True, exist_ok=True)
    rights = {e["id"]: e["rights"] for e in doc["elements"]}
    meta = {"pack_id": doc["pack_id"], "pack_version": doc["pack_version"], "document_id": doc["document_id"],
            "edition_id": doc["edition_id"], "edition_short": doc["edition_id"],
            "citable_statuses": sorted(__import__("irpacks.docmodel", fromlist=["CITABLE"]).CITABLE)}
    write_json(out / "document.json", doc)
    write_json(out / "page-map.json", pm)
    (out / "pack.yaml").write_text((NIST / "pack.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    write_json(out / "qa" / "pilot-scope.json", {"scope": "test fixture slice"})
    em = packbuild.emit_outputs(doc, pm, {}, rights, meta, out, variant=variant, omitted=omitted,
                                chunk_cfg=recipe["chunking"], corpus_rules=packbuild.make_corpus_rules(recipe),
                                pack=pack, log=lambda *a, **k: None)
    return {"dir": out, "chunks": em["chunks"], "doc": doc}


@pytest.fixture
def mini():
    return load_mini()


@pytest.fixture
def no_network_or_exec(monkeypatch):
    """Fail the test if anything tries to open a socket or start a process."""
    import os
    import socket
    import subprocess

    def boom(*a, **k):
        raise AssertionError(f"forbidden side effect: {a[:1]}")
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(os, "system", boom)
    monkeypatch.setattr(os, "popen", boom)
    return boom
