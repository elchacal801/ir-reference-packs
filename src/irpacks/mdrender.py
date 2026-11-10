"""Render Markdown (full book and per-unit files) from a canonical document.

Only ``document.json`` content is used, so corrections made upstream propagate
to every export.  Generated (non-source) text is limited to navigation,
structural markup and clearly labelled conversion notes / omission notices.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from .adochtml import md_escape

ALERT = {"note": "NOTE", "tip": "TIP", "important": "IMPORTANT", "warning": "WARNING", "caution": "CAUTION"}
SECTION_TYPES = {"part", "chapter", "division", "section"}


def _fence(code: str) -> str:
    runs = [len(m.group(0)) for m in re.finditer(r"`+", code)]
    return "`" * max(3, (max(runs) + 1) if runs else 3)


def _cell_md(c: dict) -> str:
    t = c.get("md") or md_escape(c.get("text", ""))
    return t.replace("|", "\\|").replace("\n", "<br>")


class Renderer:
    def __init__(self, doc: dict, assets: dict[str, dict], *, omitted: dict[str, dict] | None = None,
                 asset_prefix: str = "", page_anchor_prefix: str = "", index_link_target: str = ""):
        self.doc = doc
        self.els = {e["id"]: e for e in doc["elements"]}
        self.assets = assets
        self.omitted = omitted or {}
        self.asset_prefix = asset_prefix
        self.page_anchor_prefix = page_anchor_prefix
        self.index_link_target = index_link_target
        self.emitted_pages: set[int] = set()
        self.page_of_label: dict[str, int] = {}
        self.labels: dict[int, str | None] = {}
        for e in doc["elements"]:
            for s in e["source_spans"]:
                self.labels.setdefault(s["page"], s.get("page_label"))
        for p in doc.get("page_labels") or []:
            self.labels[p["page"]] = p["label"]

    # ------------------------------------------------------------------
    def first_page(self, el: dict) -> int | None:
        pages = [s["page"] for s in el["source_spans"] if s.get("page")]
        return min(pages) if pages else None

    def page_anchor(self, el: dict) -> list[str]:
        p = self.first_page(el)
        if p is None or p in self.emitted_pages:
            return []
        lines = []
        last = max(self.emitted_pages, default=p - 1)
        # Pages skipped since the last anchor (inside a long block, or visual-only
        # pages) get anchors here so every page remains linkable.
        for q in range(last + 1 if last < p else p, p + 1):
            if q in self.emitted_pages:
                continue
            self.emitted_pages.add(q)
            lab = self.labels.get(q)
            lines.append(f'<a id="pdf-page-{q}"></a><!-- PDF page {q}, printed page {lab} -->')
        return lines

    def omission(self, el: dict) -> list[str]:
        o = self.omitted[el["id"]]
        locs = ", ".join(f"printed p. {s['page_label']} (PDF p. {s['page']})" for s in o.get("source_spans", [])[:3])
        what = {"figure": "Figure image", "quote": "Quotation"}.get(el["type"], el["type"].replace("_", " ").capitalize())
        cap = el["attrs"].get("caption")
        label = f"{what}" + (f" — {el['attrs'].get('label')} {el['attrs'].get('number')}: {cap}" if cap and el["attrs"].get("number") else "")
        return [f"> **[Omitted from this public distribution]** {label}. Reason: {(o.get('reason') or o.get('state')).rstrip('.')}. "
                f"Source: {locs or 'see page map'}."]

    def heading_level(self, el: dict, mode: str) -> int:
        t = el["type"]
        if mode == "book":
            if t in ("part", "chapter", "division"):
                return 2
            lvl = el["attrs"].get("level") or 2
            return min(6, lvl + 1)
        # unit file
        if t in ("part", "chapter", "division"):
            return 1
        lvl = el["attrs"].get("level") or 2
        return min(6, lvl)

    # ------------------------------------------------------------------
    def render(self, el: dict, mode: str = "book", top: bool = True) -> list[str]:
        if el["id"] in self.omitted:
            anchor = [f'<a id="{el["anchor"]}"></a>'] if el.get("anchor") else []
            title = [f"{'#' * self.heading_level(el, mode)} {md_escape(el['attrs']['title'])}", ""] \
                if el["type"] in SECTION_TYPES and el["attrs"].get("title") else []
            return anchor + self.page_anchor(el) + title + self.omission(el) + [""]
        t = el["type"]
        out: list[str] = []
        if t in SECTION_TYPES:
            kids = [self.els[c] for c in el["children"]]
            head = next((k for k in kids if k["type"] == "heading" and not k["attrs"].get("discrete")), None)
            title = (head["md"] or md_escape(head["text"])) if head else md_escape(el["attrs"].get("title") or "")
            if el.get("anchor"):
                out.append(f'<a id="{el["anchor"]}"></a>')
            out += self.page_anchor(head or el)
            if title and not el["attrs"].get("synthetic_title"):
                out += ["#" * self.heading_level(el, mode) + " " + title, ""]
            elif el["attrs"].get("synthetic_title"):
                out += [f"<!-- {el['attrs'].get('title')}: content preceding the first titled section -->", ""]
            for k in kids:
                if k is head:
                    continue
                out += self.render(k, mode)
            return out
        out += self.page_anchor(el)
        out += self.block(el, mode)
        out.append("")
        return out

    def block(self, el: dict, mode: str, indent: str = "") -> list[str]:
        lines = self._block(el, mode, indent)
        if el.get("anchor") and el["type"] not in SECTION_TYPES:
            lines = [f'{indent}<a id="{el["anchor"]}"></a>'] + lines
        return lines

    def _block(self, el: dict, mode: str, indent: str = "") -> list[str]:
        t = el["type"]
        a = el["attrs"]
        kids = [self.els[c] for c in el["children"]]
        md = el.get("md") or md_escape(el.get("text") or "")
        if el["id"] in self.omitted:
            return [indent + x for x in self.omission(el)]
        if t in ("paragraph", "unknown_block", "pdf_text"):
            return [indent + line for line in md.split("\n")]
        if t == "heading":
            lvl = min(6, (a.get("level") or 3) + (1 if mode == "book" else 0))
            return [indent + "#" * lvl + " " + md]
        if t == "block_title":
            return [indent + f"**{md}**"]
        if t == "list":
            lines = []
            n = a.get("start") or 1
            for i, it in enumerate(kids):
                mark = f"{n + i}." if a.get("ordered") else "-"
                sub_indent = indent + " " * (len(mark) + 1)
                item_md = it.get("md") or md_escape(it.get("text") or "")
                if it.get("anchor"):
                    item_md = f'<a id="{it["anchor"]}"></a>' + item_md
                if it["id"] in self.omitted:
                    item_md = " ".join(self.omission(it))
                lines.append(f"{indent}{mark} {item_md}".rstrip())
                for ch in (self.els[c] for c in it["children"]):
                    lines.append("")
                    lines += self.block(ch, mode, sub_indent)
            return lines
        if t == "callout_list":
            lines = []
            for it in kids:
                num = it["attrs"].get("callout")
                lines.append(f"{indent}- ({num}) {it.get('md') or md_escape(it.get('text') or '')}")
            return lines
        if t == "dlist":
            lines = []
            for it in kids:
                lines.append(f"{indent}- **{it['attrs'].get('term_md') or md_escape(it['attrs'].get('term', ''))}**")
                for ch in (self.els[c] for c in it["children"]):
                    lines.append("")
                    lines += self.block(ch, mode, indent + "  ")
            return lines
        if t == "listing":
            lines = []
            for ch in kids:
                if ch["type"] == "caption":
                    lines += [f"{indent}**{ch.get('md') or md_escape(ch['text'])}**", ""]
                else:
                    lines += self.block(ch, mode, indent)
            return lines
        if t == "code":
            code = el.get("text") or ""
            f = _fence(code)
            lang = a.get("lang") or ""
            return [indent + f + lang] + [indent + l for l in code.split("\n")] + [indent + f]
        if t == "table":
            return self.table(el, indent)
        if t == "figure":
            return self.figure(el, kids, indent)
        if t == "math":
            asset = self.assets.get(el["id"])
            lines = [indent + "$$", *[indent + l for l in (el.get("text") or "").split("\n")], indent + "$$"]
            if asset:
                lines += ["", f"{indent}![Formula as printed in the PDF]({self.asset_prefix}{asset['path']})"]
            return lines
        if t == "admonition":
            lines = [f"{indent}> [!{ALERT.get(a.get('kind'), 'NOTE')}]"]
            for ch in kids:
                for l in self.block(ch, mode):
                    lines.append(f"{indent}> {l}" if l.strip() else f"{indent}>")
                lines.append(f"{indent}>")
            return lines[:-1] if lines[-1].strip() == ">" else lines
        if t in ("sidebar", "example", "open_block", "quote"):
            lines = []
            prefix = "" if t == "open_block" else "> "
            if t == "sidebar":
                lines.append(f"{indent}<!-- sidebar -->")
            for ch in kids:
                if ch["type"] == "attribution":
                    lines.append(f"{indent}{prefix}— {ch.get('md') or md_escape(ch['text']).lstrip('— ')}".rstrip())
                    continue
                for l in self.block(ch, mode):
                    lines.append(f"{indent}{prefix}{l}" if l.strip() else f"{indent}{prefix.rstrip()}")
                lines.append(f"{indent}{prefix.rstrip()}")
            while lines and lines[-1].strip() in (">", ""):
                lines.pop()
            return lines
        if t == "attribution":
            return [indent + "— " + md.lstrip("— ")]
        if t == "caption":
            return [indent + f"*{md}*"]
        if t == "footnote":
            return [f"[^{el['id'].replace('.', '-')}]: {md}"]
        if t == "thematic_break":
            return [indent + "---"]
        if t == "toc_entry":
            depth = int(max(0, (a.get("indent") or 0)) // 10)
            tl = a.get("target_page_label")
            title = md_escape(a.get("title") or el["text"])
            return [f"{'  ' * depth}- {title}" + (f" — {tl}" if tl else "")]
        if t == "index_entry":
            if a.get("entry_kind") == "letter":
                return [f"**{md_escape(el['text'])}**"]
            depth = a.get("level") or 0
            locs = []
            for lab in a.get("locator_labels") or []:
                pg = self.page_of_label.get(lab)
                locs.append(f"[{lab}]({self.index_link_target}#pdf-page-{pg})" if pg else md_escape(lab))
            see = f" (see {md_escape(a['see'])})" if a.get("see") else ""
            term = md_escape(a.get("term") or el["text"])
            return [f"{'  ' * depth}- {term}" + (", " + ", ".join(locs) if locs else "") + see]
        # containers without special handling
        lines = []
        for ch in kids:
            lines += self.block(ch, mode, indent)
            lines.append("")
        return lines

    def figure(self, el: dict, kids: list[dict], indent: str) -> list[str]:
        a = el["attrs"]
        cap = next((k for k in kids if k["type"] == "caption"), None)
        alt = a.get("caption") or a.get("alt") or "Figure"
        asset = self.assets.get(el["id"])
        lines = []
        if asset and asset.get("path"):
            lines.append(f"{indent}![{md_escape(alt)}]({self.asset_prefix}{asset['path']})")
        elif a.get("src_url"):
            lines.append(f"{indent}*[Image not captured: {md_escape(alt)}. Published image: <{a['src_url']}>]*")
        else:
            lines.append(f"{indent}*[Image not included in this distribution: see the source PDF page for the visual.]*")
        if cap is not None:
            lines += ["", f"{indent}*{cap.get('md') or md_escape(cap['text'])}*"]
        return lines

    def table(self, el: dict, indent: str) -> list[str]:
        a = el["attrs"]
        rows = a["rows"]
        lines = []
        if a.get("caption"):
            lab = f"{a.get('label')} {a.get('number')}. " if a.get("number") else ""
            cap_md = a.get("caption_md") or md_escape(a["caption"])
            if lab and cap_md.startswith(md_escape(lab.strip())):
                lab = ""  # caption markup already carries the label
            lines += [f"{indent}**{lab}{cap_md}**", ""]
        if not rows:
            return lines
        if not a.get("complex") and rows and len({sum(c["colspan"] for c in r["cells"]) for r in rows}) == 1:
            head = [r for r in rows if r["section"] == "thead"]
            body = [r for r in rows if r["section"] != "thead"]
            hdr = head[0] if head else None
            ncol = sum(c["colspan"] for c in rows[0]["cells"])
            if hdr is not None:
                lines.append(indent + "| " + " | ".join(_cell_md(c) for c in hdr["cells"]) + " |")
            else:
                lines.append(indent + "| " + " | ".join(" " for _ in range(ncol)) + " |")
            lines.append(indent + "|" + "|".join("---" for _ in range(ncol)) + "|")
            for r in (head[1:] if head else []) + body:
                lines.append(indent + "| " + " | ".join(_cell_md(c) for c in r["cells"]) + " |")
        else:
            lines.append(indent + "<table>")
            for sec in ("thead", "tbody", "tfoot"):
                rs = [r for r in rows if r["section"] == sec]
                if not rs:
                    continue
                lines.append(f"{indent}<{sec}>")
                for r in rs:
                    cells = []
                    for c in r["cells"]:
                        tag = "th" if c["header"] else "td"
                        attrs = (f' colspan="{c["colspan"]}"' if c["colspan"] > 1 else "") + (
                            f' rowspan="{c["rowspan"]}"' if c["rowspan"] > 1 else "")
                        body = "<br><br>".join(html.escape(p).replace("\n", "<br>") for p in c["text"].split("\n\n"))
                        cells.append(f"<{tag}{attrs}>{body}</{tag}>")
                    lines.append(f"{indent}<tr>{''.join(cells)}</tr>")
                lines.append(f"{indent}</{sec}>")
            lines.append(indent + "</table>")
            asset = self.assets.get(el["id"])
            if asset:
                for pth in asset.get("paths", [asset.get("path")]):
                    if pth:
                        lines += ["", f"{indent}![Table as printed in the PDF]({self.asset_prefix}{pth})"]
        return lines


# ---------------------------------------------------------------------------


def unit_files(doc: dict) -> list[tuple[str, str]]:
    """(element_id, file name) for each top-level reading unit."""
    els = {e["id"]: e for e in doc["elements"]}
    root = els[doc["root_id"]]
    out = []
    n_fm = 0
    for cid in root["children"]:
        e = els[cid]
        title = e["attrs"].get("title") or cid
        if e["type"] == "part":
            out.append((cid, f"part-{e['attrs'].get('number')}.md"))
            for ch in e["children"]:
                c = els[ch]
                if c["type"] == "chapter":
                    out.append((ch, f"{ch}-{_slug(c['attrs'].get('title') or '')}.md"))
        elif e["type"] == "chapter":
            out.append((cid, f"{cid}-{_slug(e['attrs'].get('title') or '')}.md"))
        elif cid.startswith("fm-"):
            out.append((cid, f"00-{n_fm:02d}-{_slug(title)}.md"))
            n_fm += 1
        else:
            out.append((cid, f"zz-{_slug(title)}.md"))
    return out


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:50].rstrip("-") or "x"


def _descendant_ids(els: dict, eid: str, stop_types=("chapter",)) -> list[str]:
    out = [eid]
    for c in els[eid]["children"]:
        if els[c]["type"] in stop_types and eid != c:
            continue
        out += _descendant_ids(els, c, stop_types)
    return out


def _drop_dead_links(md: str, anchors: set[str]) -> str:
    """Links to anchors absent from this document (e.g. outside a pilot scope) become plain text."""
    return re.sub(r"\[([^\]]*)\]\(#([^)\s]+)\)",
                  lambda m: m.group(0) if (m.group(2) in anchors or m.group(2).startswith("pdf-page-")) else m.group(1), md)


def _anchors(doc: dict) -> set[str]:
    return {e["anchor"] for e in doc["elements"] if e.get("anchor")}


def render_book(doc: dict, assets: dict, *, omitted: dict | None = None, header: list[str] | None = None) -> str:
    r = Renderer(doc, assets, omitted=omitted)
    r.page_of_label = _page_of_label(doc)
    els = r.els
    root = els[doc["root_id"]]
    lines = [f"# {doc['title']}", ""]
    lines += header or []
    lines += ["## Contents", ""]
    for cid in root["children"]:
        e = els[cid]
        lines.append(f"- [{_title(els, e)}](#{e.get('anchor') or cid})")
        for sub in e["children"]:
            s = els[sub]
            if s["type"] in ("chapter", "section", "division"):
                lines.append(f"  - [{_title(els, s)}](#{s.get('anchor') or sub})")
    lines.append("")
    for cid in root["children"]:
        lines += r.render(els[cid], "book")
    return _tidy(_drop_dead_links("\n".join(lines), _anchors(doc)))


def render_units(doc: dict, assets: dict, *, omitted: dict | None = None) -> dict[str, str]:
    """{file name: markdown} for chapters/ (one per top-level unit)."""
    els = {e["id"]: e for e in doc["elements"]}
    files = unit_files(doc)
    owner: dict[str, str] = {}
    for eid, fname in files:
        stop = ("chapter",) if els[eid]["type"] == "part" else ()
        for d in _descendant_ids(els, eid, stop):
            if els[d].get("anchor"):
                owner[els[d]["anchor"]] = fname
    out = {}
    page_of_label = _page_of_label(doc)
    for eid, fname in files:
        r = Renderer(doc, assets, omitted=omitted, asset_prefix="../", index_link_target="../book.md")
        r.page_of_label = page_of_label
        e = els[eid]
        if e["type"] == "part":
            part = dict(e)
            part["children"] = [c for c in e["children"] if els[c]["type"] != "chapter"]
            r.els[eid] = part
            md = "\n".join(r.render(part, "unit"))
        else:
            md = "\n".join(r.render(e, "unit"))

        def relink(m):
            anchor = m.group(1)
            f = owner.get(anchor)
            return f"]({f}#{anchor})" if f and f != fname else m.group(0)

        md = re.sub(r"\]\(#([^)\s]+)\)", relink, _drop_dead_links(md, _anchors(doc)))
        out[fname] = _tidy(md)
    return out


def _title(els, e) -> str:
    head = next((els[c] for c in e["children"] if els[c]["type"] == "heading"), None)
    t = head["text"] if head else (e["attrs"].get("title") or e["id"])
    return md_escape(t).replace("[", "\\[").replace("]", "\\]")


def _page_of_label(doc: dict) -> dict[str, int]:
    m: dict[str, int] = {}
    for e in doc["elements"]:
        for s in e["source_spans"]:
            if s.get("page_label") and s["page_label"] not in m:
                m[s["page_label"]] = s["page"]
    return m


def _tidy(md: str) -> str:
    """Collapse runs of blank lines outside fenced code; code blocks are left byte-for-byte."""
    out, blank, fence = [], 0, None
    for line in md.split("\n"):
        m = re.match(r"^((?:\s*>\s?)*\s*)(`{3,})", line)
        if fence is None and m:
            fence = (m.group(1), m.group(2))
        elif fence is not None and line.startswith(fence[0]) and line[len(fence[0]):].strip() == fence[1]:
            fence = None
            out.append(line)
            blank = 0
            continue
        if fence is None and not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    return "\n".join(out).strip() + "\n"
