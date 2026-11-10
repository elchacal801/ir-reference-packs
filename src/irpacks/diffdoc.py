"""Semantic diff between two builds of a pack (``irpacks diff``), and section-level history fingerprints.

The diff compares canonical documents, not raw files. A rebuilt page whose article text did not change produces no
content changes, however different its bytes are. It reports:
* units (chapters or web articles) added or removed, and changes to their version metadata;
* sections added, removed or retitled;
* blocks added, removed or changed within matched sections.

Blocks are aligned in document order: identical content first, then the closest remaining block of the same type
(a similarity ratio of at least 0.5 counts as a change, anything less as a removal plus an addition).

Each change gets a severity, so a reviewer sees what matters first:
* ``high``: commands and code, tables, warning/caution/important admonitions, and steps of ordered (procedure)
  lists;
* ``medium``: other prose and list items;
* ``low``: whitespace or punctuation only.

``fingerprint()`` produces a small history record (hashes and titles, no text) that can be tracked in git. Later
builds can then report what changed since any recorded snapshot without keeping old builds around.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .util import read_json, text_key, utc_now

SECTIONS = ("part", "chapter", "division", "section")
META_KEYS = ("title", "url", "git_commit_id", "updated_at", "ms_date", "document_id")


def load_document(path: str | Path) -> dict:
    p = Path(path)
    return read_json(p / "document.json" if p.is_dir() else p)


class _Doc:
    def __init__(self, doc: dict):
        self.doc = doc
        self.els = {e["id"]: e for e in doc["elements"]}
        self.root = self.els[doc["root_id"]]

    def title(self, e: dict) -> str:
        head = next((self.els[c] for c in e["children"] if self.els[c]["type"] == "heading"), None)
        return (head or {}).get("text") or e["attrs"].get("title") or e["id"]

    def path(self, eid: str) -> list[str]:
        out, cur = [], self.els.get(eid)
        while cur is not None and cur["id"] != self.root["id"]:
            if cur["type"] in SECTIONS:
                out.append(self.title(cur))
            cur = self.els.get(cur["parent_id"]) if cur["parent_id"] else None
        return list(reversed(out))

    def blocks(self, sec_id: str) -> list[dict]:
        """Content leaves of a section (not of its subsections), in document order."""
        out = []

        def walk(eid: str):
            for c in self.els[eid]["children"]:
                e = self.els[c]
                if e["type"] in SECTIONS or (e["type"] == "heading" and e["parent_id"] == sec_id
                                              and not e["attrs"].get("discrete")):
                    continue
                if e.get("text") or e["type"] in ("figure", "table", "code"):
                    out.append(e)
                walk(c)
        walk(sec_id)
        return out

    def sections(self, unit_id: str) -> list[dict]:
        out = []

        def walk(eid: str):
            for c in self.els[eid]["children"]:
                if self.els[c]["type"] in SECTIONS:
                    out.append(self.els[c])
                    walk(c)
        walk(unit_id)
        return out

    def in_warning(self, e: dict) -> bool:
        cur = e
        while cur is not None:
            if cur["type"] == "admonition" and cur["attrs"].get("kind") in ("warning", "caution", "important"):
                return True
            cur = self.els.get(cur["parent_id"]) if cur["parent_id"] else None
        return False

    def procedure_step(self, e: dict) -> bool:
        p = self.els.get(e["parent_id"]) if e["parent_id"] else None
        return e["type"] == "list_item" and p is not None and bool(p["attrs"].get("ordered"))


def _content(e: dict) -> str:
    if e["type"] == "table":
        return "\n".join(" | ".join(c["text"] for c in r["cells"]) for r in e["attrs"].get("rows", []))
    if e["type"] == "figure":
        return e["attrs"].get("alt") or e["attrs"].get("caption") or ""
    return e.get("text") or ""


def _severity(d: _Doc, e: dict, old: str | None, new: str | None) -> str:
    if old is not None and new is not None and (old.split() == new.split() or text_key(old) == text_key(new)):
        return "low"
    if e["type"] in ("code", "table") or d.in_warning(e) or d.procedure_step(e):
        return "high"
    return "medium"


def _udiff(old: str, new: str, limit: int = 40, *, lines_only: bool = False) -> list[str]:
    lines = list(difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=1))[2:]
    if not lines_only and len(old.split("\n")) == 1 and len(new.split("\n")) == 1:  # prose: word-level diff reads better
        sm = difflib.SequenceMatcher(None, old.split(), new.split())
        out = []
        for op, a1, a2, b1, b2 in sm.get_opcodes():
            if op != "equal":
                out.append(f"{op}: {' '.join(old.split()[a1:a2])!r} -> {' '.join(new.split()[b1:b2])!r}")
        return out[:limit]
    return lines[:limit]


def _align(od: _Doc, nd: _Doc, ob: list[dict], nb: list[dict], path: list[str], changes: list[dict]) -> None:
    oc = [_content(e) for e in ob]
    nc = [_content(e) for e in nb]
    sm = difflib.SequenceMatcher(None, [hashlib.sha256(x.encode()).hexdigest() for x in oc],
                                 [hashlib.sha256(x.encode()).hexdigest() for x in nc], autojunk=False)
    rem, add = [], []
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            continue
        rem += list(range(a1, a2))
        add += list(range(b1, b2))
    pairs, used = [], set()
    for i in rem:
        best, score = None, 0.5
        for j in add:
            if j in used or ob[i]["type"] != nb[j]["type"]:
                continue
            r = difflib.SequenceMatcher(None, oc[i], nc[j], autojunk=False).ratio()
            if r >= score:
                best, score = j, r
        if best is not None:
            used.add(best)
            pairs.append((i, best))
    paired_old = {i for i, _ in pairs}
    for i, j in pairs:
        changes.append({"change": "changed", "element_type": nb[j]["type"], "section_path": path,
                        "old_element_id": ob[i]["id"], "new_element_id": nb[j]["id"],
                        "severity": _severity(nd, nb[j], oc[i], nc[j]), "old_text": oc[i], "new_text": nc[j],
                        "diff": _udiff(oc[i], nc[j], lines_only=nb[j]["type"] in ("code", "table"))})
    for i in rem:
        if i not in paired_old:
            changes.append({"change": "removed", "element_type": ob[i]["type"], "section_path": path,
                            "old_element_id": ob[i]["id"], "severity": _severity(od, ob[i], None, None),
                            "old_text": oc[i]})
    for j in add:
        if j not in used:
            changes.append({"change": "added", "element_type": nb[j]["type"], "section_path": path,
                            "new_element_id": nb[j]["id"], "severity": _severity(nd, nb[j], None, None),
                            "new_text": nc[j]})


def diff_documents(old: dict, new: dict) -> dict:
    od, nd = _Doc(old), _Doc(new)
    ou = {c: od.els[c] for c in od.root["children"]}
    nu = {c: nd.els[c] for c in nd.root["children"]}
    report: dict[str, Any] = {"generated_at": utc_now(), "old": old["document_id"], "new": new["document_id"],
                              "units_added": [u for u in nu if u not in ou], "units_removed": [u for u in ou if u not in nu],
                              "units": []}
    for uid in [u for u in nu if u in ou]:
        o, n = ou[uid], nu[uid]
        meta = {k: {"old": o["attrs"].get(k), "new": n["attrs"].get(k)} for k in META_KEYS
                if o["attrs"].get(k) != n["attrs"].get(k)}
        changes: list[dict] = []
        osecs = {s["id"]: s for s in od.sections(uid)}
        nsecs = {s["id"]: s for s in nd.sections(uid)}
        # retitled sections keep their id when anchors are stable; match unmatched ones by title as a fallback
        o_title = {od.title(s): sid for sid, s in osecs.items() if sid not in nsecs}
        renamed = {sid: o_title[nd.title(s)] for sid, s in nsecs.items() if sid not in osecs and nd.title(s) in o_title}
        sec_events = []
        for sid, s in nsecs.items():
            old_id = sid if sid in osecs else renamed.get(sid)
            if old_id is None:
                sec_events.append({"change": "section_added", "section_path": nd.path(sid), "new_element_id": sid,
                                   "severity": "medium", "blocks": len(nd.blocks(sid))})
                continue
            if od.title(osecs[old_id]) != nd.title(s):
                sec_events.append({"change": "section_retitled", "section_path": nd.path(sid), "old_title": od.title(osecs[old_id]),
                                   "new_title": nd.title(s), "severity": "low"})
            _align(od, nd, od.blocks(old_id), nd.blocks(sid), nd.path(sid), changes)
        matched_old = {sid if sid in osecs else None for sid in nsecs} | set(renamed.values())
        for sid in osecs:
            if sid not in matched_old:
                sec_events.append({"change": "section_removed", "section_path": od.path(sid), "old_element_id": sid,
                                   "severity": "high" if any(b["type"] in ("code", "table") or od.in_warning(b)
                                                             for b in od.blocks(sid)) else "medium",
                                   "blocks": len(od.blocks(sid))})
        _align(od, nd, od.blocks(uid), nd.blocks(uid), nd.path(uid), changes)  # unit-level intro blocks
        allc = sec_events + changes
        if meta or allc:
            report["units"].append({
                "unit_id": uid, "title": nd.title(n), "url": n["attrs"].get("url"), "metadata_changes": meta,
                "content_changed": bool(allc),
                "counts": {s: sum(1 for c in allc if c["severity"] == s) for s in ("high", "medium", "low")},
                "changes": sorted(allc, key=lambda c: {"high": 0, "medium": 1, "low": 2}[c["severity"]]),
            })
    report["summary"] = {
        "units_compared": len([u for u in nu if u in ou]), "units_added": len(report["units_added"]),
        "units_removed": len(report["units_removed"]),
        "units_with_content_changes": sum(1 for u in report["units"] if u["content_changed"]),
        "units_with_metadata_only_changes": sum(1 for u in report["units"] if not u["content_changed"]),
        "changes_by_severity": {s: sum(u["counts"][s] for u in report["units"]) for s in ("high", "medium", "low")},
    }
    return report


def render_markdown(r: dict) -> str:
    s = r["summary"]
    L = [f"# Change report: `{r['old']}` → `{r['new']}`", "", f"Generated {r['generated_at']}. Content is compared on the "
         "canonical documents; page-template or markup-only differences do not appear here.", "",
         f"- Units compared: {s['units_compared']}; added: {s['units_added']}; removed: {s['units_removed']}",
         f"- Units with content changes: {s['units_with_content_changes']}; metadata-only: "
         f"{s['units_with_metadata_only_changes']}",
         f"- Changes by severity: high {s['changes_by_severity']['high']}, medium {s['changes_by_severity']['medium']}, "
         f"low {s['changes_by_severity']['low']} (high = commands/code, tables, warnings, procedure steps)", ""]
    for key, label in (("units_added", "Added"), ("units_removed", "Removed")):
        if r[key]:
            L += [f"**{label} units:** " + ", ".join(f"`{u}`" for u in r[key]), ""]
    for u in r["units"]:
        L += [f"## {u['title']} (`{u['unit_id']}`)", ""]
        if u.get("url"):
            L += [f"<{u['url']}>", ""]
        for k, v in u["metadata_changes"].items():
            L.append(f"- {k}: `{v['old']}` → `{v['new']}`")
        if not u["content_changed"]:
            L += ["- No content changes (metadata only).", ""]
            continue
        L.append("")
        for c in u["changes"]:
            where = " > ".join(c.get("section_path") or []) or "(article intro)"
            if c["change"].startswith("section_"):
                extra = (f": “{c['old_title']}” → “{c['new_title']}”" if c["change"] == "section_retitled"
                         else f" ({c.get('blocks', 0)} block{'' if c.get('blocks') == 1 else 's'})")
                L.append(f"- **{c['severity'].upper()}** {c['change'].replace('_', ' ')}: {where}{extra}")
                continue
            L.append(f"- **{c['severity'].upper()}** {c['change']} {c['element_type']} in {where}")
            if c["change"] == "changed":
                if c["element_type"] in ("code", "table") or "\n" in c["old_text"]:
                    L += ["", "  ```diff", *[f"  {x}" for x in c["diff"]], "  ```", ""]
                else:
                    L += [f"  - {x}" for x in c["diff"][:8]]
            else:
                txt = c.get("new_text") or c.get("old_text") or ""
                L.append("  > " + (txt[:300] + ("…" if len(txt) > 300 else "")).replace("\n", "\n  > "))
        L.append("")
    return "\n".join(L) + "\n"


def fingerprint(build_dir: str | Path) -> dict:
    """Small, text-free history record of a build: per unit its version metadata and a hash per section."""
    b = Path(build_dir)
    doc = load_document(b)
    d = _Doc(doc)
    man = read_json(b / "manifest.json") if (b / "manifest.json").exists() else {}
    units = []
    for uid in d.root["children"]:
        u = d.els[uid]
        # units not cleared for redistribution keep no text at all: hashed section ids, no titles
        open_ = (u.get("rights") or {}).get("state") == "redistributable"
        secs = [{("id" if open_ else "id_hash"): s["id"] if open_ else hashlib.sha256(s["id"].encode()).hexdigest()[:16],
                 "title": d.title(s) if open_ else None,
                 "hash": hashlib.sha256("\n".join(e["content_hash"] for e in d.blocks(s["id"])).encode()).hexdigest()}
                for s in d.sections(uid)]
        units.append({"unit_id": uid, "title": d.title(u) if open_ else None, "redacted": not open_,
                      **{k: u["attrs"].get(k) for k in META_KEYS[1:]},
                      "intro_hash": hashlib.sha256("\n".join(e["content_hash"] for e in d.blocks(uid)).encode()).hexdigest(),
                      "sections": secs})
    return {"schema_version": 1, "pack_id": doc["pack_id"], "document_id": doc["document_id"],
            "edition_id": doc["edition_id"], "recorded_at": utc_now(), "built_at": (man.get("run") or {}).get("built_at"),
            "units": units}


def diff_fingerprints(old: dict, new: dict) -> dict:
    """Section-level changes between two history records (no text: what changed, not how)."""
    ou = {u["unit_id"]: u for u in old["units"]}
    nu = {u["unit_id"]: u for u in new["units"]}
    units = []
    for uid, n in nu.items():
        o = ou.get(uid)
        if o is None:
            continue
        key = lambda s: s.get("id_hash") or hashlib.sha256(s["id"].encode()).hexdigest()[:16]  # noqa: E731
        os_ = {key(s): s for s in o["sections"]}
        ns_ = {key(s): s for s in n["sections"]}
        label = lambda s: s.get("title") or s.get("id") or f"section {s['id_hash']}"  # noqa: E731
        ch = ([{"section": label(s), "change": "added"} for k, s in ns_.items() if k not in os_] +
              [{"section": label(s), "change": "removed"} for k, s in os_.items() if k not in ns_] +
              [{"section": label(s), "change": "changed"} for k, s in ns_.items() if k in os_ and os_[k]["hash"] != s["hash"]])
        if o.get("intro_hash") != n.get("intro_hash"):
            ch.append({"section": "(intro)", "change": "changed"})
        meta = {k: {"old": o.get(k), "new": n.get(k)} for k in META_KEYS[1:] if o.get(k) != n.get(k)}
        if ch or meta:
            units.append({"unit_id": uid, "title": n.get("title"), "metadata_changes": meta, "section_changes": ch})
    return {"old": old["document_id"], "new": new["document_id"], "old_recorded_at": old.get("recorded_at"),
            "units_compared": sum(1 for u in nu if u in ou),
            "units_added": [u for u in nu if u not in ou], "units_removed": [u for u in ou if u not in nu],
            "units": units}


def render_fingerprint_markdown(r: dict) -> str:
    """Markdown for a history-record comparison: which units and sections changed (no wording: history records
    carry no text)."""
    L = [f"# Change report: `{r['old']}` (history record) → `{r['new']}`", "",
         f"Compared with the history record made {r.get('old_recorded_at') or 'earlier'}. History records hold "
         "content hashes, not text, so this lists *what* changed; build both snapshots and use `irpacks diff` on the "
         "two builds for the wording.", "",
         f"- Units compared: {r.get('units_compared', '?')}; added: {len(r['units_added'])}; removed: {len(r['units_removed'])}",
         f"- Units with changes: {len(r['units'])}", ""]
    for u in r["units_added"]:
        L.append(f"- **added unit** `{u}`")
    for u in r["units_removed"]:
        L.append(f"- **removed unit** `{u}`")
    for u in r["units"]:
        L += ["", f"## {u.get('title') or u['unit_id']} (`{u['unit_id']}`)", ""]
        for k, v in u["metadata_changes"].items():
            L.append(f"- {k}: `{v['old']}` → `{v['new']}`")
        for c in u["section_changes"]:
            L.append(f"- section {c['change']}: {c['section']}")
    return "\n".join(L) + "\n"


def load_any(path: str | Path) -> tuple[str, dict]:
    """('document', doc) for a build dir / document.json; ('fingerprint', fp) for a history record."""
    p = Path(path)
    if p.is_dir() or p.name == "document.json":
        return "document", load_document(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    return ("fingerprint", data) if "units" in data and "elements" not in data else ("document", data)
