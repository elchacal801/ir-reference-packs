"""Route ``learn_html_pages``: published Microsoft Learn articles (captured HTML) -> canonical document.

Each recipe artifact is one article: one authoritative input, hash-pinned in the lock. The article body is the
DocFX-rendered ``div.content`` that follows the title. Headings carry stable ``id`` anchors and become sections.
Paragraphs, lists (including checklists), code (``pre > code.lang-*``), tables, alerts (``div.NOTE`` / ``TIP`` /
``IMPORTANT`` / ``WARNING`` / ``CAUTION``) and images map onto the same element types as the book. Text is taken
from the DOM as data:
* scripts are never executed;
* links are not followed;
* images are not downloaded: they are recorded with alt text and an absolute URL.

The same code serves other documentation sites through a *profile* (``PROFILES``): where the article body and
title are, how alerts are marked up, which page chrome to skip, which ``<meta>`` tags carry version metadata.
Routes using it:
* ``aws_docs_html``: docs.aws.amazon.com (``div#main-col-body``, ``div.awsdocs-note``);
* ``gcp_docs_html``: docs.cloud.google.com, Google DevSite (``div.devsite-article-body``, ``aside.note``,
  ``devsite-selector`` tabs). The update date and the content license come from the page footer.

Web articles are not paginated. Every element's locator is the article URL plus the anchor of the section that
contains it (``locator_type: section_anchor``). The page metadata (``git_commit_id``, ``updated_at``, ``ms.date``,
``document_id``) is recorded per article in ``source-map.json`` and drives change detection.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import lxml.html

from .mdrender import md_escape

ROUTE = "learn_html_pages"
META_KEYS = ("ms.date", "updated_at", "git_commit_id", "document_id", "source_path", "original_content_git_url",
             "ms.topic", "ms.service", "ms.author", "description", "locale")
AWS_META_KEYS = ("product", "guide", "description", "abstract", "guide-locale", "this_doc_product", "pdf")
NOTE_KINDS = {"note": "note", "tip": "tip", "important": "important", "warning": "warning", "caution": "caution"}
ALERTS = {"NOTE": "note", "TIP": "tip", "IMPORTANT": "important", "WARNING": "warning", "CAUTION": "caution"}
SKIP_TAGS = {"script", "style", "noscript", "template", "button"}
CODE_LANGS = {"json", "bash", "shell", "sh", "python", "powershell", "yaml", "sql", "javascript", "java", "xml", "ruby",
              "go", "csharp", "kusto", "kql", "hcl", "toml", "html", "css", "typescript", "rust", "php"}
INLINE_TAGS = {"b", "strong", "i", "em", "a", "code", "span", "sup", "sub", "kbd", "abbr", "u", "small", "mark"}
BLOCK_TAGS = {"ul", "ol", "pre", "table", "div", "p", "blockquote", "dl", "details", "h2", "h3", "h4", "h5", "h6"}
BLOCK_XPATH = ".//p|.//ul|.//ol|.//table|.//pre|.//div|.//blockquote|.//h2|.//h3|.//h4|.//h5|.//h6"
# Block-level tags whose text, when collected as inline text (a table cell), goes on its own line.
SEPARATED_TAGS = {"p", "div", "li", "dt", "dd", "pre", "blockquote", "section", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
_HID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_WS = re.compile(r"[ \t\r\f\v]*\n[ \t\r\f\v]*|[ \t\r\f\v]+")


def page_metadata(html: str, keys: tuple = META_KEYS) -> dict:
    """Page metadata from <meta name=...> tags and <title> (regex over the captured bytes; nothing is run)."""
    out = {}
    for k in keys:
        m = re.search(r'<meta name="' + re.escape(k) + r'" content="([^"]*)"', html)
        if m:
            out[k.replace(".", "_")] = lxml.html.fromstring(f"<p>{m.group(1)}</p>").text_content() if m.group(1) else ""
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if m:
        out["html_title"] = lxml.html.fromstring(f"<p>{m.group(1)}</p>").text_content().strip()
    return out


def meta_for(html: str, prof: dict) -> dict:
    """Page metadata for a profile: its ``<meta>`` keys, plus what the profile reads from the page itself."""
    return {**page_metadata(html, prof["meta_keys"]), **(prof["meta"](html) if prof.get("meta") else {})}


def profile_for(url: str | None = None, route: str | None = None) -> dict:
    if route:
        return PROFILES[route]
    host = urlsplit(url or "").hostname
    return next((p for p in PROFILES.values() if p["host"] == host), PROFILES[ROUTE])


def observe(path: str | Path, url: str = "https://learn.microsoft.com/") -> dict:
    """Version fingerprint of a captured page: page metadata plus a hash of the article body text."""
    html = Path(path).read_text(encoding="utf-8")
    prof = profile_for(url)
    pm = meta_for(html, prof)
    art = _Article("x", url, lxml.html.fromstring(html), pm, "x", prof)
    body = _norm(art.body.text_content())
    return {"title": art.title, **{k: pm.get(k) for k in ("git_commit_id", "updated_at", "ms_date", "document_id",
                                                         "source_path")},
            "body_text_sha256": _sha(body), "body_chars": len(body)}


def _wrap(node):
    """A <p> holding a copy of an inline element (without its tail), so the element's own formatting is kept."""
    import copy
    p = lxml.html.Element("p")
    c = copy.deepcopy(node)
    c.tail = None
    p.append(c)
    return p


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _bold(md: str) -> str:
    """Markdown bold, without doubling emphasis the source already applied."""
    return md if not md or (md.startswith("**") and md.endswith("**")) else f"**{md}**"


