"""Per-element content-rights classification from a pack's ``rights.yaml``.

States: ``redistributable`` (an identified rights basis permits public
redistribution), ``metadata_only`` (only locators/metadata may be published),
``user_supplied_local`` (local use of a user-supplied source only) and
``pending_review`` (no resolved basis; keep out of public exports).

A rights record is a classification aid, not legal advice.  Hashes and
classification prove nothing about rights; the basis text and reviewer fields
must be maintained by a human.
"""

from __future__ import annotations

from typing import Any

STATES = ("redistributable", "metadata_only", "user_supplied_local", "pending_review")


def _matches(rule_match: dict, el: dict, ancestors: list[dict]) -> bool:
    for k, v in rule_match.items():
        if k == "element_id":
            if el["id"] != v and el["id"] not in (v if isinstance(v, list) else [v]):
                return False
        elif k == "within":
            ids = v if isinstance(v, list) else [v]
            if not (el["id"] in ids or any(a["id"] in ids for a in ancestors)):
                return False
        elif k == "type":
            if el["type"] not in (v if isinstance(v, list) else [v]):
                return False
        elif k == "has_attribution":
            if bool(el["attrs"].get("attribution")) != bool(v):
                return False
        elif k == "id_prefix":
            if not el["id"].startswith(v):
                return False
        else:
            raise ValueError(f"unknown rights match key: {k}")
    return True


def apply_rights(elements: list[dict], cfg: dict) -> dict[str, dict]:
    """Return {element_id: {"state", "basis", "rule", "reason"}} for every element."""
    by_id = {e["id"]: e for e in elements}
    overrides = {o["element_id"]: o for o in (cfg.get("overrides") or [])}
    out: dict[str, dict] = {}
    inherited: dict[str, dict] = {}
    for el in elements:
        anc = []
        pid = el["parent_id"]
        while pid:
            anc.append(by_id[pid])
            pid = by_id[pid]["parent_id"]
        rec = None
        if el["id"] in overrides:
            o = overrides[el["id"]]
            rec = {"state": o["state"], "basis": o.get("basis"), "rule": "override", "reason": o.get("reason"),
                   "reviewer": o.get("reviewer"), "review_date": o.get("date")}
        else:
            for r in cfg.get("rules", []):
                if _matches(r["match"], el, anc):
                    rec = {"state": r["state"], "basis": r.get("basis"), "rule": r["id"], "reason": r.get("reason")}
                    if r.get("inherit", False):
                        inherited[el["id"]] = rec
                    break
        if rec is None:
            parent_rule = next((inherited[a["id"]] for a in anc if a["id"] in inherited), None)
            if parent_rule is not None:
                rec = dict(parent_rule)
                inherited[el["id"]] = parent_rule
            else:
                rec = {"state": cfg["default_state"], "basis": cfg.get("default_basis"), "rule": "default",
                       "reason": None}
        if rec["state"] not in STATES:
            raise ValueError(f"invalid rights state {rec['state']!r} for {el['id']}")
        out[el["id"]] = rec
    return out


def most_restrictive(states: list[str]) -> str:
    rank = {"redistributable": 0, "user_supplied_local": 1, "metadata_only": 2, "pending_review": 3}
    return max(states, key=lambda s: rank[s]) if states else "pending_review"


def summarize(rights: dict[str, dict], elements: list[dict]) -> dict[str, Any]:
    import collections
    c = collections.Counter(r["state"] for r in rights.values())
    by_rule = collections.Counter(r["rule"] for r in rights.values())
    return {"by_state": dict(c), "by_rule": dict(by_rule)}
