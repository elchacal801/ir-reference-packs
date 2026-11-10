"""Bounded context expansion around a retrieved chunk.

Priority order (each item carries its own citation):
1. the chunk itself;
2. its required context (first fragment / lead-in / warnings / referenced notes);
3. the previous and next chunks in the same section;
4. remaining chunks of the parent section, nearest first.

Items that do not fit the token budget are listed under ``omitted`` with the
reason, never silently dropped.  Asset references are returned as paths; the
caller decides whether a visual is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .search import all_chunks, get_chunk


def expand(db_path: str | Path, chunk_id: str, budget: int = 3000, include_section: bool = True) -> dict[str, Any]:
    seed = get_chunk(db_path, chunk_id)
    if seed is None:
        return {"error": f"unknown chunk_id {chunk_id}"}
    pack = all_chunks(db_path, seed["pack_id"])
    by_id = {c["chunk_id"]: c for c in pack}
    order = [c["chunk_id"] for c in pack]
    pos = {cid: i for i, cid in enumerate(order)}
    items: list[tuple[dict, str]] = [(seed, "seed")]
    for r in seed["required_context"]:
        if r["id"] in by_id:
            items.append((by_id[r["id"]], "required: " + r["reason"]))
    same_sec = [c for c in pack if c["parent_section_id"] == seed["parent_section_id"]]
    for nid, why in ((seed["previous_chunk_id"], "previous chunk"), (seed["next_chunk_id"], "next chunk")):
        if nid and nid in by_id and by_id[nid]["parent_section_id"] == seed["parent_section_id"]:
            items.append((by_id[nid], why))
    if include_section:
        rest = sorted(same_sec, key=lambda c: abs(pos[c["chunk_id"]] - pos[seed["chunk_id"]]))
        for c in rest:
            items.append((c, "same section"))
    used, out, omitted, seen = 0, [], [], set()
    for c, why in items:
        if c["chunk_id"] in seen:
            continue
        seen.add(c["chunk_id"])
        cost = c["token_count"]
        if out and used + cost > budget:
            omitted.append({"chunk_id": c["chunk_id"], "reason": f"budget ({why})", "token_count": cost,
                            "required": why.startswith("required")})
            continue
        used += cost
        out.append({"chunk_id": c["chunk_id"], "role": why, "heading_path": c["heading_path"],
                    "citation": c["citation"], "token_count": cost, "asset_refs": c["asset_refs"],
                    "note_refs": c["note_refs"], "text": c["text"]})
    out.sort(key=lambda x: pos[x["chunk_id"]])
    missing_required = [o for o in omitted if o["required"]]
    needs_visual = [a for x in out for a in x["asset_refs"] if a.get("kind") in ("figure", "table_rendering", "formula_rendering")]
    return {
        "seed": chunk_id,
        "budget": budget,
        "tokens_used": used,
        "items": out,
        "omitted": omitted,
        "required_context_complete": not missing_required,
        "visual_assets": needs_visual,
        "notice": ("Some required context did not fit the budget; expand again with a larger budget before relying on this passage."
                   if missing_required else None),
    }
