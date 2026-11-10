"""Parse Asciidoctor (HTML5 backend) single-page output into an element tree.

The tree is a *candidate* structured representation.  It is only accepted as
source text where :mod:`irpacks.align` establishes a match with the PDF.

Element dicts produced here have the keys:

``type``      element type (see ``BLOCK_TYPES``)
``anchor``    Asciidoctor id when present
``text``      plain text (leaf elements)
``md``        inline Markdown rendering (leaf elements)
``children``  ordered child elements (containers)
``attrs``     type-specific attributes
``align``     list of alignment units: plain strings expected to appear in the PDF
"""

from __future__ import annotations

import base64
import re
from typing import Any

from lxml import html as LH

from .util import collapse_ws, sha256_bytes

CONTAINER_TYPES = {
    "document", "division", "part", "chapter", "section", "list", "list_item", "admonition",
    "sidebar", "quote", "open_block", "example", "callout_list", "dlist", "dlist_item", "listing",
    "figure",
}

# Private-use markers delimit variable inline parts whose PDF rendering can
# differ from the HTML rendering (footnote reference labels, cross-reference
# text).  They are resolved against the PDF in :mod:`irpacks.align` and never
# survive into exported text.
FN_OPEN, FN_CLOSE = "\ue000", "\ue001"
XR_OPEN, XR_CLOSE, XR_SEP = "\ue002", "\ue003", "\ue004"
MARKERS = FN_OPEN + FN_CLOSE + XR_OPEN + XR_CLOSE + XR_SEP
ZWSP = "\u200b"

_MD_ESCAPE = re.compile(r"([\\`*_\[\]<>])")
_BLOCK_START = re.compile(r"^(\s*)([#>+\-]|\d+[.)])(\s)", re.M)


def md_escape(text: str) -> str:
    return _MD_ESCAPE.sub(r"\\\1", text)


def _classes(el) -> set[str]:
    return set((el.get("class") or "").split())


# ---------------------------------------------------------------------------
# Inline conversion


class Inline:
    """Accumulates plain text and Markdown for inline content."""

    def __init__(self) -> None:
        self.plain: list[str] = []
        self.md: list[str] = []
        self.footnotes: list[int] = []
        self.links: list[dict] = []
        self.xrefs: list[dict] = []

    def add(self, plain: str, md: str | None = None) -> None:
        self.plain.append(plain)
        self.md.append(md if md is not None else md_escape(plain))

    def result(self) -> tuple[str, str]:
        plain = collapse_ws("".join(self.plain).replace(ZWSP, ""))
        md = collapse_ws("".join(self.md).replace(ZWSP, ""))
        return plain, md


# Anchor ids present in the document being parsed.  Asciidoctor emits
# cross-file xrefs (e.g. ``href="_prepare_activity"`` or ``chapter.html#id``)
# that resolve to anchors of the single-page output; they are internal xrefs.
_DOC_IDS: set[str] = set()


def _internal_href(href: str) -> str:
    if not href or href.startswith("#"):
        return href
    if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):  # http:, mailto:, ...
        return href
    frag = href.split("#", 1)[1] if "#" in href else href
    return "#" + frag if frag in _DOC_IDS else href