def _space(s: str) -> str:
    """HTML whitespace in running text (line wraps in the source included) is one space; only <br> breaks a line."""
    return re.sub(r"[ \t\r\n\f\v]+", " ", s)


def _norm(s: str) -> str:
    return _WS.sub(lambda m: "\n" if "\n" in m.group(0) else " ", s).strip()


def _norm_lines(s: str) -> str:
    """``_norm``, with runs of line breaks (between separated blocks) collapsed to one."""
    return re.sub(r"\n{2,}", "\n", _norm(s))


# ------------------------------------------------------------------ site profiles

def _learn_body(root, unit_id):
    bodies = root.xpath('//main//div[contains(concat(" ", normalize-space(@class), " "), " content ")]')
    if len(bodies) < 2:
        raise ValueError(f"{unit_id}: expected a title block and an article body (div.content), found {len(bodies)}")
    h1 = bodies[0].find(".//h1")
    return (_norm(h1.text_content()) if h1 is not None else None), bodies[1]


def _learn_alert(node):
    cls = (node.get("class") or "").split()
    if node.tag != "div" or not any(c in ALERTS for c in cls):
        return None
    kids = [k for k in node if isinstance(k.tag, str)]
    kind = ALERTS[next(c for c in cls if c in ALERTS)]
    if kids and kids[0].tag == "p" and _norm(kids[0].text_content()).lower() == kind:
        kids = kids[1:]  # the alert's printed label ("Note", "Important", ...)
    return kind, kids


def _aws_body(root, unit_id):
    bodies = root.xpath('//div[@id="main-col-body"]')
    if not bodies:
        raise ValueError(f"{unit_id}: expected an article body (div#main-col-body)")
    h1 = bodies[0].find(".//h1")
    return (_norm(h1.text_content()) if h1 is not None else None), bodies[0]


def _aws_alert(node):
    cls = (node.get("class") or "").split()
    if node.tag != "div" or "awsdocs-note" not in cls:
        return None
    title = node.xpath('./div[contains(@class, "awsdocs-note-title")]')
    label = _norm(title[0].text_content()).lower() if title else "note"
    kind = NOTE_KINDS.get(label, "note")
    text = node.xpath('./div[contains(@class, "awsdocs-note-text")]')
    return kind, [k for k in (text[0] if text else node) if isinstance(k.tag, str) and k not in title]


def _aws_chrome(node) -> bool:
    cls = (node.get("class") or "").split()
    return (node.tag.startswith(("awsdocs-", "awsui-")) or node.tag == "h1"
            or any(c in ("awsdocs-page-header-container", "highlights") for c in cls))


GCP_ALERTS = {"note": "note", "caution": "caution", "warning": "warning", "special": "important", "key-point": "tip",
              "key-term": "note", "objective": "note", "success": "tip", "beta": "note", "preview": "note",
              "deprecated": "warning"}
_ALERT_LABEL = re.compile(r"(note|important|caution|warning|key points?|key terms?|objective|success|preview|beta|"
                          r"deprecated|tip)\s*:?", re.I)
GCP_CONTAINERS = {"devsite-selector", "devsite-code", "devsite-expandable", "devsite-filter"}
GCP_CHROME_TAGS = {"devsite-hats-survey", "devsite-feedback", "devsite-thumb-rating", "devsite-toc", "devsite-video",
                   "devsite-tooltip", "devsite-bookmark", "cloud-free-trial",  # survey, rating, trial promotion
                   "form", "devsite-select"}                                    # interactive table filters


def _gcp_body(root, unit_id):
    bodies = root.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " devsite-article-body ")]')
    if not bodies:
        raise ValueError(f"{unit_id}: expected an article body (div.devsite-article-body)")
    # the title is the page-title heading; some pages print it as the body's first <h1> instead
    h1 = root.xpath('//h1[contains(@class, "devsite-page-title")]') or bodies[0].xpath(".//h1")
    if not h1:
        return None, bodies[0]
    import copy
    title = copy.deepcopy(h1[0])
    for w in title.xpath(".//devsite-actions|.//devsite-feature-tooltip|.//button"):  # bookmark widget in the title
        w.drop_tree()
    return _norm(title.text_content()), bodies[0]


