"""Context-preserving retrieval chunks generated from a canonical document.

Policy (all sizes in tokens of the configured tokenizer, counting the
serialized text including the heading-context line):

* Chunks never cross section boundaries.  A section whose whole subtree fits
  ``max_tokens`` is emitted intact (one chunk).  Otherwise its direct content is
  packed into chunks and each subsection is processed recursively.
* Atomic units stay whole: a lead-in paragraph ending in ":" with the
  procedure/list/listing/table it introduces; a listing with its callout list;
  a procedure with an adjacent warning-type admonition; tables, figures,
  sidebars, quotes, admonitions.
* Oversize units are split explicitly (never truncated): lists at item
  boundaries, code at line boundaries, tables by rows with header rows repeated,
  containers at child boundaries, prose at sentence boundaries.  Fragments are
  linked and point to their first fragment as required context.  A unit that
  cannot be split below the limit is emitted with ``oversize: true``.
* Overlap is used only where prose is split across chunks: the next chunk is
  prefixed with sentence-aligned text from the previous chunk's last paragraph
  (configurable, default ~100 tokens within 80-120) and the overlap is recorded.
"""

from __future__ import annotations

import collections
import math
import re
from typing import Any

from . import tokens
from .adochtml import md_escape
from .mdrender import Renderer
from .util import sha256_text, short_hash

CHUNK_SCHEMA_VERSION = "1.0.0"
SECTION_TYPES = {"part", "chapter", "division", "section"}
WARNING_KINDS = {"warning", "caution", "important"}
PROCEDURE_TYPES = {"list", "listing", "code", "table", "callout_list", "dlist"}