def _code_span(text: str) -> str:
    if not text:
        return ""
    ticks = "`"
    while ticks in text:
        ticks += "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _inline_walk(el, acc: Inline, in_code: bool = False) -> None:
    if el.text:
        acc.add(el.text, el.text if in_code else None)
    for ch in el:
        if not isinstance(ch.tag, str):  # comments / processing instructions
            if ch.tail:
                acc.add(ch.tail, ch.tail if in_code else None)
            continue
        tag = ch.tag.lower()
        cls = _classes(ch)
        if tag == "sup" and "footnote" in cls:
            a = ch.find(".//a")
            num = (a.text_content() if a is not None else ch.text_content()).strip().strip("[]")
            try:
                n = int(num)
                acc.footnotes.append(n)
                acc.add(f"{FN_OPEN}{n}{FN_CLOSE}", f"[^{FN_OPEN}{n}{FN_CLOSE}]")
            except ValueError:
                acc.add(ch.text_content())
        elif tag == "code" and not in_code:
            sub = Inline()
            _inline_walk(ch, sub, in_code=True)
            raw = "".join(sub.plain)
            acc.plain.append(raw)
            acc.md.append(_code_span(collapse_ws(raw)))
            acc.footnotes += sub.footnotes
            acc.links += sub.links
            acc.xrefs += sub.xrefs
        elif tag in ("strong", "b") and not in_code:
            sub = Inline()
            _inline_walk(ch, sub)
            p, m = "".join(sub.plain), "".join(sub.md)
            acc.plain.append(p)
            acc.md.append(f"**{m.strip()}**" if m.strip() else m)
            acc.footnotes += sub.footnotes
            acc.links += sub.links
            acc.xrefs += sub.xrefs
        elif tag in ("em",) or (tag == "i" and "conum" not in cls and "fa" not in cls) and not in_code:
            sub = Inline()
            _inline_walk(ch, sub)
            p, m = "".join(sub.plain), "".join(sub.md)
            acc.plain.append(p)
            acc.md.append(f"*{m.strip()}*" if m.strip() else m)
            acc.footnotes += sub.footnotes
            acc.links += sub.links
            acc.xrefs += sub.xrefs
        elif tag == "i" and ("conum" in cls or "fa" in cls):
            pass  # icon / callout glyph; the following <b>(n)</b> carries the text
        elif tag == "a":
            href = _internal_href(ch.get("href") or "")
            sub = Inline()
            _inline_walk(ch, sub, in_code=in_code)
            p, m = "".join(sub.plain), "".join(sub.md)
            if href.startswith("#") and not in_code:
                acc.xrefs.append({"target": href[1:], "text": collapse_ws(p)})
                acc.plain.append(f"{XR_OPEN}{collapse_ws(p)}{XR_SEP}{href[1:]}{XR_CLOSE}")
                acc.md.append(f"[{XR_OPEN}{collapse_ws(m)}{XR_SEP}{href[1:]}{XR_CLOSE}](#{href[1:]})")
            elif href.startswith("#"):
                acc.plain.append(p)
                acc.md.append(p)
            elif href:
                acc.links.append({"href": href, "text": collapse_ws(p)})
                acc.plain.append(p)
                acc.md.append(f"[{m.strip()}]({href})" if not in_code else p)
            else:
                acc.plain.append(p)
                acc.md.append(m)
            acc.footnotes += sub.footnotes
        elif tag == "br":
            acc.add("\n", "  \n" if not in_code else "\n")
        elif tag == "img":
            alt = ch.get("alt") or ""
            acc.add(alt)
        else:
            _inline_walk(ch, acc, in_code=in_code)
        if ch.tail:
            acc.add(ch.tail, ch.tail if in_code else None)


def inline(el) -> dict:
    acc = Inline()
    _inline_walk(el, acc)
    plain, md = acc.result()
    # Protect Markdown block syntax at the start of an inline run.
    md = _BLOCK_START.sub(lambda m: m.group(1) + "\\" + m.group(2) + m.group(3), md)
    return {"text": plain, "md": md, "footnotes": acc.footnotes, "links": acc.links, "xrefs": acc.xrefs}


def pre_text(pre) -> dict:
    """Exact text of a <pre> block (whitespace preserved; callouts as text)."""
    raw = pre.text_content().replace(ZWSP, "")
    lang = None
    code = pre.find(".//code")
    if code is not None:
        lang = code.get("data-lang")
    strong_spans = [collapse_ws(s.text_content()) for s in pre.iter("strong")]
    return {"text": raw.rstrip("\n"), "lang": lang, "typed_input": [s for s in strong_spans if s]}


# ---------------------------------------------------------------------------
# Block parsing

_NUMBERED_TITLE = re.compile(r"^(Figure|Table|Listing|Example)\s+(\d+)\.\s+(.*)$", re.S)


def _numbered(title: str) -> dict:
    m = _NUMBERED_TITLE.match(title or "")
    if m:
        return {"label": m.group(1), "number": int(m.group(2)), "caption": collapse_ws(m.group(3))}
    return {"label": None, "number": None, "caption": collapse_ws(title or "")}