def _gcp_meta(html: str) -> dict:
    """DevSite footer: 'Last updated 2026-10-20 UTC.' and the content license sentence."""
    out = {}
    m = re.search(r"Last updated (\d{4}-\d{2}-\d{2}) UTC", html)
    if m:
        out["updated_at"] = m.group(1)
    m = re.search(r"the content of this page is licensed under the\s*(?:<a [^>]*>)?\s*([^<]+?)\s*(?:</a>)?\s*,", html)
    if m:
        out["page_license"] = _norm(m.group(1))
    return out


def _runs(node) -> list:
    """An element's content as block nodes: inline runs (text, <strong>, <a>, ...) are wrapped in a synthetic <p>.
    Copies are made, so the captured page tree is never changed."""
    import copy
    out, cur = [], None
    if (node.text or "").strip():
        cur = lxml.html.Element("p")
        cur.text = node.text
    for k in node:
        if not isinstance(k.tag, str):
            continue
        if k.tag in BLOCK_TAGS or k.tag in GCP_CONTAINERS:
            if cur is not None:
                out.append(cur)
                cur = None
            out.append(k)
            if (k.tail or "").strip():
                cur = lxml.html.Element("p")
                cur.text = k.tail
            continue
        if cur is None:
            cur = lxml.html.Element("p")
        cur.append(copy.deepcopy(k))  # with its tail
    if cur is not None:
        out.append(cur)
    return out


def _strip_alert_label(kids: list) -> list:
    """Drop an alert's leading inline label ('<strong>Note:</strong>', 'Important:') from its first paragraph."""
    import copy
    if not kids or kids[0].tag != "p":
        return kids
    p = copy.deepcopy(kids[0])
    first = next((k for k in p if isinstance(k.tag, str)), None)
    if not (p.text or "").strip() and first is not None and first.tag in ("strong", "b") \
            and _ALERT_LABEL.fullmatch(_norm(first.text_content())):
        p.text = (p.text or "") + (first.tail or "")
        p.remove(first)
    elif (p.text or "").strip():
        m = _ALERT_LABEL.match(p.text.lstrip())
        if m and m.group(0).endswith(":"):
            p.text = p.text.lstrip()[m.end():]
    return [p] + kids[1:]


def _gcp_alert(node):
    if node.tag != "aside":
        return None
    cls = (node.get("class") or "").split()
    kind = next((GCP_ALERTS[c] for c in cls if c in GCP_ALERTS), "note")
    return kind, _strip_alert_label(_runs(node))


def _is_tabs(node) -> bool:
    """A tab group: DocFX ``div.tabGroup``, DevSite ``devsite-selector`` or its static form ``div.ds-selector-tabs``."""
    cls = (node.get("class") or "").split()
    return node.tag == "devsite-selector" or "tabGroup" in cls or node.tag == "div" and "ds-selector-tabs" in cls


def _gcp_chrome(node) -> bool:
    if node.tag in GCP_CHROME_TAGS or node.tag == "label" and _norm(node.text_content()).lower().startswith("filter"):
        return True
    par = node.getparent()
    # the label heading of a DevSite tab: carried by the tab label, not a section
    return (bool(re.fullmatch(r"h[2-6]", node.tag)) and par is not None and par.tag == "section"
            and par.getparent() is not None and _is_tabs(par.getparent())
            and next((k for k in par if isinstance(k.tag, str)), None) is node)


def _first_body(root, unit_id):
    bodies = root.xpath('//div[@id="c1"]')
    if not bodies:
        raise ValueError(f"{unit_id}: expected an article body (div#c1)")
    h1 = bodies[0].xpath(".//h1")
    return (_norm(h1[0].text_content()) if h1 else None), bodies[0]