# sentence boundary: terminal punctuation, optional closing quote/paren, whitespace, then a
# capital/digit/opening quote (fixed-width look-behinds for Python's re)
_SENT = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)]))\s+(?=[A-Z0-9“\"(\[])")


class _Unit:
    __slots__ = ("elements", "kind", "tokens", "text", "prose_tail")

    def __init__(self, elements, kind, text):
        self.elements = elements
        self.kind = kind
        self.text = text
        self.tokens = tokens.count(text)
        self.prose_tail = elements[-1]["type"] == "paragraph"


class Chunker:
    def __init__(self, doc: dict, cfg: dict, *, assets: dict, rights: dict, meta: dict,
                 corpus_rules: dict, omitted: dict | None = None, variant: str = "local"):
        self.doc = doc
        self.cfg = cfg
        self.els = {e["id"]: e for e in doc["elements"]}
        self.assets = assets
        self.rights = rights
        self.meta = meta
        self.corpus_rules = corpus_rules
        self.omitted = omitted or {}
        self.variant = variant
        self.max = cfg["max_tokens"]
        self.min = cfg["min_tokens"]
        self.overlap_target = cfg.get("overlap_tokens", 100)
        self.overlap_min = cfg.get("overlap_min_tokens", 80)
        self.overlap_max = cfg.get("overlap_max_tokens", 120)
        self.cfg_hash = short_hash(sorted(cfg.items()), tokens.TOKENIZER_ID, tokens.TOKENIZER_REVISION, variant, n=8)
        self.r = Renderer(doc, assets, omitted=self.omitted)
        self.r.page_anchor = lambda el: []  # no page anchors inside chunk text
        self.chunks: list[dict] = []
        self.el_chunk: dict[str, str] = {}  # element id -> first chunk containing it
        self.pending_heads: list[dict] = []  # headings of split sections awaiting their first chunk

    # ------------------------------------------------------------------ text
    def serialize(self, el: dict) -> str:
        if el["id"] in self.omitted:
            return "\n".join(self.r.omission(el))
        t = el["type"]
        if t == "figure":
            a = el["attrs"]
            cap = next((self.els[c] for c in el["children"] if self.els[c]["type"] == "caption"), None)
            asset = self.assets.get(el["id"])
            where = f"image asset {asset['path']}" if asset and asset.get("path") else "image not included in this distribution"
            capt = cap["text"] if cap else (a.get("caption") or "untitled figure")
            return f"[{capt} — {where}; the image content is not transcribed in this text]"
        if t == "math":
            return f"$$\n{el['text']}\n$$"
        if t in SECTION_TYPES:
            lines = self.r.render(el, "unit")
            return "\n".join(l for l in lines if not l.startswith("<a id=")).strip()
        lines = self.r.block(el, "unit")
        return "\n".join(lines).strip()

    def heading_path(self, sec_id: str) -> list[str]:
        path = []
        cur = self.els.get(sec_id)
        while cur is not None and cur["type"] != "document":
            head = next((self.els[c] for c in cur["children"] if self.els[c]["type"] == "heading"), None)
            if cur["type"] in SECTION_TYPES:
                title = head["text"] if head else cur["attrs"].get("title")
                if title:
                    path.append(title)
            cur = self.els.get(cur["parent_id"]) if cur["parent_id"] else None
        return list(reversed(path))

    def context_line(self, sec_id: str) -> str:
        return "Section: " + " > ".join(self.heading_path(sec_id))

    # ------------------------------------------------------------------ units
    def subtree_tokens(self, sec: dict) -> int:
        return tokens.count(self.context_line(sec["id"]) + "\n\n" + self.serialize(sec))

    def units_for(self, blocks: list[dict]) -> list[_Unit]:
        units: list[_Unit] = []
        i = 0
        while i < len(blocks):
            b = blocks[i]
            group = [b]
            # a discrete (unnumbered, non-section) heading stays with the block it introduces
            while b["type"] == "heading" and i + 1 < len(blocks):
                i += 1
                b = blocks[i]
                group.append(b)
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            # lead-in paragraph ending with ":" + the block it introduces
            if b["type"] == "paragraph" and b["text"].rstrip().endswith(":") and nxt is not None and \
                    nxt["type"] in PROCEDURE_TYPES | {"figure", "admonition", "sidebar"}:
                group.append(nxt)
                i += 1
            last = group[-1]
            # listing + its callout list
            if last["type"] in ("listing", "code") and i + 1 < len(blocks) and blocks[i + 1]["type"] == "callout_list":
                group.append(blocks[i + 1])
                i += 1
            # procedure + following warning-type admonition
            if group[-1]["type"] in PROCEDURE_TYPES and i + 1 < len(blocks) and blocks[i + 1]["type"] == "admonition" \
                    and blocks[i + 1]["attrs"].get("kind") in WARNING_KINDS:
                group.append(blocks[i + 1])
                i += 1
            kind = "procedure" if any(g["type"] in PROCEDURE_TYPES for g in group) else group[0]["type"]
            units.append(_Unit(group, kind, "\n\n".join(self.serialize(g) for g in group)))
            i += 1
        # warning admonition immediately before a procedure: attach forward
        merged: list[_Unit] = []
        for u in units:
            if merged and merged[-1].elements[-1]["type"] == "admonition" and len(merged[-1].elements) == 1 and \
                    merged[-1].elements[0]["attrs"].get("kind") in WARNING_KINDS and u.kind == "procedure" and \
                    merged[-1].tokens + u.tokens <= self.max:
                prev = merged.pop()
                u = _Unit(prev.elements + u.elements, "procedure", prev.text + "\n\n" + u.text)
            merged.append(u)
        return merged

    # ------------------------------------------------------------------ split
    def split_unit(self, u: _Unit, budget: int) -> list[tuple[list[dict], str, dict]]:
        """Split an oversize unit into (elements, text, fragment-info) pieces."""
        if len(u.elements) > 1:
            pieces = []
            for el in u.elements:
                sub = _Unit([el], el["type"], self.serialize(el))
                pieces += [p for p in (self.split_unit(sub, budget) if sub.tokens > budget else [([el], sub.text, {})])]
            return self._group_pieces(pieces, budget, "child_boundaries")
        el = u.elements[0]
        t = el["type"]
        if t == "list":
            items = [self.els[c] for c in el["children"]]
            a = el["attrs"]
            pieces = []
            for k, it in enumerate(items):
                fake = dict(el)
                fake["children"] = [it["id"]]
                fake["attrs"] = dict(a, start=(a.get("start") or 1) + k)
                pieces.append(([el], "\n".join(self.r.block(fake, "unit")), {"item": it["id"]}))
            return self._group_pieces(pieces, budget, "list_items")
        if t in ("listing", "code"):
            code_el = el if t == "code" else next(self.els[c] for c in el["children"] if self.els[c]["type"] == "code")
            cap = None if t == "code" else next((self.els[c] for c in el["children"] if self.els[c]["type"] == "caption"), None)
            head = f"**{cap['md'] or md_escape(cap['text'])}**\n\n" if cap else ""
            lines = code_el["text"].split("\n")
            fence = "```"
            while fence in code_el["text"]:
                fence += "`"
            lang = code_el["attrs"].get("lang") or ""
            pieces, cur = [], []
            for ln in lines:
                trial = head + fence + lang + "\n" + "\n".join(cur + [ln]) + "\n" + fence
                if cur and tokens.count(trial) > budget - 20:
                    pieces.append(cur)
                    cur = []
                cur.append(ln)
            if cur:
                pieces.append(cur)
            out = []
            for k, pl in enumerate(pieces):
                marker = f"[Fragment {k + 1} of {len(pieces)} of this listing]\n" if len(pieces) > 1 else ""
                out.append(([el], head + marker + fence + lang + "\n" + "\n".join(pl) + "\n" + fence,
                            {"policy": "code_lines", "index": k, "count": len(pieces)}))
            return out
        if t == "table":
            a = el["attrs"]
            heads = [r for r in a["rows"] if r["section"] == "thead"]
            body = [r for r in a["rows"] if r["section"] != "thead"]
            grouped = any(r.get("group") for r in body)
            # semantic row groups (e.g. one CSF Category with its Subcategories); function-level rows
            # join the group that follows them
            if grouped:
                sem, pend = [], []
                for r in body:
                    if r.get("csf_level") == "function" or not r.get("group"):
                        pend.append(r)
                        continue
                    if sem and sem[-1][0] == r["group"]:
                        sem[-1][1].append(r)
                    else:
                        sem.append((r["group"], pend + [r]))
                    pend = []
                if pend:
                    sem.append((None, pend))
                units = [g for _, g in sem]
            else:
                units = [[r] for r in body]
            groups, cur = [], []
            for unit_rows in units:
                fake = dict(el)
                fake["attrs"] = dict(a, rows=heads + cur + unit_rows)
                too_big = tokens.count("\n".join(self.r.table(fake, ""))) > budget - 20
                if cur and (too_big or grouped):
                    groups.append(cur)
                    cur = []
                if grouped and too_big and len(unit_rows) > 1:  # a single group that is itself too large
                    for r in unit_rows:
                        fake["attrs"] = dict(a, rows=heads + cur + [r])
                        if cur and tokens.count("\n".join(self.r.table(fake, ""))) > budget - 20:
                            groups.append(cur)
                            cur = []
                        cur.append(r)
                    continue
                cur.extend(unit_rows)
            if cur:
                groups.append(cur)
            out = []
            for k, g in enumerate(groups):
                fake = dict(el)
                fake["attrs"] = dict(a, rows=heads + g)
                marker = f"[Fragment {k + 1} of {len(groups)} of this table; header rows repeated]\n\n" if len(groups) > 1 else ""
                spans = [s for r in g for s in (r.get("source_spans") or [])]
                info = {"policy": "table_row_groups_header_repeated" if grouped else "table_rows_header_repeated",
                        "index": k, "count": len(groups), "spans": spans,
                        "groups": sorted({r.get("group") for r in g if r.get("group")})}
                out.append(([el], marker + "\n".join(self.r.table(fake, "")), info))
            return out
        if el["children"] and t not in ("figure",):
            kids = [self.els[c] for c in el["children"]]
            sub_units = self.units_for(kids)
            pieces = []
            for su in sub_units:
                pieces += self.split_unit(su, budget) if su.tokens > budget else [(su.elements, su.text, {})]
            return self._group_pieces(pieces, budget, "child_boundaries")
        if t == "paragraph":
            sents = _SENT.split(el["text"])
            pieces = [([el], s, {"sentence": True}) for s in sents]
            return self._group_pieces(pieces, budget, "sentences", prose=True)
        return [([el], u.text, {"policy": "unsplittable", "oversize": True})]

    def _group_pieces(self, pieces, budget, policy, prose=False):
        groups, cur, cur_els = [], [], []
        for els, text, info in pieces:
            trial = "\n\n".join([p[1] for p in cur] + [text])
            if cur and tokens.count(trial) > budget:
                groups.append(cur)
                cur = []
            cur.append((els, text, info))
        if cur:
            groups.append(cur)
        out = []
        for k, g in enumerate(groups):
            els = []
            for p in g:
                for e in p[0]:
                    if e not in els:
                        els.append(e)
            joiner = " " if prose else "\n\n"
            info = {"policy": policy, "index": k, "count": len(groups)}
            if any(p[2].get("oversize") for p in g):
                info["oversize"] = True
            out.append((els, joiner.join(p[1] for p in g), info))
        return out

    # ------------------------------------------------------------------ main
    def run(self) -> list[dict]:
        root = self.els[self.doc["root_id"]]
        for cid in root["children"]:
            self.process(self.els[cid])
        self._link()
        return self.chunks

    def process(self, sec: dict) -> None:
        kids = [self.els[c] for c in sec["children"]]
        title_head = next((k for k in kids if k["type"] == "heading" and not k["attrs"].get("discrete")), None)
        blocks = [k for k in kids if k["type"] not in SECTION_TYPES and k is not title_head and k["type"] != "footnote"]
        notes = [k for k in kids if k["type"] == "footnote"]
        subs = [k for k in kids if k["type"] in SECTION_TYPES]
        if sec["type"] != "part" and self.subtree_tokens(sec) <= self.max:
            self.emit(sec["id"], [sec], self.serialize(sec), whole_section=True)
            return
        if title_head is not None:  # represented by the next chunk's heading-context line
            self.pending_heads.append(title_head)
        if blocks:
            self.pack(sec, blocks)
        for s in subs:
            self.process(s)
        if notes:  # chapter-end notes, as printed, in their own chunk(s)
            self.pack(sec, notes)

    def pack(self, sec: dict, blocks: list[dict]) -> None:
        ctx = self.context_line(sec["id"])
        budget = self.max - tokens.count(ctx) - 4
        units = self.units_for(blocks)
        total = sum(u.tokens for u in units)
        n = max(1, math.ceil(total / budget))
        target = min(budget, max(self.min, total / n))
        cur: list[_Unit] = []
        cur_tok = 0
        pending_overlap = None

        def flush():
            nonlocal cur, cur_tok, pending_overlap
            if not cur:
                return
            text = "\n\n".join(u.text for u in cur)
            els = [e for u in cur for e in u.elements]
            ov = pending_overlap
            if ov is not None and cur[0].kind == "paragraph":
                text = ov["text"] + "\n\n" + text
            else:
                ov = None
            self.emit(sec["id"], els, text, overlap=ov)
            # overlap for a following prose chunk
            last = cur[-1]
            pending_overlap = self._overlap(last) if last.kind == "paragraph" else None
            if pending_overlap is not None:
                pending_overlap["from_index"] = len(self.chunks) - 1
            cur, cur_tok = [], 0

        for u in units:
            if u.tokens > budget:
                flush()
                pieces = self.split_unit(u, budget)
                first_idx = len(self.chunks)
                for k, (els, text, info) in enumerate(pieces):
                    frag = {"of_element_id": u.elements[0]["id"] if len(u.elements) == 1 else u.elements[0]["id"],
                            "policy": info.get("policy"), "index": k, "count": len(pieces)}
                    ov = None
                    if info.get("policy") == "sentences" and k > 0:
                        prev_text = pieces[k - 1][1]
                        ov = self._overlap_text(prev_text)
                        if ov:
                            ov["from_index"] = len(self.chunks) - 1
                            text = ov["text"] + " " + text
                    if info.get("groups"):
                        frag["row_groups"] = info["groups"]
                    self.emit(sec["id"], els, text, fragment=frag, overlap=ov, oversize=info.get("oversize", False),
                              first_fragment_index=first_idx if k > 0 else None, spans_override=info.get("spans"))
                pending_overlap = None
                continue
            if cur and (cur_tok + u.tokens > budget or (cur_tok >= target and cur_tok + u.tokens > target * 1.15)):
                flush()
            cur.append(u)
            cur_tok += u.tokens
        flush()

    def _overlap(self, u: _Unit) -> dict | None:
        return self._overlap_text(u.elements[-1]["text"])

    def _overlap_text(self, text: str) -> dict | None:
        sents = [s for s in _SENT.split(text) if s.strip()]
        picked: list[str] = []
        for s in reversed(sents):
            cand = [s] + picked
            n = tokens.count(" ".join(cand))
            if n > self.overlap_max and picked:
                break
            picked = cand
            if n >= self.overlap_target:
                break
        if not picked:
            return None
        t = " ".join(picked)
        n = tokens.count(t)
        if n > self.overlap_max * 2:  # a single giant sentence: skip rather than duplicate it
            return None
        return {"text": t, "tokens": n}

    # ------------------------------------------------------------------ records
    def emit(self, sec_id: str, els: list[dict], body: str, *, whole_section=False, fragment=None,
             overlap=None, oversize=False, first_fragment_index=None, spans_override=None) -> None:
        ctx = self.context_line(sec_id)
        text = ctx + "\n\n" + body.strip()
        leaves = list(self.pending_heads)
        self.pending_heads = []
        for e in els:
            leaves += self._leaves(e)
        src_ids = []
        for e in leaves:
            if e["id"] not in src_ids:
                src_ids.append(e["id"])
        first = els[0]["id"]
        frag_idx = fragment["index"] if fragment else 0
        cid = f"{self.meta['pack_id']}:{self.meta['edition_short']}:{self.cfg_hash}:{first}:{frag_idx}"
        ntok = tokens.count(text)
        rec = {
            "chunk_id": cid,
            "_section": sec_id,
            "_els": els,
            "_leaves": leaves,
            "_overlap": overlap,
            "_first_fragment_index": first_fragment_index,
            "_spans_override": spans_override,
            "text": text,
            "source_element_ids": src_ids,
            "fragment": fragment,
            "token_count": ntok,
            "oversize": bool(oversize or ntok > self.max),
            "whole_section": whole_section,
        }
        self.chunks.append(rec)
        for e in leaves + els:
            self.el_chunk.setdefault(e["id"], cid)

    def _leaves(self, e: dict) -> list[dict]:
        if not e["children"] or e["type"] in ("figure",):
            return [e] + ([self.els[c] for c in e["children"]] if e["type"] == "figure" else [])
        out = [e] if e["type"] in ("list_item",) and e.get("text") else []
        for c in e["children"]:
            out += self._leaves(self.els[c])
        return out

    def _link(self) -> None:
        meta = self.meta
        chunks = self.chunks
        for i, c in enumerate(chunks):
            c["previous_chunk_id"] = chunks[i - 1]["chunk_id"] if i > 0 else None
            c["next_chunk_id"] = chunks[i + 1]["chunk_id"] if i + 1 < len(chunks) else None
        sec_chunks: dict[str, list[dict]] = collections.defaultdict(list)
        for c in chunks:
            sec_chunks[c["_section"]].append(c)
        out = []
        for i, c in enumerate(chunks):
            leaves = c["_leaves"]
            req: list[dict] = []

            def add(cid, reason):
                if cid and cid != c["chunk_id"] and all(r["id"] != cid for r in req):
                    req.append({"id": cid, "reason": reason})

            if c["_first_fragment_index"] is not None:
                add(chunks[c["_first_fragment_index"]]["chunk_id"], "first fragment of the same split unit (caption, "
                                                                     "lead-in or header)")
            if c["fragment"] and c["fragment"]["index"] > 0:
                add(chunks[i - 1]["chunk_id"], "preceding fragment of the same split unit")
            # a chunk that opens with a procedure whose lead-in is in the previous chunk of the same section
            first_el = c["_els"][0]
            if first_el["type"] in PROCEDURE_TYPES and i > 0 and chunks[i - 1]["_section"] == c["_section"]:
                prev_last = chunks[i - 1]["_els"][-1]
                if prev_last["type"] == "paragraph" and prev_last["text"].rstrip().endswith(":"):
                    add(chunks[i - 1]["chunk_id"], "lead-in for the procedure that opens this chunk")
            # warnings in the same section outside this chunk
            if any(e["type"] in PROCEDURE_TYPES for e in c["_els"]):
                for oc in sec_chunks[c["_section"]]:
                    if oc is c:
                        continue
                    if any(e["type"] == "admonition" and e["attrs"].get("kind") in WARNING_KINDS for e in oc["_els"]):
                        add(oc["chunk_id"], "warning/important admonition in the same section")
            # notes and assets
            note_refs, asset_refs, warn_refs, table_refs, related = [], [], [], [], []
            for e in leaves:
                for v in (e.get("extraction") or {}).get("variables", []):
                    if v["kind"] == "footnote_ref" and v.get("pdf_label"):
                        nid = self._note_id(e, v["pdf_label"])
                        if nid and nid not in [n["element_id"] for n in note_refs]:
                            note_refs.append({"element_id": nid, "label": v["pdf_label"],
                                              "chunk_id": self.el_chunk.get(nid)})
                            add(self.el_chunk.get(nid), "footnote referenced by this passage")
                    if v["kind"] == "xref" and v.get("target"):
                        tgt = self._anchor_el(v["target"])
                        if tgt is not None:
                            related.append(tgt["id"])
                            if tgt["type"] == "figure":
                                asset_refs.append(self._asset_ref(tgt, "referenced"))
            for e in c["_els"] + leaves:
                if e["type"] == "figure":
                    asset_refs.append(self._asset_ref(e, "contained"))
                if e["type"] == "math" and self.assets.get(e["id"]):
                    asset_refs.append({"element_id": e["id"], "kind": "formula_rendering",
                                       "path": self.assets[e["id"]]["path"], "relation": "contained"})
                if e["type"] == "table":
                    table_refs.append(e["id"])
                    if self.assets.get(e["id"]):
                        for pth in self.assets[e["id"]].get("paths", []):
                            asset_refs.append({"element_id": e["id"], "kind": "table_rendering", "path": pth,
                                               "relation": "contained"})
                if e["type"] == "admonition":
                    warn_refs.append({"element_id": e["id"], "kind": e["attrs"].get("kind")})
            dedup_assets, seen = [], set()
            for a_ in asset_refs:
                k = (a_["element_id"], a_.get("path"), a_["relation"])
                if k not in seen:
                    seen.add(k)
                    dedup_assets.append(a_)
            out.append(self._record(c, req, note_refs, dedup_assets, warn_refs, table_refs, related))
        self.chunks = out

    def _note_id(self, e: dict, label: str) -> str | None:
        cur = e
        while cur is not None and cur["type"] not in ("chapter", "division"):
            cur = self.els.get(cur["parent_id"]) if cur["parent_id"] else None
        if cur is None:
            return None
        nid = f"{cur['id']}.fn{label.zfill(2)}"
        return nid if nid in self.els else None

    def _anchor_el(self, anchor: str) -> dict | None:
        if not hasattr(self, "_anchors"):
            self._anchors = {e["anchor"]: e for e in self.doc["elements"] if e.get("anchor")}
        return self._anchors.get(anchor)

    def _asset_ref(self, el: dict, relation: str) -> dict:
        a = self.assets.get(el["id"]) or {}
        return {"element_id": el["id"], "kind": "figure", "path": a.get("path"), "asset_id": a.get("asset_id"),
                "caption": el["attrs"].get("caption"), "number": el["attrs"].get("number"), "relation": relation,
                "included_in_distribution": bool(a.get("path"))}

    def _record(self, c, req, note_refs, asset_refs, warn_refs, table_refs, related) -> dict:
        meta = self.meta
        leaves = c["_leaves"]
        spans = []
        for e in leaves:
            if c.get("_spans_override") and e["type"] == "table":
                # a table fragment cites only the pages of the rows it contains
                src = c["_spans_override"]
            else:
                src = e["source_spans"]
            for s in src:
                span = {"locator_type": s["locator_type"], "page": s.get("page"), "page_label": s.get("page_label"),
                        "native_locator": s.get("native_locator"), "element_id": e["id"],
                        "mapping_status": s.get("mapping_status")}
                if s.get("url"):  # unpaginated sources: the section's URL#anchor is the locator
                    span["url"] = s["url"]
                spans.append(span)
        statuses = sorted({e["mapping_status"] for e in leaves if e["mapping_status"] not in ("aggregate", "not_applicable")})
        validated = bool(statuses) and all(s in meta["citable_statuses"] for s in statuses)
        corpus, include, reason = self.corpus_rules["classify"](c, self.els)
        if include and not validated:
            include, reason = False, "contains elements without a verified source locator: " + ", ".join(
                s for s in statuses if s not in meta["citable_statuses"])
        rights_states = [self.rights[e["id"]]["state"] for e in leaves if e["id"] in self.rights]
        from .rights import most_restrictive
        rstate = most_restrictive(rights_states) if rights_states else "pending_review"
        hp = self.heading_path(c["_section"])
        ctype = self._content_type(c, corpus)
        citation = meta["cite"](hp, spans, self.els[c["_section"]])
        rec = {
            "chunk_id": c["chunk_id"],
            "pack_id": meta["pack_id"],
            "pack_version": meta["pack_version"],
            "document_id": meta["document_id"],
            "edition_id": meta["edition_id"],
            "schema_version": CHUNK_SCHEMA_VERSION,
            "variant": self.variant,
            "chunk_config_hash": self.cfg_hash,
            "content_hash": sha256_text(c["text"]),
            "text": c["text"],
            "heading_path": hp,
            "content_type": ctype,
            "source_element_ids": c["source_element_ids"],
            "parent_section_id": c["_section"],
            "previous_chunk_id": c["previous_chunk_id"],
            "next_chunk_id": c["next_chunk_id"],
            "required_context_ids": [r["id"] for r in req],
            "required_context": req,
            "related_element_ids": sorted(set(related)),
            "source_spans": spans,
            "citation": citation,
            "asset_refs": asset_refs,
            "note_refs": note_refs,
            "warning_refs": warn_refs,
            "table_refs": table_refs,
            "extraction": {
                "methods": sorted({(e.get("extraction") or {}).get("method") or "structure" for e in leaves}),
                "mapping_statuses": statuses,
                "validated_locators": validated,
                "review_status": meta["review_status_of"](c["source_element_ids"]),
            },
            "tokenizer": {"id": tokens.TOKENIZER_ID, "revision": tokens.TOKENIZER_REVISION},
            "token_count": c["token_count"],
            "oversize": c["oversize"],
            "oversize_policy": ("unsplittable unit emitted whole; never truncated" if c["oversize"] else None),
            "fragment": c["fragment"],
            "overlap": ({"tokens": c["_overlap"]["tokens"], "from_chunk_id": self.chunks[c["_overlap"]["from_index"]]["chunk_id"]
                         if isinstance(self.chunks[c["_overlap"]["from_index"]], dict) else None}
                        if c["_overlap"] else None),
            "corpus": corpus,
            "include_in_default_retrieval": include,
            "exclusion_reason": None if include else reason,
            "applicability": meta["applicability"],
            "authority_class": meta["authority_class"],
            "superseded": meta["superseded"],
            "distribution": {"policy": meta["distribution_policy"], "rights_state": rstate},
            "review_status": meta["review_status_of"](c["source_element_ids"]),
        }
        return rec

    def _content_type(self, c, corpus) -> str:
        if corpus != "narrative":
            return corpus
        types = collections.Counter()
        desc = []

        def walk(e):
            desc.append(e)
            for ch in e["children"]:
                walk(self.els[ch])

        for e in c["_els"]:
            walk(e)
        for e in desc:
            types[e["type"]] += 1
        if types["code"]:
            return "code" if types["paragraph"] <= 1 else "mixed_code"
        if types["table"]:
            return "table" if types["paragraph"] <= 1 else "mixed_table"
        if any(e["type"] == "list" and e["attrs"].get("ordered") for e in desc):
            return "procedure"
        if types["figure"] and types["paragraph"] == 0:
            return "figure"
        if types["footnote"]:
            return "notes"
        return "prose"


def build_chunks(doc, cfg, **kw) -> list[dict]:
    ch = Chunker(doc, cfg, **kw)
    chunks = ch.run()
    # resolve overlap.from_chunk_id now that ids are final
    return chunks