def _title_of(el) -> dict | None:
    t = el.find("./div[@class='title']")
    if t is None:
        return None
    r = inline(t)
    r.update(_numbered(r["text"]))
    return r


def _leaf(type_: str, text: str, md: str, anchor=None, attrs=None, align=None, inl=None) -> dict:
    e = {"type": type_, "anchor": anchor, "text": text, "md": md, "children": [], "attrs": attrs or {}}
    if inl:
        e["attrs"].setdefault("footnote_refs", inl.get("footnotes", []))
        e["attrs"].setdefault("links", inl.get("links", []))
        e["attrs"].setdefault("xrefs", inl.get("xrefs", []))
    e["align"] = align if align is not None else ([text] if text.strip() else [])
    return e


def _container(type_: str, children: list, anchor=None, attrs=None) -> dict:
    return {"type": type_, "anchor": anchor, "text": "", "md": "", "children": children,
            "attrs": attrs or {}, "align": []}


def _caption_leaf(title: dict, owner_type: str) -> dict:
    return _leaf("caption", title["text"], title["md"], attrs={
        "label": title["label"], "number": title["number"], "caption": title["caption"],
        "caption_of": owner_type}, inl=title)


def parse_blocks(parent, ctx: dict) -> list[dict]:
    """Parse block-level children of ``parent`` into element dicts."""
    out: list[dict] = []
    # Bare text directly in a container (e.g. simple admonition content).
    if parent.text and parent.text.strip():
        out.append(_leaf("paragraph", collapse_ws(parent.text), md_escape(collapse_ws(parent.text))))
    for el in parent:
        if not isinstance(el.tag, str):
            continue
        out.extend(parse_block(el, ctx))
        if el.tail and el.tail.strip():
            out.append(_leaf("paragraph", collapse_ws(el.tail), md_escape(collapse_ws(el.tail))))
    return out


def _table(el, ctx) -> dict:
    cap = el.find("./caption")
    title = None
    if cap is not None:
        title = inline(cap)
        title.update(_numbered(title["text"]))
    rows = []
    complex_ = False
    for section in ("thead", "tbody", "tfoot"):
        for tr in el.findall(f"./{section}/tr"):
            cells = []
            for cell in tr:
                if not isinstance(cell.tag, str) or cell.tag not in ("th", "td"):
                    continue
                colspan = int(cell.get("colspan", "1"))
                rowspan = int(cell.get("rowspan", "1"))
                if colspan > 1 or rowspan > 1:
                    complex_ = True
                blocks = []
                paras = cell.findall("./p")
                content_div = cell.find("./div[@class='content']")
                if content_div is not None:
                    complex_ = True
                    blocks = parse_blocks(content_div, ctx)
                    text = "\n\n".join(_flatten_text(b) for b in blocks)
                    md = "<br><br>".join(_flatten_md(b) for b in blocks)
                elif paras:
                    parts = [inline(p) for p in paras]
                    if len(parts) > 1:
                        complex_ = True
                    text = "\n\n".join(p["text"] for p in parts)
                    md = "<br><br>".join(p["md"] for p in parts)
                    for p in parts:
                        ctx["footnotes_seen"].extend(p["footnotes"])
                else:
                    r = inline(cell)
                    text, md = r["text"], r["md"]
                cells.append({"text": text, "md": md, "header": cell.tag == "th" or section == "thead",
                              "colspan": colspan, "rowspan": rowspan,
                              "halign": next((c[7:] for c in _classes(cell) if c.startswith("halign-")), None)})
            rows.append({"section": section, "cells": cells})
    align = []
    if title:
        align.append(title["text"])
    for r in rows:  # one unit per row (possibly empty) so units map 1:1 to rows
        align.append(" ".join(c["text"] for c in r["cells"]))
    attrs = {"rows": rows, "complex": complex_, "label": title["label"] if title else None,
             "number": title["number"] if title else None, "caption": title["caption"] if title else None,
             "caption_md": title["md"] if title else None,
             "n_rows": len(rows), "n_cols": max((sum(c["colspan"] for c in r["cells"]) for r in rows), default=0),
             "header_rows": sum(1 for r in rows if r["section"] == "thead")}
    body = "\n".join(" | ".join(c["text"] for c in r["cells"]) for r in rows)
    text = f"{title['text']}\n{body}" if title else body
    e = _leaf("table", text.strip(), "", anchor=el.get("id"), attrs=attrs, align=align)
    e["attrs"]["align_units"] = "caption+rows" if title else "rows"
    return e