PROFILES = {
    "learn_html_pages": {"route": "learn_html_pages", "method": "learn_html_dom", "host": "learn.microsoft.com",
                         "site": "Microsoft Learn", "meta_keys": META_KEYS,
                         "body": _learn_body, "alert": _learn_alert, "chrome": lambda node: False},
    "aws_docs_html": {"route": "aws_docs_html", "method": "aws_html_dom", "host": "docs.aws.amazon.com",
                      "site": "AWS documentation", "meta_keys": AWS_META_KEYS,
                      "body": _aws_body, "alert": _aws_alert, "chrome": _aws_chrome},
    "gcp_docs_html": {"route": "gcp_docs_html", "method": "gcp_devsite_html_dom", "host": "docs.cloud.google.com",
                      "site": "Google Cloud documentation", "meta_keys": ("description",), "meta": _gcp_meta,
                      "body": _gcp_body, "alert": _gcp_alert, "chrome": _gcp_chrome, "containers": GCP_CONTAINERS,
                      "expect_license": r"^Creative Commons Attribution 4\.0( License)?$"},
    # first.org standards pages: one long page per document in div#c1; <h1> also marks top-level sections, and
    # anchors keep capitals ("5-1-Service-Monitoring-and-detection"). No update time or commit is published.
    "first_org_html": {"route": "first_org_html", "method": "first_org_html_dom", "host": "www.first.org",
                       "site": "FIRST", "meta_keys": ("description",), "body": _first_body,
                       "alert": lambda node: None, "chrome": lambda node: False, "h1_sections": True,
                       "anchor_pattern": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")},
}


class _Article:
    def __init__(self, unit_id: str, url: str, root, meta: dict, source_id: str, profile: dict | None = None):
        self.unit_id, self.url, self.meta, self.source_id = unit_id, url, meta, source_id
        self.profile = profile or PROFILES[ROUTE]
        title, self.body = self.profile["body"](root, unit_id)
        self.title = title or meta.get("html_title", unit_id)
        self.elements: list[dict] = []
        self.counters: dict[tuple[str, str], int] = {}
        self.unknown: list[str] = []
        self.figures = 0

    # -------------------------------------------------------------- ids, spans, elements
    def _next_id(self, parent_id: str, prefix: str) -> str:
        k = (parent_id, prefix)
        self.counters[k] = self.counters.get(k, 0) + 1
        return f"{parent_id}.{prefix}{self.counters[k]:02d}"

    def span(self, anchor: str | None) -> dict:
        return {"locator_type": "section_anchor", "page": None, "page_label": None, "url": self.url + (f"#{anchor}" if anchor else ""),
                "anchor": anchor, "unit": self.unit_id, "role": "text", "mapping_status": "native_structure"}

    def add(self, eid: str, etype: str, parent: dict | None, anchor_name: str | None, *, text: str = "", md: str = "",
            attrs: dict | None = None, anchor: str | None = None, status: str | None = None) -> dict:
        el = {"id": eid, "type": etype, "order": 0, "parent_id": parent["id"] if parent else None, "children": [],
              "anchor": anchor, "text": text, "md": md, "attrs": attrs or {},
              "source_spans": [self.span(anchor_name)],
              "mapping_status": status or ("aggregate" if etype in ("chapter", "section", "list", "admonition", "quote")
                                           else "native_structure"),
              "extraction": {"method": self.profile["method"], "route": self.profile["route"], "variables": []},
              "content_hash": _sha(text if text else json.dumps(attrs or {}, sort_keys=True, ensure_ascii=False)),
              "rights": {"state": "pending_review", "rule": "unassigned"}}
        if parent is not None:
            parent["children"].append(eid)
        self.elements.append(el)
        return el

    # -------------------------------------------------------------- inline
    def inline(self, node, *, skip_blocks: bool = False) -> tuple[str, str]:
        """(plain text, Markdown) of an element's inline content; block children are skipped when asked."""
        texts, mds = [], []

        def walk(n, top=False):
            if not top:
                tag = n.tag if isinstance(n.tag, str) else ""
                if skip_blocks and tag in BLOCK_TAGS:
                    tail(n)
                    return
                if tag == "br":
                    texts.append("\n"), mds.append("\n")
                    tail(n)
                    return
                if tag in ("img", "input", "script", "style", "button") or tag == "span" and \
                        {"material-symbols-outlined", "material-icons"} & set((n.get("class") or "").split()):
                    tail(n)  # icon-font ligatures ("segment", "more_vert") are pictures, not text
                    return
                t, m = self.inline(n)
                if t and tag in SEPARATED_TAGS:  # paragraphs, list items, headings inside a table cell: own lines
                    texts.append(f"\n{t}\n"), mds.append(f"\n{m}\n")
                    tail(n)
                    return
                if t:
                    if tag in ("strong", "b"):
                        m = f"**{m}**"
                    elif tag in ("em", "i"):
                        m = f"*{m}*"
                    elif tag == "code":
                        t = _space(n.text_content())
                        m = "`" + t.replace("`", "\\`") + "`" if "`" not in t else "``" + t + "``"
                    elif tag == "a" and n.get("href") and not n.get("href").endswith("#lightbox"):
                        href = urljoin(self.url, n.get("href").strip())
                        if urlsplit(href).scheme in ("http", "https"):  # parentheses/spaces would end the link
                            href = href.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
                            m = f"[{m}]({href})"
                    texts.append(t), mds.append(m)
                tail(n)
                return
            if n.text:
                texts.append(_space(n.text)), mds.append(md_escape(_space(n.text)))
            for ch in n:
                walk(ch)

        def tail(n):
            if n.tail:
                texts.append(_space(n.tail)), mds.append(md_escape(_space(n.tail)))

        walk(node, top=True)
        return _norm_lines("".join(texts)), _norm_lines("".join(mds))

    # -------------------------------------------------------------- blocks
    def build(self) -> dict:
        unit = self.add(self.unit_id, "chapter", None, None, anchor=self.unit_id,
                        attrs={"title": self.title, "level": 1, "number": None, "url": self.url,
                               "source_id": self.source_id, **{k: v for k, v in self.meta.items() if k != "html_title"}})
        self.add(f"{self.unit_id}.h01", "heading", unit, None, text=self.title, md=md_escape(self.title),
                 attrs={"title": self.title, "level": 1})
        stack: list[tuple[int, dict, str | None]] = [(1, unit, None)]
        # Sites that use <h1> for top-level sections after the title (first.org) shift every heading down one level.
        shift = 1 if self.profile.get("h1_sections") else 0
        heading_re = re.compile(r"h([1-6])" if shift else r"h([2-6])")
        anchor_re = self.profile.get("anchor_pattern") or _HID
        title_seen = False
        for node in self.stream(self.body):
            if isinstance(node, tuple):  # ("tab", label): tabbed content is linearised, each tab after its label
                self.tab_label(node[1], stack[-1][1], stack[-1][2])
                continue
            m = heading_re.fullmatch(node.tag)
            if m:
                title = _norm(node.text_content())
                if node.tag == "h1" and title == self.title and not title_seen:  # the title, printed in the body
                    title_seen = True
                    continue
                level = int(m.group(1)) + shift
                while stack[-1][0] >= level:
                    stack.pop()
                parent = stack[-1][1]
                if not title:
                    continue
                hid = node.get("id") or ""
                if not anchor_re.match(hid):  # no published anchor to cite: keep it in place as a bold paragraph
                    self.block(node, stack[-1][1], stack[-1][2], reason="heading without an anchor id")
                    continue
                sid = f"{self.unit_id}.{hid.lower()}"  # element IDs are lower case; the anchor keeps its case
                while any(e["id"] == sid for e in self.elements):
                    sid += "-x"
                sec = self.add(sid, "section", parent, hid, anchor=f"{self.unit_id}--{sid.split('.', 1)[1]}",
                               attrs={"title": title, "level": level, "number": None, "anchor": hid})
                _, hmd = self.inline(node)
                self.add(f"{sid}.h01", "heading", sec, hid, text=title, md=hmd, attrs={"title": title, "level": level})
                stack.append((level, sec, hid))
                continue
            self.block(node, stack[-1][1], stack[-1][2])
        return unit

    def stream(self, container):
        """The body's block nodes in reading order. Containers that hold headings (FAQ ``article``/``section``
        wrappers, tab groups) are opened so their headings become sections; tab groups yield ("tab", label) before
        each tab's content and drop their tab-list navigation."""
        for node in container:
            if not isinstance(node.tag, str) or node.tag in SKIP_TAGS or self.profile["chrome"](node):
                continue
            if (node.tag in ("div", "section", "article", "span") or node.tag in self.profile.get("containers", ())) \
                    and self.profile["alert"](node) is None and node.xpath(".//h2|.//h3|.//h4|.//h5|.//h6"):
                if self._is_tabs(node):
                    for item in self.tabs(node):
                        if isinstance(item, tuple):
                            yield item
                        else:
                            yield from self.stream(item)
                else:
                    yield from self.stream(node)
                continue
            yield node

    def expected_headings(self) -> list:
        """Headings of the article text: not empty, not in skipped page chrome (e.g. AWS "Topics" lists), not an
        alert's own title, and not in skipped tags."""
        out = []
        title_seen = False
        h1 = ".//h1|" if self.profile.get("h1_sections") else ""
        for h in self.body.xpath(h1 + ".//h2|.//h3|.//h4|.//h5|.//h6"):
            if not _norm(h.text_content()) or self.profile["chrome"](h):
                continue
            if h.tag == "h1" and _norm(h.text_content()) == self.title and not title_seen:
                title_seen = True
                continue
            n, skip = h.getparent(), False
            while n is not None and n is not self.body:
                cls = (n.get("class") or "").split()
                # a heading inside a table cell (DevSite "responsive" card tables) stays in the cell's text
                if n.tag in SKIP_TAGS or n.tag in ("td", "th") or self.profile["chrome"](n) or "awsdocs-note-title" in cls:
                    skip = True
                    break
                n = n.getparent()
            if not skip:
                out.append(h)
        return out

    _is_tabs = staticmethod(_is_tabs)

    def tabs(self, group):
        """("tab", label), panel, ("tab", label), panel, ... for a DocFX tab group or a DevSite selector (whose
        panels are labelled by their first heading, which the profile skips as chrome)."""
        if "tabGroup" not in (group.get("class") or "").split():
            for panel in group.xpath("./section"):
                h = next((k for k in panel if isinstance(k.tag, str)), None)
                is_label = h is not None and re.fullmatch(r"h[2-6]", h.tag)
                yield ("tab", _norm(h.text_content()) if is_label else "Tab")
                yield panel
            return
        labels = {a.get("data-tab"): _norm(a.text_content()) for a in group.xpath(".//a[@data-tab]")}
        for panel in group.xpath("./section"):
            key = panel.get("data-tab")
            yield ("tab", labels.get(key) or key or "Tab")
            yield panel

    def tab_label(self, label: str, parent: dict, anchor: str | None) -> None:
        self.add(self._next_id(parent["id"], "p"), "paragraph", parent, anchor, text=label, md=f"*Tab: {md_escape(label)}*",
                 attrs={"links": [], "tab_label": True})

    def block(self, node, parent: dict, anchor: str | None, reason: str | None = None) -> None:
        tag = node.tag
        cls = (node.get("class") or "").split()
        if tag in SKIP_TAGS or self.profile["chrome"](node):
            return
        alert = self.profile["alert"](node)
        if alert is not None:
            kind, kids = alert
            adm = self.add(self._next_id(parent["id"], "a"), "admonition", parent, anchor, attrs={"kind": kind})
            for k in kids:
                self.block(k, adm, anchor)
            return
        if self._is_tabs(node):
            for item in self.tabs(node):
                if isinstance(item, tuple):
                    self.tab_label(item[1], parent, anchor)
                else:
                    for k in item:
                        if isinstance(k.tag, str):
                            self.block(k, parent, anchor)
            return
        if tag == "h1" and _norm(node.text_content()) == self.title:
            return  # the article title, printed again in the body: it is the unit's heading already
        if tag == "figure":  # <figure><img><figcaption>: one figure per image, captioned by the figcaption
            cap = node.find("figcaption")
            caption = _norm(cap.text_content()) if cap is not None else None
            for img in node.xpath(".//img"):
                self.figure(img, parent, anchor, caption=caption)
            return
        if re.fullmatch(r"h[1-6]" if self.profile.get("h1_sections") else r"h[2-6]", tag):
            # a heading that cannot be a section: kept in place as a bold paragraph
            text, md = self.inline(node)
            if not text:
                return
            why = reason or f"heading inside <{node.getparent().tag}>"
            if not (tag == "h6" and reason):  # untitled-anchor h6 sub-titles (procedure titles) are routine
                self.unknown.append(f"{why} kept as a bold paragraph: {text[:60]!r}")
            self.add(self._next_id(parent["id"], "p"), "paragraph", parent, anchor, text=text, md=_bold(md),
                     attrs={"links": [], "heading_level": int(tag[1]), "heading_anchor": node.get("id")})
            return
        if tag == "p":
            imgs = node.xpath(".//img")
            text, md = self.inline(node)
            if imgs and not text:
                for img in imgs:
                    self.figure(img, parent, anchor)
                return
            if text:
                self.add(self._next_id(parent["id"], "p"), "paragraph", parent, anchor, text=text, md=md,
                         attrs={"links": []})
            for img in imgs:
                self.figure(img, parent, anchor)
            return
        if tag in ("ul", "ol"):
            self.listing(node, parent, anchor)
            return
        if tag == "dl":
            self.definitions(node, parent, anchor)
            return
        if tag == "pre":
            code = node.find("code")
            src = code if code is not None else node
            syntax = (node.get("syntax") or "").lower()  # DevSite: <pre syntax="Bash">
            lang = next((c[5:].lower() for c in (src.get("class") or "").split() if c.startswith("lang-")), None) or \
                next((c.lower() for c in (src.get("class") or "").split() if c.lower() in CODE_LANGS), None) or \
                (syntax if syntax in CODE_LANGS else None)
            text = src.text_content().rstrip("\n")
            self.add(self._next_id(parent["id"], "c"), "code", parent, anchor, text=text, md="",
                     attrs={"lang": lang, "literal": True})
            return
        if tag == "table":
            self.table(node, parent, anchor)
            return
        if tag == "blockquote":
            q = self.add(self._next_id(parent["id"], "q"), "quote", parent, anchor)
            for k in node:
                if isinstance(k.tag, str):
                    self.block(k, q, anchor)
            return
        if tag == "details":  # expandable block: its summary as a bold line, then its content
            summ = node.find("summary")
            if summ is not None:
                text, md = self.inline(summ)
                if text:
                    self.add(self._next_id(parent["id"], "p"), "paragraph", parent, anchor, text=text, md=_bold(md),
                             attrs={"links": [], "details_summary": True})
            for k in node:
                if isinstance(k.tag, str) and k is not summ:
                    self.block(k, parent, anchor)
            return
        if tag in ("div", "section", "article") or tag in self.profile.get("containers", ()) \
                or tag == "span" and node.xpath(BLOCK_XPATH):
            for k in node:
                if isinstance(k.tag, str):
                    self.block(k, parent, anchor)
            if tag == "span" and (node.text or "").strip():
                self.unknown.append(f"text directly inside a block-holding <span> not kept: {node.text.strip()[:60]!r}")
            return
        if tag in INLINE_TAGS:  # inline content directly in the article body: a paragraph
            text, md = self.inline(_wrap(node))
            if text:
                self.add(self._next_id(parent["id"], "p"), "paragraph", parent, anchor, text=text, md=md,
                         attrs={"links": []})
            return
        if tag in ("span", "a", "img") and node.xpath(".//img") or tag == "img":
            for img in ([node] if tag == "img" else node.xpath(".//img")):
                self.figure(img, parent, anchor)
            return
        text, md = self.inline(node)
        if text:
            self.unknown.append(f"<{tag} class={' '.join(cls)}> kept as text")
            self.add(self._next_id(parent["id"], "u"), "unknown_block", parent, anchor, text=text, md=md,
                     attrs={"html_tag": tag})

    def figure(self, img, parent: dict, anchor: str | None, caption: str | None = None) -> None:
        self.figures += 1
        alt = _norm(img.get("alt") or "")
        self.add(self._next_id(parent["id"], "f"), "figure", parent, anchor, text="", md="",
                 attrs={"alt": alt, "caption": caption or alt or None, "src_url": urljoin(self.url, img.get("src") or ""),
                        "captured": False, "label": None, "number": None})

    def listing(self, node, parent: dict, anchor: str | None) -> None:
        lst = self.add(self._next_id(parent["id"], "l"), "list", parent, anchor,
                       attrs={"ordered": node.tag == "ol", "style": None, "title": None,
                              "start": int(node.get("start")) if (node.get("start") or "").isdigit() else None})
        item = None
        for li in node:
            if not isinstance(li.tag, str) or li.tag in SKIP_TAGS or self.profile["chrome"](li):
                continue
            if li.tag != "li":  # malformed markup: blocks directly inside <ul>/<ol>; kept, not dropped
                if item is None:
                    text, md = self.inline(li) if li.tag == "p" else ("", "")
                    item = self.add(self._next_id(lst["id"], "i"), "list_item", lst, anchor, text=text, md=md,
                                    attrs={"checked": None, "links": [], "unwrapped_content": True})
                    if li.tag == "p":
                        continue
                self.block(li, item, anchor)
                continue
            checkbox = li.find("input") is not None or li.find("p/input") is not None
            first_p = li.find("p")
            if first_p is not None and not (li.text or "").strip():
                text, md = self.inline(first_p)
                rest = [k for k in li if isinstance(k.tag, str) and k is not first_p]
            else:
                text, md = self.inline(li, skip_blocks=True)
                rest = [k for k in li if isinstance(k.tag, str) and k.tag in BLOCK_TAGS]
            item = self.add(self._next_id(lst["id"], "i"), "list_item", lst, anchor, text=text, md=md,
                            attrs={"checked": False if checkbox else None, "links": []})
            for k in rest:
                self.block(k, item, anchor)

    def definitions(self, node, parent: dict, anchor: str | None) -> None:
        """<dl>: one list item per term (in bold), its <dd> blocks as the item's children."""
        lst = self.add(self._next_id(parent["id"], "l"), "list", parent, anchor,
                       attrs={"ordered": False, "style": "definitions", "start": None, "title": None})
        item = None
        for k in node:
            if not isinstance(k.tag, str):
                continue
            if k.tag == "dt":
                text, md = self.inline(k)
                item = self.add(self._next_id(lst["id"], "i"), "list_item", lst, anchor, text=text,
                                md=_bold(md), attrs={"checked": None, "links": [], "term": True})
            elif k.tag == "dd":
                if item is None:
                    item = self.add(self._next_id(lst["id"], "i"), "list_item", lst, anchor, text="", md="",
                                    attrs={"checked": None, "links": []})
                kids = [c for c in k if isinstance(c.tag, str)]
                if not kids and (k.text or "").strip():
                    text, md = self.inline(k)
                    self.add(self._next_id(item["id"], "p"), "paragraph", item, anchor, text=text, md=md, attrs={"links": []})
                for c in kids:
                    self.block(c, item, anchor)

    def table(self, node, parent: dict, anchor: str | None) -> None:
        rows = []
        for tr in node.xpath("./thead/tr|./tbody/tr|./tfoot/tr|./tr"):
            sect = tr.getparent().tag if tr.getparent().tag in ("thead", "tbody", "tfoot") else "tbody"
            cells = []
            for c in tr:
                if not isinstance(c.tag, str) or c.tag not in ("td", "th"):
                    continue
                t, m = self.inline(c)
                cells.append({"text": t, "md": m.replace("\n", "<br>"), "header": c.tag == "th" or sect == "thead",
                              "colspan": int(c.get("colspan") or 1), "rowspan": int(c.get("rowspan") or 1),
                              "halign": None})
            rows.append({"section": sect, "cells": cells, "source_spans": [self.span(anchor)], "verification": "native"})
        widths = {sum(c["colspan"] for c in r["cells"]) for r in rows}
        complex_ = len(widths) > 1 or any(c["rowspan"] > 1 for r in rows for c in r["cells"])
        text = "\n".join(" | ".join(c["text"] for c in r["cells"]) for r in rows)
        self.add(self._next_id(parent["id"], "t"), "table", parent, anchor, text=text, md="",
                 attrs={"rows": rows, "complex": complex_, "caption": None, "label": None, "number": None})


def run(articles: list[dict], recipe: dict, meta: dict, log=print) -> dict:
    """``articles``: resolved lock records (hash-verified) with ``unit_id``, ``url`` and ``path``."""
    route = recipe.get("route", ROUTE)
    prof = PROFILES[route]
    elements: list[dict] = []
    root = {"id": "doc", "type": "document", "order": 0, "parent_id": None, "children": [], "anchor": None, "text": "",
            "md": "", "attrs": {}, "source_spans": [], "mapping_status": "aggregate", "extraction": None,
            "content_hash": _sha(""), "rights": {"state": "pending_review", "rule": "unassigned"}}
    elements.append(root)
    units, issues = [], []
    for a in articles:
        html = Path(a["path"]).read_text(encoding="utf-8")
        pm = meta_for(html, prof)
        art = _Article(a["unit_id"], a["url"], lxml.html.fromstring(html), pm, a["source_id"], prof)
        unit = art.build()
        unit["parent_id"] = "doc"
        root["children"].append(unit["id"])
        elements.extend(art.elements)
        for u in art.unknown:
            issues.append({"severity": "minor", "kind": "unmapped_html", "location": a["unit_id"], "message": u})
        if prof.get("expect_license") and not re.search(prof["expect_license"], pm.get("page_license") or ""):
            issues.append({"severity": "major", "kind": "page_license_unexpected", "location": a["unit_id"],
                           "message": f"page footer license is {pm.get('page_license')!r}, not the site's usual "
                                      "license: review this page's rights before distributing it"})
        heads = art.expected_headings()
        inline_heads = sum(1 for e in art.elements if e["attrs"].get("heading_level"))
        secs = [e for e in art.elements if e["type"] == "section"]
        if len(heads) != len(secs) + inline_heads:
            issues.append({"severity": "major", "kind": "section_count_mismatch", "location": a["unit_id"],
                           "message": f"{len(heads)} headings in the article body but {len(secs)} sections built "
                                      f"and {inline_heads} kept as bold paragraphs"})
        body_text = _norm(art.body.text_content())
        units.append({
            "unit_id": a["unit_id"], "title": art.title, "url": a["url"], "source_id": a["source_id"],
            "sha256": a["sha256"], "http": a.get("http"), "retrieved_at": a.get("retrieved_at"),
            **{k: pm.get(k) for k in ("git_commit_id", "updated_at", "ms_date", "document_id", "source_path",
                                      "original_content_git_url", "ms_topic", "ms_service", "description",
                                      "product", "guide", "page_license")},
            "body_text_sha256": _sha(body_text), "body_chars": len(body_text),
            "sections": [{"element_id": s["id"], "anchor": s["attrs"]["anchor"], "title": s["attrs"]["title"],
                          "level": s["attrs"]["level"]} for s in secs],
            "elements": len(art.elements), "figures_not_captured": art.figures,
        })
        log(f"  {a['unit_id']}: {len(art.elements)} elements, {len(secs)} sections, commit {str(pm.get('git_commit_id'))[:7]}")
    for n, e in enumerate(elements):
        e["order"] = n
    document = {
        "schema_version": "1.0.0", "document_id": meta["document_id"], "pack_id": meta["pack_id"],
        "pack_version": meta["pack_version"], "edition_id": meta["edition_id"], "title": meta["title"],
        "authors": meta["authors"], "edition_statement": meta["edition_statement"], "route": route,
        "sources": meta["sources"], "coordinate_convention": "not_applicable",
        "page_numbering": {"physical": "not paginated", "printed": "not paginated; locators are article URLs with section anchors"},
        "root_id": "doc", "page_labels": [], "element_count": len(elements), "elements": elements,
    }
    source_map = {"schema_version": "1.0.0", "document_id": meta["document_id"], "route": route,
                  "snapshot": recipe["inputs"]["snapshot"], "units": units}
    return {"document": document, "source_map": source_map, "issues": issues,
            "qa": {"route-summary.json": {"articles": len(units), "elements": len(elements),
                                          "sections": sum(len(u["sections"]) for u in units),
                                          "figures_not_captured": sum(u["figures_not_captured"] for u in units),
                                          "unmapped_html": sum(1 for i in issues if i["kind"] == "unmapped_html")}},
            "conversion_changes": [
                "Article text, headings, lists, code, tables and alerts are taken from the published HTML's DOM; "
                "whitespace inside text is collapsed (code is kept verbatim).",
                "Relative links are made absolute against the article URL; in-page lightbox links on images are dropped.",
                "Alert labels ('Note', 'Important', ...) are carried by the admonition kind rather than as text.",
                "Tabbed content is linearised: each tab's content follows a 'Tab: <label>' line; the tab bar is dropped.",
                "A heading inside a list or table cell is kept in place as a bold paragraph (not a section).",
                "Images are not captured: each figure keeps its alt text and the absolute URL of the published image.",
                "Navigation, feedback widgets, the 'In this article' list and page chrome outside the article body are "
                "not part of the converted text.",
            ]}
