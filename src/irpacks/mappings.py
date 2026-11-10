"""Book-to-framework mapping files (e.g. examples/countermap/mappings/dynamic-ir-attack.yaml).

A mapping links one canonical pack element to one framework ID and states where the association comes from:

* ``source_explicit`` — the source text itself names the ID at that element (an ``evidence`` quote must occur in
  the element's text);
* ``analyst_mapping`` — an analyst's judgment, with ``rationale``, ``analyst`` and ``review_status``. The source
  does not make the association, and it is **not** a Countermap or MITRE edge;
* ``derived`` — computed mechanically from other mappings in the same file (``derived_from`` + ``rule``).

Framework IDs are resolved through an injected ``resolver`` so validation runs offline in tests and against a real
Countermap snapshot in the example. A resolver returns ``{"id", "name", "warnings"}`` or raises ``LookupError``.
A revoked or deprecated ID that the framework redirects to another one is an error, never silently rewritten.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

import jsonschema
import yaml

from .util import PROJECT_ROOT

PROVENANCE = ("source_explicit", "analyst_mapping", "derived")
RULES = {"parent_of_subtechnique"}
SCHEMA = PROJECT_ROOT / "schemas" / "framework-mappings.schema.json"

Resolver = Callable[[str], dict]


def _ws(s: str) -> str:
    return " ".join(s.split())


def load(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def countermap_resolver(db: str | Path, binary: str = "countermap") -> Resolver:
    """Resolve IDs with the read-only Countermap CLI (`countermap --db DB lookup ID`)."""
    cache: dict[str, dict] = {}

    def resolve(fid: str) -> dict:
        if fid not in cache:
            p = subprocess.run([binary, "--db", str(db), "lookup", fid], capture_output=True, text=True, timeout=60)
            if p.returncode != 0:
                raise LookupError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout) else fid)
            d = json.loads(p.stdout)
            cache[fid] = {"id": d["ext_id"], "name": d["name"], "warnings": d.get("warnings", []),
                          "dataset": (d.get("source") or {}).get("dataset"),
                          "version": (d.get("source") or {}).get("version")}
        return cache[fid]
    return resolve


def countermap_versions(db: str | Path, binary: str = "countermap") -> dict[str, str]:
    p = subprocess.run([binary, "--db", str(db), "info"], capture_output=True, text=True, timeout=60, check=True)
    return {s["key"]: s["version"] for s in json.loads(p.stdout)["sources"] if s.get("status") == "loaded"}


def validate(data: dict, elements: dict[str, dict], resolver: Resolver | None,
             snapshot_versions: dict[str, str] | None = None) -> dict[str, Any]:
    """Return {"passed", "errors", "warnings", "resolved"}; never modifies the mapping file."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        jsonschema.validate(data, json.loads(SCHEMA.read_text(encoding="utf-8")))
    except jsonschema.ValidationError as e:
        return {"passed": False, "errors": [f"schema: {e.message} at {'/'.join(map(str, e.absolute_path))}"],
                "warnings": [], "resolved": {}}
    pinned = data["framework_snapshot"]["datasets"]
    if snapshot_versions is not None:
        for key, ver in pinned.items():
            if snapshot_versions.get(key) != ver:
                errors.append(f"framework snapshot mismatch: file pins {key} {ver}, "
                              f"database has {snapshot_versions.get(key)}")
    by_id = {m["id"]: m for m in data["mappings"]}
    if len(by_id) != len(data["mappings"]):
        errors.append("duplicate mapping ids")
    seen_pairs: set[tuple[str, str, str]] = set()
    resolved: dict[str, dict] = {}
    for m in data["mappings"]:
        mid, fid, prov = m["id"], m["framework_id"], m["provenance"]
        key = (m["element_id"], fid, prov)
        if key in seen_pairs:
            errors.append(f"{mid}: duplicate of another mapping ({m['element_id']} -> {fid}, {prov})")
        seen_pairs.add(key)
        el = elements.get(m["element_id"])
        if el is None:
            errors.append(f"{mid}: element {m['element_id']} is not in the pack document")
        if resolver is not None and fid not in resolved:
            try:
                r = resolver(fid)
            except LookupError as e:
                errors.append(f"{mid}: {fid} does not resolve in the framework snapshot ({e})")
                r = None
            if r is not None:
                if r["id"] != fid:
                    errors.append(f"{mid}: {fid} resolves to {r['id']} ({'; '.join(r.get('warnings') or [])}); "
                                  "update the mapping explicitly instead of relying on the redirect")
                elif r.get("warnings"):
                    warnings.append(f"{mid}: {fid}: {'; '.join(r['warnings'])}")
                if m.get("framework_name") and _ws(m["framework_name"]).lower() != _ws(r["name"]).lower():
                    errors.append(f"{mid}: {fid} is named {r['name']!r} in the snapshot, not {m['framework_name']!r}")
                resolved[fid] = r
        if prov == "source_explicit":
            ev = m.get("evidence")
            if not ev:
                errors.append(f"{mid}: source_explicit mapping needs an evidence quote")
            elif el is not None and _ws(ev) not in _ws(el.get("text") or ""):
                errors.append(f"{mid}: evidence quote not found in {m['element_id']}")
            if el is not None and not _cites(el.get("text") or "", fid):
                errors.append(f"{mid}: {m['element_id']} does not name {fid}; use analyst_mapping")
        elif prov == "analyst_mapping":
            for f in ("rationale", "analyst", "review_status"):
                if not m.get(f):
                    errors.append(f"{mid}: analyst_mapping needs {f}")
        elif prov == "derived":
            src = [by_id.get(s) for s in m.get("derived_from") or []]
            if not src or None in src:
                errors.append(f"{mid}: derived mapping needs derived_from ids that exist in this file")
            elif m.get("rule") not in RULES:
                errors.append(f"{mid}: unknown derivation rule {m.get('rule')!r}")
            elif m["rule"] == "parent_of_subtechnique":
                for s in src:
                    if "." not in s["framework_id"] or s["framework_id"].split(".")[0] != fid:
                        errors.append(f"{mid}: {fid} is not the parent of {s['framework_id']} ({s['id']})")
                    if s["element_id"] != m["element_id"]:
                        errors.append(f"{mid}: derived mapping must keep the element of {s['id']}")
                    if s["provenance"] == "derived":
                        errors.append(f"{mid}: derive only from non-derived mappings")
    return {"passed": not errors, "errors": errors, "warnings": warnings, "resolved": resolved}


def _cites(text: str, fid: str) -> bool:
    """True if `text` names the ATT&CK ID as T1234[.001] or in an attack.mitre.org URL (T1234/001)."""
    base, _, sub = fid.partition(".")
    pats = [re.escape(fid) + r"(?![.\d])"]
    if sub:
        pats.append(re.escape(base) + "/" + re.escape(sub) + r"(?!\d)")
    else:
        pats.append(re.escape(base) + r"(?![.\d/])")
    return any(re.search(r"(?<![\w])" + p, text) for p in pats)


def by_framework_id(data: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in data["mappings"]:
        out.setdefault(m["framework_id"], []).append(m)
    return out