def _flatten_text(e: dict) -> str:
    if e["type"] == "list":
        start = e["attrs"].get("start") or 1
        lines = []
        for i, it in enumerate(e["children"]):
            mark = f"{start + i}." if e["attrs"].get("ordered") else "\u2022"
            body = "\n".join(x for x in [it.get("text", "")] + [_flatten_text(c) for c in it["children"]] if x)
            lines.append(f"{mark} {body}")
        return "\n".join(lines)
    if e["children"]:
        return "\n".join(_flatten_text(c) for c in e["children"] if _flatten_text(c))
    return e.get("text", "")


def _flatten_md(e: dict) -> str:
    if e["children"]:
        return " ".join(_flatten_md(c) for c in e["children"])
    return e.get("md", "") or md_escape(e.get("text", ""))


def _image(el, ctx) -> dict:
    img = el.find(".//img")
    title = _title_of(el)
    src = img.get("src", "") if img is not None else ""
    data = None
    mime = None
    m = re.match(r"data:(image/[a-z+.-]+);base64,(.*)$", src, re.S)
    if m:
        mime = m.group(1)
        data = base64.b64decode(m.group(2))
    asset_attrs = {
        "alt": img.get("alt") if img is not None else None,
        "html_width": img.get("width") if img is not None else None,
        "mime": mime,
        "src_kind": "data_uri" if data else ("url" if src else None),
        "src_url": None if data else src or None,
        "sha256": sha256_bytes(data) if data else None,
        "bytes": len(data) if data else 0,
    }
    if data:
        ctx["image_bytes"][asset_attrs["sha256"]] = (mime, data)
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(data)) as im:
                asset_attrs["pixel_size"] = list(im.size)
        except Exception:
            asset_attrs["pixel_size"] = None
    attrs = dict(asset_attrs)
    if title:
        attrs.update({"label": title["label"], "number": title["number"], "caption": title["caption"],
                      "caption_md": title["md"]})
    else:
        attrs.update({"label": None, "number": None, "caption": None, "caption_md": None})
    children = []
    if title:
        children.append(_caption_leaf(title, "figure"))
    fig = _container("figure", children, anchor=el.get("id"), attrs=attrs)
    return fig


def parse_block(el, ctx: dict) -> list[dict]:
    tag = el.tag.lower()
    cls = _classes(el)
    anchor = el.get("id")
    style = (el.get("style") or "").replace(" ", "")
    if tag == "div" and "page-break-after:always" in style:
        ctx["layout_skips"]["page_break"] += 1
        return []
    if tag == "div" and "paragraph" in cls:
        p = el.find("./p")
        title = _title_of(el)
        r = inline(p if p is not None else el)
        ctx["footnotes_seen"].extend(r["footnotes"])
        if not r["text"].strip():
            ctx["layout_skips"]["empty_paragraph"] += 1
            return []
        out = []
        if title:
            out.append(_leaf("block_title", title["text"], title["md"], inl=title))
        out.append(_leaf("paragraph", r["text"], r["md"], anchor=anchor, inl=r))
        return out
    if tag in ("h2", "h3", "h4", "h5", "h6") and "discrete" in cls:
        r = inline(el)
        return [_leaf("heading", r["text"], r["md"], anchor=anchor,
                      attrs={"level": int(tag[1]), "discrete": True}, inl=r)]
    if tag == "div" and ("ulist" in cls or "olist" in cls):
        lst = el.find("./ul") if "ulist" in cls else el.find("./ol")
        title = _title_of(el)
        items = []
        for li in (lst if lst is not None else []):
            if not isinstance(li.tag, str) or li.tag != "li":
                continue
            p = li.find("./p")
            first = inline(p) if p is not None else {"text": "", "md": "", "footnotes": [], "links": [], "xrefs": []}
            ctx["footnotes_seen"].extend(first["footnotes"])
            kids = []
            for sub in li:
                if not isinstance(sub.tag, str) or sub is p:
                    continue
                kids.extend(parse_block(sub, ctx))
            item = _container("list_item", kids, anchor=li.get("id"),
                              attrs={"checked": None, "footnote_refs": first["footnotes"],
                                     "links": first["links"], "xrefs": first["xrefs"]})
            item["text"], item["md"] = first["text"], first["md"]
            item["align"] = [first["text"]] if first["text"].strip() else []
            items.append(item)
        attrs = {"ordered": "olist" in cls, "style": next(iter(cls - {"ulist", "olist"}), None),
                 "start": int(lst.get("start")) if lst is not None and lst.get("start") else None,
                 "title": title["text"] if title else None}
        out = []
        if title:
            out.append(_leaf("block_title", title["text"], title["md"], inl=title))
        out.append(_container("list", items, anchor=anchor, attrs=attrs))
        return out
    if tag == "div" and "dlist" in cls:
        items = []
        dl = el.find("./dl")
        cur = None
        for sub in (dl if dl is not None else []):
            if not isinstance(sub.tag, str):
                continue
            if sub.tag == "dt":
                r = inline(sub)
                cur = _container("dlist_item", [], attrs={"term": r["text"], "term_md": r["md"]})
                cur["text"], cur["md"], cur["align"] = r["text"], r["md"], [r["text"]]
                items.append(cur)
            elif sub.tag == "dd" and cur is not None:
                cur["children"].extend(parse_blocks(sub, ctx))
        return [_container("dlist", items, anchor=anchor)]
    if tag == "div" and ("listingblock" in cls or "literalblock" in cls):
        title = _title_of(el)
        pre = el.find(".//pre")
        r = pre_text(pre) if pre is not None else {"text": "", "lang": None, "typed_input": []}
        attrs = {"lang": r["lang"], "typed_input": r["typed_input"], "literal": "literalblock" in cls}
        code = _leaf("code", r["text"], "", attrs=attrs)
        if not title:
            code["anchor"] = anchor
            return [code]
        lattrs = {"label": title["label"], "number": title["number"], "caption": title["caption"],
                  "caption_md": title["md"]}
        return [_container("listing", [_caption_leaf(title, "listing"), code], anchor=anchor, attrs=lattrs)]
    if tag == "div" and "imageblock" in cls:
        return [_image(el, ctx)]
    if tag == "div" and "admonitionblock" in cls:
        kind = next((c for c in cls if c in ("note", "tip", "important", "warning", "caution")), "note")
        content = el.find(".//td[@class='content']")
        title = _title_of(content) if content is not None else None
        kids = []
        if content is not None:
            if title:
                kids.append(_leaf("block_title", title["text"], title["md"], inl=title))
            # Simple admonitions: inline content directly inside td.content.
            has_blocks = any(isinstance(c.tag, str) and c.tag == "div" for c in content)
            if not has_blocks:
                r = inline(content)
                ctx["footnotes_seen"].extend(r["footnotes"])
                if r["text"].strip():
                    kids.append(_leaf("paragraph", r["text"], r["md"], inl=r))
            else:
                for sub in content:
                    if not isinstance(sub.tag, str) or (sub.tag == "div" and sub.get("class") == "title"):
                        continue
                    kids.extend(parse_block(sub, ctx))
        return [_container("admonition", kids, anchor=anchor, attrs={"kind": kind})]
    if tag == "div" and "sidebarblock" in cls:
        content = el.find("./div[@class='content']")
        title = _title_of(content) if content is not None else None
        kids = []
        if title:
            kids.append(_leaf("block_title", title["text"], title["md"], inl=title))
        for sub in (content if content is not None else []):
            if not isinstance(sub.tag, str) or (sub.tag == "div" and sub.get("class") == "title"):
                continue
            kids.extend(parse_block(sub, ctx))
        return [_container("sidebar", kids, anchor=anchor, attrs={"title": title["text"] if title else None})]
    if tag == "div" and ("quoteblock" in cls or "verseblock" in cls):
        bq = el.find("./blockquote")
        kids = []
        if bq is not None:
            kids = parse_blocks(bq, ctx)
        else:
            pre = el.find("./pre")
            if pre is not None:
                kids = [_leaf("paragraph", pre.text_content().strip(), md_escape(pre.text_content().strip()))]
        attribution = el.find("./div[@class='attribution']")
        attr_text = None
        if attribution is not None:
            br = attribution.find("./br")
            if br is not None and attribution.find("./cite") is not None:
                # the PDF prints "— Name, Cite title"; the HTML separates the two with <br>
                br.tag = "span"
                br.tail = ", " + (br.tail or "").lstrip()
            r = inline(attribution)
            attr_text = r["text"]
            kids.append(_leaf("attribution", r["text"], r["md"], inl=r))
        title = _title_of(el)
        if title:
            kids.insert(0, _leaf("block_title", title["text"], title["md"], inl=title))
        return [_container("quote", kids, anchor=anchor, attrs={"attribution": attr_text,
                                                              "verse": "verseblock" in cls})]
    if tag == "div" and "stemblock" in cls:
        content = el.find("./div[@class='content']")
        tex = (content.text_content() if content is not None else el.text_content()).strip()
        return [_leaf("math", tex, "", anchor=anchor, attrs={"notation": "latex"},
                      align=[])]  # rendered math glyphs are aligned separately (see align.py)
    if tag == "div" and "colist" in cls:
        items = []
        for tr in el.iter("tr"):
            tds = tr.findall("./td")
            if len(tds) >= 2:
                num = collapse_ws(tds[0].text_content())
                r = inline(tds[1])
                it = _container("list_item", [], attrs={"callout": num, "footnote_refs": r["footnotes"],
                                                        "links": r["links"], "xrefs": r["xrefs"]})
                it["text"], it["md"], it["align"] = r["text"], r["md"], [r["text"]]
                items.append(it)
        if not items:  # ol-based colist
            for li in el.iter("li"):
                r = inline(li)
                it = _container("list_item", [], attrs={"callout": None})
                it["text"], it["md"], it["align"] = r["text"], r["md"], [r["text"]]
                items.append(it)
        return [_container("callout_list", items, anchor=anchor)]
    if tag == "table" and "tableblock" in cls:
        return [_table(el, ctx)]
    if tag == "div" and ("openblock" in cls or "exampleblock" in cls):
        content = el.find("./div[@class='content']")
        title = _title_of(el)
        kids = parse_blocks(content if content is not None else el, ctx)
        if title:
            kids.insert(0, _leaf("block_title", title["text"], title["md"], inl=title))
        typ = "example" if "exampleblock" in cls else "open_block"
        return [_container(typ, kids, anchor=anchor, attrs={"partintro": "partintro" in cls})]
    if tag == "hr":
        return [_leaf("thematic_break", "", "", align=[])]
    if tag == "div" and ("text-left" in cls or "text-right" in cls or "left" in cls) and not cls & {"sect1"}:
        return parse_blocks(el, ctx)
    if tag == "div" and "content" in cls:
        return parse_blocks(el, ctx)
    # Unknown element: keep its text visibly flagged rather than dropping it.
    txt = collapse_ws(el.text_content())
    ctx["unknown_blocks"].append({"tag": tag, "class": sorted(cls), "text": txt[:200]})
    if txt:
        return [_leaf("unknown_block", txt, md_escape(txt), anchor=anchor, attrs={"tag": tag, "class": sorted(cls)})]
    return []


_SECTNUM = re.compile(r"^\s*(\d+)\.\s+(.*)$", re.S)


def _section(el, level: int, ctx: dict) -> dict:
    h = el.find(f"./h{level + 1}")
    r = inline(h) if h is not None else {"text": "", "md": "", "footnotes": []}
    anchor = h.get("id") if h is not None else el.get("id")
    number = None
    title = r["text"]
    m = _SECTNUM.match(title)
    if level == 1 and m:
        number, title = int(m.group(1)), collapse_ws(m.group(2))
    # The section container carries the anchor; its heading does not (anchors stay unique).
    heading = _leaf("heading", r["text"], r["md"], anchor=None,
                    attrs={"level": level, "discrete": False, "number": number, "title": title}, inl=r)
    body_parent = el.find("./div[@class='sectionbody']") if level == 1 else el
    kids = [heading]
    for sub in (body_parent if body_parent is not None else []):
        if not isinstance(sub.tag, str) or sub is h:
            continue
        scls = _classes(sub)
        nxt = next((c for c in scls if re.fullmatch(r"sect\d", c)), None)
        if nxt:
            kids.append(_section(sub, int(nxt[4:]), ctx))
        else:
            kids.extend(parse_block(sub, ctx))
    typ = "section"
    if level == 1:
        typ = "chapter" if number is not None else "division"
    sec = _container(typ, kids, anchor=anchor, attrs={"level": level, "number": number, "title": title})
    return sec


def parse_document(html_path: str) -> dict:
    """Return ``{"root": element-tree, "footnotes": {...}, "images": {...}, "meta": {...}}``."""
    doc = LH.parse(html_path, parser=LH.HTMLParser(huge_tree=True, encoding="utf-8")).getroot()
    _DOC_IDS.clear()
    _DOC_IDS.update(i for i in doc.xpath("//@id"))
    ctx: dict[str, Any] = {"image_bytes": {}, "footnotes_seen": [], "unknown_blocks": [],
                           "layout_skips": {"page_break": 0, "empty_paragraph": 0}}
    meta = {
        "generator": (doc.xpath("//meta[@name='generator']/@content") or [None])[0],
        "title": (doc.xpath("//title/text()") or [None])[0],
        "author": (doc.xpath("//meta[@name='author']/@content") or [None])[0],
    }
    content = doc.get_element_by_id("content")
    top: list[dict] = []
    current_part = None
    for el in content:
        if not isinstance(el.tag, str):
            continue
        cls = _classes(el)
        if el.get("id") == "preamble":
            body = el.find("./div[@class='sectionbody']")
            top.append(_container("division", parse_blocks(body if body is not None else el, ctx),
                                  anchor="preamble", attrs={"level": 1, "number": None, "title": "Front Matter",
                                                            "synthetic_title": True}))
            continue
        if el.tag == "h1" and "sect0" in cls:
            r = inline(el)
            heading = _leaf("heading", r["text"], r["md"], anchor=None,
                            attrs={"level": 0, "discrete": False, "number": None, "title": r["text"]}, inl=r)
            m = re.match(r"Part\s+(\d+)", r["text"])
            current_part = _container("part", [heading], anchor=el.get("id"),
                                      attrs={"level": 0, "number": int(m.group(1)) if m else None,
                                             "title": r["text"]})
            top.append(current_part)
            continue
        if "partintro" in cls and current_part is not None:
            current_part["children"].extend(parse_block(el, ctx))
            continue
        if "sect1" in cls:
            sec = _section(el, 1, ctx)
            if current_part is not None and sec["type"] == "chapter":
                current_part["children"].append(sec)
            elif current_part is not None and sec["type"] == "division":
                # Back matter after the last part (Afterword, Index, Colophon) ends the part.
                current_part = None
                top.append(sec)
            else:
                top.append(sec)
            continue
        top.extend(parse_block(el, ctx))
    # Footnote definitions
    footnotes = {}
    fn_root = doc.get_element_by_id("footnotes", None)
    if fn_root is not None:
        for fd in fn_root.findall("./div"):
            if "footnote" not in _classes(fd):
                continue
            a = fd.find("./a")
            num = int(a.text_content().strip()) if a is not None else None
            if a is not None:  # drop the numeric back-link; numbering is resolved against the PDF
                tail = a.tail or ""
                a.getparent().remove(a)
                fd.text = (fd.text or "") + re.sub(r"^\s*\.\s*", "", tail)
            r = inline(fd)
            text, md = r["text"], r["md"]
            footnotes[num] = {"number": num, "anchor": fd.get("id"), "text": text, "md": md,
                              "links": r["links"], "xrefs": r["xrefs"]}
    root = _container("document", top, anchor=None, attrs={})
    return {"root": root, "footnotes": footnotes, "images": ctx["image_bytes"], "meta": meta,
            "unknown_blocks": ctx["unknown_blocks"], "layout_skips": ctx["layout_skips"],
            "footnote_refs_in_order": ctx["footnotes_seen"]}


def iter_elements(e: dict, depth: int = 0):
    yield e, depth
    for c in e["children"]:
        yield from iter_elements(c, depth + 1)
