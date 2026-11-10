"""Align structured-candidate text units to the PDF character stream.

The PDF is the authority.  Each alignment unit (a paragraph, list item, table
row, caption, code block, footnote, ...) from the structured candidate is
split into *literal* parts and *variable* parts:

* literal parts must appear in the PDF body text (running furniture removed),
  compared on a case-folded alphanumeric key;
* variable parts are inline items whose rendering differs by output format in
  Asciidoctor (footnote reference labels and cross-reference text).  Their
  wording is taken from the PDF and checked against the expected rendering
  (the per-chapter footnote label, or the cross-reference target's title).

Placement first selects a globally monotonic chain of exact anchors (weighted
longest increasing subsequence) and then fills the remaining units only
between already placed neighbours, so a spurious far-away match cannot move
the alignment.

Unit statuses: ``exact`` (all literal parts located in order), ``fuzzy``
(anchored similarity >= threshold; the structured text is NOT accepted and the
PDF text is used), ``unmatched``, ``no_text``.
"""

from __future__ import annotations

import bisect
import difflib
import re
from dataclasses import dataclass, field

from .adochtml import FN_CLOSE, FN_OPEN, XR_CLOSE, XR_OPEN, XR_SEP
from .pdfsource import char_in_bbox
from .util import nfkc, text_key, visible_key

LONG_UNIT = 30           # key chars; literal parts at least this long can anchor
MAX_VAR_GAP = 200        # max key chars a variable part may occupy in the PDF
FUZZY_MIN_RATIO = 0.90

_PART_RE = re.compile(f"({re.escape(FN_OPEN)}.*?{re.escape(FN_CLOSE)}|{re.escape(XR_OPEN)}.*?{re.escape(XR_CLOSE)})", re.S)


@dataclass
class Stream:
    """PDF body text as key characters with back-references to page chars."""

    key: str
    key_src: list[tuple[int, int]]          # (page, char index on page)
    vis: str
    vis_src: list[tuple[int, int]]
    key_to_vis: list[int]
    page_key_start: dict[int, int]          # first key index of each page
    furniture_chars: dict[int, set[int]] = field(default_factory=dict)
    skey: str = ""                          # search key: key with masked (PDF-only) pages blanked

    def __post_init__(self):
        if not self.skey:
            self.skey = self.key

    def page_of(self, kpos: int) -> int:
        return self.key_src[min(kpos, len(self.key_src) - 1)][0]


def build_stream(pages: list[dict], furniture: dict[int, list[dict]], masked_pages: set[int] | None = None) -> Stream:
    """``masked_pages``: pages whose text must never receive structured-candidate matches (e.g. the
    printed table of contents and the index, which repeat headings verbatim)."""
    key_chars: list[str] = []
    key_src: list[tuple[int, int]] = []
    vis_chars: list[str] = []
    vis_src: list[tuple[int, int]] = []
    key_to_vis: list[int] = []
    page_start: dict[int, int] = {}
    furn_chars: dict[int, set[int]] = {}
    for p in pages:
        pno = p["page"]
        page_start[pno] = len(key_chars)
        fl = furniture.get(pno, [])
        fset = set()
        for i, ch in enumerate(p["chars"]):
            if fl and any(char_in_bbox(ch, f["bbox"]) for f in fl):
                fset.add(i)
                continue
            for c in nfkc(ch["text"]).casefold():
                if c.isspace():
                    continue
                vis_chars.append(c)
                vis_src.append((pno, i))
                if c.isalnum():
                    key_chars.append(c)
                    key_src.append((pno, i))
                    key_to_vis.append(len(vis_chars) - 1)
        if fset:
            furn_chars[pno] = fset
    key = "".join(key_chars)
    skey = key
    if masked_pages:
        skey = "".join("\x00" if key_src[i][0] in masked_pages else ch for i, ch in enumerate(key_chars))
    return Stream(key, key_src, "".join(vis_chars), vis_src, key_to_vis, page_start, furn_chars, skey)


# ---------------------------------------------------------------------------
# Units and parts


@dataclass
class Part:
    kind: str                 # lit | fn | xr
    text: str                 # literal text, footnote number (str) or HTML xref text
    key: str = ""
    start: int | None = None  # key index range in the stream once placed
    end: int | None = None
    # variable-part resolution
    expected: list[str] = field(default_factory=list)
    resolved: str | None = None
    resolution: str | None = None   # html_same | pdf_title | pdf_label | pdf_region | unresolved


@dataclass
class Unit:
    uid: str                  # element id + "#" + unit index
    element_id: str
    text: str                 # text with variable-part markers
    parts: list[Part]
    group: str = "body"       # body | footnote
    scope: tuple[int, int] | None = None
    status: str = "pending"
    start: int | None = None
    end: int | None = None
    ratio: float | None = None
    visible_equal: bool | None = None
    visible_diff: list | None = None
    note: str | None = None

    @property
    def key(self) -> str:
        return "".join(p.key for p in self.parts if p.kind == "lit")

    @property
    def lits(self) -> list[Part]:
        return [p for p in self.parts if p.kind == "lit" and p.key]

    @property
    def has_vars(self) -> bool:
        return any(p.kind != "lit" for p in self.parts)


def split_parts(text: str) -> list[Part]:
    parts: list[Part] = []
    for piece in _PART_RE.split(text):
        if not piece:
            continue
        if piece.startswith(FN_OPEN):
            parts.append(Part("fn", piece[1:-1]))
        elif piece.startswith(XR_OPEN):
            label, _, target = piece[1:-1].partition(XR_SEP)
            parts.append(Part("xr", label, key="", expected=[target]))
        elif parts and parts[-1].kind == "lit":
            parts[-1].text += piece
            parts[-1].key = text_key(parts[-1].text)
        else:
            parts.append(Part("lit", piece, key=text_key(piece)))
    return parts


def make_units(element_id: str, texts: list[str], group: str = "body",
               scope: tuple[int, int] | None = None) -> list[Unit]:
    return [Unit(f"{element_id}#{i}", element_id, t, split_parts(t), group, scope) for i, t in enumerate(texts)]


# ---------------------------------------------------------------------------
# Placement


def _place_lits(stream: Stream, u: Unit, lo: int, hi: int, anchor: tuple[int, int] | None = None) -> bool:
    """Locate every literal part of ``u`` in order inside [lo, hi).

    ``anchor`` = (index into u.lits, stream position) fixes one literal part.
    Consecutive literal parts are separated by >= 1 variable part, so each
    subsequent literal may start at most MAX_VAR_GAP key chars after the
    previous one ends.
    """
    lits = u.lits
    if not lits:
        return False
    if anchor is None:
        ai = max(range(len(lits)), key=lambda i: len(lits[i].key))
        pos = stream.skey.find(lits[ai].key, lo, hi)
        if pos < 0:
            return False
    else:
        ai, pos = anchor
    placed = {ai: (pos, pos + len(lits[ai].key))}
    # forward
    prev_end = placed[ai][1]
    for i in range(ai + 1, len(lits)):
        k = lits[i].key
        p = stream.skey.find(k, prev_end, min(len(stream.key), prev_end + MAX_VAR_GAP + len(k)))
        if p < 0:
            return False
        placed[i] = (p, p + len(k))
        prev_end = p + len(k)
    # backward
    next_start = placed[ai][0]
    for i in range(ai - 1, -1, -1):
        k = lits[i].key
        p = stream.skey.rfind(k, max(0, next_start - MAX_VAR_GAP - len(k)), next_start)
        if p < 0:
            return False
        placed[i] = (p, p + len(k))
        next_start = p
    for i, lp in enumerate(lits):
        lp.start, lp.end = placed[i]
    u.start = lits[0].start
    u.end = lits[-1].end
    return True


def _fuzzy(stream: Stream, u: Unit, lo: int, hi: int) -> bool:
    k = u.key
    m = len(k)
    if m < 16 or hi <= lo:
        return False
    anchors = [(0, k[:16]), (m // 2 - 8, k[m // 2 - 8: m // 2 + 8]), (m - 16, k[-16:])]
    for off, a in anchors:
        pos = stream.skey.find(a, lo, hi)
        if pos < 0:
            continue
        cand_start = max(lo, pos - off - 8)
        cand_end = min(len(stream.key), hi + 64, cand_start + int(m * 1.15) + 24)
        seg = stream.skey[cand_start:cand_end]
        sm = difflib.SequenceMatcher(None, k, seg, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size]
        if not blocks:
            continue
        s = cand_start + blocks[0].b
        e = cand_start + blocks[-1].b + blocks[-1].size
        ratio = difflib.SequenceMatcher(None, k, stream.skey[s:e], autojunk=False).ratio()
        if ratio >= FUZZY_MIN_RATIO:
            u.status, u.start, u.end, u.ratio = "fuzzy", s, e, round(ratio, 4)
            u.note = "anchored fuzzy match; structured text not accepted for this unit"
            return True
    return False


def _place(stream: Stream, u: Unit, lo: int, hi: int) -> bool:
    if _place_lits(stream, u, lo, hi):
        u.status, u.ratio = "exact", 1.0
        return True
    return _fuzzy(stream, u, lo, hi)


class _Fenwick:
    """Prefix-maximum Fenwick tree over compressed positions: (weight, payload)."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.t: list[tuple[int, int]] = [(0, -1)] * (n + 1)

    def update(self, i: int, val: tuple[int, int]) -> None:
        i += 1
        while i <= self.n:
            if val[0] > self.t[i][0]:
                self.t[i] = val
            i += i & -i

    def query(self, i: int) -> tuple[int, int]:  # max over ranks [0, i)
        best = (0, -1)
        while i > 0:
            # ">=" while walking towards lower ranks: on equal weight prefer the
            # earlier stream position (e.g. a table header's first printing rather
            # than its repetition on a continuation page).
            if self.t[i][0] >= best[0] and self.t[i][1] != -1:
                best = self.t[i]
            i -= i & -i
        return best


def _occurrences(key: str, needle: str, limit: int = 6) -> list[int]:
    out, s = [], 0
    while len(out) < limit:
        p = key.find(needle, s)
        if p < 0:
            break
        out.append(p)
        s = p + 1
    return out


def _anchor_chain(stream: Stream, body: list[Unit]) -> dict[int, tuple[int, int]]:
    """Weighted LIS over exact occurrences of each unit's longest literal part.

    Returns {unit index: (lit index, stream position)} for the chosen chain.
    """
    cands: list[tuple[int, int, int, int]] = []  # (unit index, lit index, start, end)
    for i, u in enumerate(body):
        if u.status != "pending" or not u.lits:
            continue
        li = max(range(len(u.lits)), key=lambda j: len(u.lits[j].key))
        k = u.lits[li].key
        if len(k) < LONG_UNIT:
            continue
        for p in _occurrences(stream.skey, k):
            cands.append((i, li, p, p + len(k)))
    if not cands:
        return {}
    ends = sorted({c[3] for c in cands})
    rank = {e: r for r, e in enumerate(ends)}
    fw = _Fenwick(len(ends))
    best: list[tuple[int, int]] = [(0, -1)] * len(cands)
    by_unit: dict[int, list[int]] = {}
    for ci, c in enumerate(cands):
        by_unit.setdefault(c[0], []).append(ci)
    for ui in sorted(by_unit):
        cis = by_unit[ui]
        for ci in cis:
            _, _, s, e = cands[ci]
            w, prev = fw.query(bisect.bisect_right(ends, s))
            best[ci] = (w + (e - s), prev)
        for ci in cis:  # update after all of this unit's candidates: no self-chaining
            fw.update(rank[cands[ci][3]], (best[ci][0], ci))
    ci = max(range(len(cands)), key=lambda k: best[k][0])
    chain: dict[int, tuple[int, int]] = {}
    while ci != -1:
        ui, li, s, _ = cands[ci]
        chain[ui] = (li, s)
        ci = best[ci][1]
    return chain


def _neighbours(body: list[Unit], i: int) -> tuple[int, int | None]:
    prev = next((body[k] for k in range(i - 1, -1, -1) if body[k].start is not None), None)
    nxt = next((body[k] for k in range(i + 1, len(body)) if body[k].start is not None), None)
    return (prev.end if prev is not None else 0), (nxt.start if nxt is not None else None)


def align_units(stream: Stream, units: list[Unit], xref_titles: dict[str, list[str]] | None = None) -> None:
    """Place body units (anchor chain, then gap filling) and resolve variables."""
    body = [u for u in units if u.group == "body"]
    for u in body:
        if not u.key and not u.has_vars:
            u.status = "no_text"
    chain = _anchor_chain(stream, body)
    # Place chained units fully around their anchor; a unit whose other literal
    # parts cannot be placed around the anchor drops out of the chain.
    for ui, (li, pos) in sorted(chain.items()):
        u = body[ui]
        if _place_lits(stream, u, 0, len(stream.key), anchor=(li, pos)):
            u.status, u.ratio, u.note = "exact", 1.0, "anchor"
        else:
            u.start = u.end = None
    # Fill gaps.
    for i, u in enumerate(body):
        if u.status != "pending":
            continue
        lo, hi = _neighbours(body, i)
        hi = len(stream.key) if hi is None else hi
        if u.key and _place(stream, u, lo, max(lo, hi)):
            continue
        if u.key and _place(stream, u, lo, min(len(stream.key), max(lo, hi) + 400)):
            u.note = (u.note or "") + "; placed past next neighbour start"
            continue
        if not u.key:  # variable-only unit (e.g. a paragraph that is only an xref)
            u.status = "var_only"
            u.start, u.end = lo, lo
            continue
        u.status = "unmatched"
        u.note = "not found between neighbouring placed units"
    _resolve_variables(stream, body, xref_titles or {})
    for u in body:
        if u.status in ("exact", "fuzzy"):
            _visible_compare(stream, u)


def align_scoped(stream: Stream, units: list[Unit], xref_titles: dict[str, list[str]] | None = None) -> None:
    """Align units (e.g. footnote definitions) inside their own key-index scope."""
    for u in units:
        if not u.key:
            u.status = "no_text"
            continue
        lo, hi = u.scope or (0, len(stream.key))
        if _place(stream, u, lo, hi):
            u.note = "found in scope"
        elif _place(stream, u, 0, len(stream.key)):
            u.note = "found outside expected scope"
        else:
            u.status = "unmatched"
            u.note = "not found"
    _resolve_variables(stream, units, xref_titles or {}, scoped=True)
    for u in units:
        if u.status in ("exact", "fuzzy"):
            _visible_compare(stream, u)


# ---------------------------------------------------------------------------
# Variable parts


def pdf_text_between(stream: Stream, pages_by_no: dict[int, dict], a: int, b: int) -> str:
    """PDF text (with spacing) covering key positions [a, b)."""
    if a is None or b is None or b <= a:
        return ""
    out = []
    (p0, c0), (p1, c1) = stream.key_src[a], stream.key_src[b - 1]
    for pno in range(p0, p1 + 1):
        page = pages_by_no.get(pno)
        if page is None:
            continue
        chars = page["chars"]
        lo = c0 if pno == p0 else 0
        hi = c1 if pno == p1 else len(chars) - 1
        furn = stream.furniture_chars.get(pno, set())
        prev = None
        for i in range(lo, hi + 1):
            if i in furn:
                continue
            ch = chars[i]
            if prev is not None and ch["text"] != " " and not out[-1].endswith(" "):
                # new visual line or a word gap without an explicit space char
                if ch["top"] > prev["bottom"] - 1 or ch["x0"] - prev["x1"] > 0.25 * max(ch["size"], 1):
                    out.append(" ")
            out.append(ch["text"])
            prev = ch
        if pno != p1:
            out.append(" ")
    return re.sub(r"\s+", " ", "".join(out)).strip()


_PAGES_BY_NO: dict[int, dict] = {}


def set_pages(pages: list[dict]) -> None:
    """Register page records for text reconstruction during resolution."""
    _PAGES_BY_NO.clear()
    _PAGES_BY_NO.update({p["page"]: p for p in pages})


def _chars_range(stream: Stream, a: int, b: int, post: int = 0, pre: int = 0) -> list[tuple[str, int | None]]:
    """Body characters from key position a to b-1 (inclusive) as (char, key_index).

    ``post``/``pre`` add up to that many non-key, non-space characters after/before
    the range (e.g. a closing bracket).  Furniture characters are skipped.
    """
    if a is None or b is None or b <= a:
        return []
    key_at = {stream.key_src[k]: k for k in range(a, b)}
    (p0, c0), (p1, c1) = stream.key_src[a], stream.key_src[b - 1]
    out: list[tuple[str, int | None]] = []
    for pno in range(p0, p1 + 1):
        page = _PAGES_BY_NO.get(pno)
        if page is None:
            continue
        chars = page["chars"]
        furn = stream.furniture_chars.get(pno, set())
        lo = c0 if pno == p0 else 0
        hi = c1 if pno == p1 else len(chars) - 1
        if pno == p0 and pre:
            j, extra = lo - 1, []
            while j >= 0 and len(extra) < pre:
                ch = chars[j]["text"]
                if j in furn or ch.isalnum():
                    break
                if not ch.isspace():
                    extra.append((ch, None))
                j -= 1
            out.extend(reversed(extra))
        prev = None
        for i in range(lo, hi + 1):
            if i in furn:
                continue
            ch = chars[i]
            if prev is not None and ch["text"] != " " and out and out[-1][0] != " ":
                if ch["top"] > prev["bottom"] - 1 or ch["x0"] - prev["x1"] > 0.25 * max(ch["size"], 1):
                    out.append((" ", None))
            k = key_at.get((pno, i))
            for c in nfkc(ch["text"]):
                out.append((c, k if c.isalnum() else None))
            prev = ch
        if pno == p1 and post:
            n = 0
            for i in range(hi + 1, len(chars)):
                ch = chars[i]["text"]
                if i in furn or ch.isalnum() or n >= post:
                    break
                if not ch.isspace():
                    out.append((ch, None))
                    n += 1
        if pno != p1:
            out.append((" ", None))
    return out


def _skip_space(T, i):
    while i < len(T) and T[i][0].isspace():
        i += 1
    return i


def _match_key(T, i, ck: str) -> int | None:
    """If the key chars of T starting at i spell ``ck`` return the end index."""
    j, n = i, 0
    while j < len(T) and n < len(ck):
        c = T[j][0]
        if c.isalnum():
            if c.casefold() != ck[n]:
                return None
            n += 1
        j += 1
    return j if n == len(ck) else None


def _text_of(T, i, j) -> str:
    return re.sub(r"\s+", " ", "".join(c for c, _ in T[i:j])).strip()


def _keypos(T, i, j):
    ks = [k for _, k in T[i:j] if k is not None]
    return (min(ks), max(ks) + 1) if ks else (None, None)


_PUNCT_EQ = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def _parse_run(run: list[Part], T, xref_titles, interior: bool) -> tuple[int, list[str]]:
    """Consume ``run`` (variable parts and key-less literals) against T."""
    i = 0
    issues: list[str] = []
    for pi, p in enumerate(run):
        i = _skip_space(T, i)
        if p.kind == "lit":
            for c in p.text:
                if c.isspace():
                    continue
                i = _skip_space(T, i)
                if i < len(T) and T[i][0].translate(_PUNCT_EQ) == c.translate(_PUNCT_EQ):
                    i += 1
            continue
        if p.kind == "fn":
            j = i
            if j < len(T) and T[j][0] == "[":
                j += 1
            k0 = j
            while j < len(T) and T[j][0].isdigit():
                j += 1
            if j > k0:
                p.resolved, p.resolution = "".join(c for c, _ in T[k0:j]), "pdf_label"
                p.start, p.end = _keypos(T, k0, j)
                if j < len(T) and T[j][0] == "]":
                    j += 1
                i = j
            else:
                p.resolution = "unresolved"
                issues.append(f"footnote {p.text}: no label in PDF region")
            continue
        # cross reference
        target = p.expected[0] if p.expected else ""
        cands = list(dict.fromkeys([p.text] + list(xref_titles.get(target, []))))
        done = False
        for c in cands:
            ck = text_key(c)
            if not ck:
                continue
            j = _match_key(T, i, ck)
            if j is not None:
                p.resolved = _text_of(T, i, j)
                p.resolution = "html_same" if text_key(p.resolved) == text_key(p.text) else "pdf_title"
                p.start, p.end = _keypos(T, i, j)
                i, done = j, True
                break
        if done:
            continue
        # Not an expected rendering: take the PDF text up to the next delimiter.
        nxt = run[pi + 1] if pi + 1 < len(run) else None
        delim = None
        if nxt is not None and nxt.kind == "lit":
            delim = next((c for c in nxt.text if not c.isspace()), None)
        elif nxt is not None and nxt.kind == "fn":
            delim = "["
        if delim is not None:
            j = i
            while j < len(T) and T[j][0] != delim:
                j += 1
            if j < len(T) and j > i:
                p.resolved, p.resolution = _text_of(T, i, j), "pdf_region"
                p.start, p.end = _keypos(T, i, j)
                i = j
                continue
        elif interior:
            j = len(T)
            p.resolved, p.resolution = _text_of(T, i, j), "pdf_region"
            p.start, p.end = _keypos(T, i, j)
            i = j
            continue
        p.resolution = "unresolved"
        issues.append(f"xref {p.text!r}: PDF wording not determined")
    if interior:
        rest = [c for c, k in T[i:] if k is not None]
        if rest:
            issues.append("unconsumed PDF text in interior variable run: " + "".join(rest)[:60])
    return i, issues


def _resolve_variables(stream: Stream, units: list[Unit], xref_titles: dict[str, list[str]],
                       scoped: bool = False) -> None:
    placed = sorted((u for u in units if u.start is not None), key=lambda u: (u.start, u.end))
    starts = [u.start for u in placed]
    for u in units:
        if not u.has_vars:
            continue
        if u.status != "exact":
            for p in u.parts:
                if p.kind != "lit":
                    p.resolution = "unresolved"
            continue
        j = bisect.bisect_right(starts, u.start)
        next_start = len(stream.key)
        while j < len(placed):
            if placed[j] is not u and placed[j].start >= u.end:
                next_start = placed[j].start
                break
            j += 1
        prev_end = 0
        for x in placed:
            if x is u or x.end is None:
                continue
            if x.end <= u.start:
                prev_end = max(prev_end, x.end)
        runs, cur, before = [], [], None
        for p in u.parts:
            if p.kind == "lit" and p.key:
                if cur:
                    runs.append((before, cur, p))
                    cur = []
                before = p
            else:
                cur.append(p)
        if cur:
            runs.append((before, cur, None))
        issues = []
        for before, run, after in runs:
            if all(p.kind == "lit" for p in run):
                continue
            if before is not None and after is not None:
                T = _chars_range(stream, before.end, after.start) if after.start > before.end else []
                _, iss = _parse_run(run, T, xref_titles, interior=True)
            elif before is not None:  # trailing run
                b = min(next_start, before.end + MAX_VAR_GAP)
                T = _chars_range(stream, before.end, b, post=2) if b > before.end else []
                _, iss = _parse_run(run, T, xref_titles, interior=False)
            else:  # leading run: parse the reversed problem by trying suffix windows
                a = max(prev_end, after.start - MAX_VAR_GAP)
                T = _chars_range(stream, a, after.start, pre=2) if after.start > a else []
                best = None
                for st in range(len(T)):
                    if st and T[st - 1][0] not in " [(" and T[st][0] != "[":
                        continue
                    saved = [(p.resolved, p.resolution, p.start, p.end) for p in run]
                    end_i, iss = _parse_run(run, T[st:], xref_titles, interior=False)
                    ok = all(p.resolution not in (None, "unresolved") for p in run if p.kind != "lit")
                    tail_keys = [k for _, k in T[st + end_i:] if k is not None]
                    if ok and not tail_keys:
                        best = iss
                        break
                    for p, sv in zip(run, saved):
                        p.resolved, p.resolution, p.start, p.end = sv
                if best is None:
                    for p in run:
                        if p.kind != "lit":
                            p.resolution, p.resolved = "unresolved", None
                    iss = ["leading variable run not resolved"]
                else:
                    iss = best
            issues.extend(iss)
        if issues:
            u.note = ((u.note + "; ") if u.note else "") + "; ".join(issues)
        spans = [(p.start, p.end) for p in u.parts if p.start is not None]
        if spans:
            u.start = min(s for s, _ in spans)
            u.end = max(e for _, e in spans)


def resolved_text(u: Unit) -> str:
    """Unit text with variables replaced by their PDF wording (markers kept)."""
    out = []
    for p in u.parts:
        if p.kind == "lit":
            out.append(p.text)
        elif p.kind == "fn":
            out.append(FN_OPEN + (p.resolved or ("?" + p.text)) + FN_CLOSE)
        else:
            out.append(XR_OPEN + (p.resolved if p.resolved is not None else p.text) + XR_SEP
                       + (p.expected[0] if p.expected else "") + XR_CLOSE)
    return "".join(out)


# ---------------------------------------------------------------------------
# Punctuation-sensitive comparison

_TYPO = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                       "—": "-", " ": " ", "−": "-"})
_MARK_RE = re.compile(f"[{re.escape(FN_OPEN + FN_CLOSE + XR_OPEN + XR_CLOSE + XR_SEP)}]")


def expected_visible(u: Unit) -> str:
    out = []
    for p in u.parts:
        if p.kind == "lit":
            out.append(p.text)
        elif p.kind == "fn":
            out.append(f"[{p.resolved or p.text}]")
        else:
            out.append(p.resolved if p.resolved is not None else p.text)
    return visible_key(_MARK_RE.sub("", "".join(out))).translate(_TYPO)


def _visible_compare(stream: Stream, u: Unit) -> None:
    exp = expected_visible(u)
    lead = len(exp) - len(exp.lstrip("".join(c for c in set(exp) if not c.isalnum())))
    trail = len(exp) - len(exp.rstrip("".join(c for c in set(exp) if not c.isalnum())))
    vs = stream.key_to_vis[u.start]
    ve = stream.key_to_vis[u.end - 1] + 1
    # extend over adjacent non-alphanumeric PDF characters only (never into the next block's text)
    k = 0
    while k < lead and vs > 0 and not stream.vis[vs - 1].isalnum():
        vs -= 1
        k += 1
    k = 0
    while k < trail and ve < len(stream.vis) and not stream.vis[ve].isalnum():
        ve += 1
        k += 1
    pdf_vis = stream.vis[vs:ve].translate(_TYPO)
    if pdf_vis == exp:
        u.visible_equal = True
        return
    u.visible_equal = False
    sm = difflib.SequenceMatcher(None, exp, pdf_vis, autojunk=False)
    diffs = []
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag == "equal":
            continue
        diffs.append([tag, exp[a0:a1][:40], pdf_vis[b0:b1][:40], exp[max(0, a0 - 12):a1 + 12][:64]])
        if len(diffs) >= 12:
            break
    u.visible_diff = diffs


# ---------------------------------------------------------------------------
# Spans and coverage


def unit_spans(stream: Stream, pages_by_no: dict[int, dict], start: int, end: int) -> list[dict]:
    """Per-page source spans (bbox of matched characters) for key range [start, end)."""
    if start is None or end is None or end <= start:
        return []
    by_page: dict[int, list[int]] = {}
    for k in range(start, end):
        pno, ci = stream.key_src[k]
        by_page.setdefault(pno, []).append(ci)
    spans = []
    for pno in sorted(by_page):
        p = pages_by_no[pno]
        chs = [p["chars"][i] for i in by_page[pno]]
        spans.append({
            "page": pno,
            "page_label": p["label"],
            "bbox": [round(min(c["x0"] for c in chs), 2), round(min(c["top"] for c in chs), 2),
                     round(max(c["x1"] for c in chs), 2), round(max(c["bottom"] for c in chs), 2)],
            "n_key_chars": len(by_page[pno]),
        })
    return spans


def coverage(stream: Stream, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return uncovered key-index intervals [start, end) given covered ranges."""
    covered = bytearray(len(stream.key))
    for a, b in ranges:
        if a is not None and b is not None and b > a:
            covered[a:b] = b"\x01" * (b - a)
    gaps = []
    i, n = 0, len(covered)
    while i < n:
        if covered[i]:
            i += 1
            continue
        j = i
        while j < n and not covered[j]:
            j += 1
        gaps.append((i, j))
        i = j
    return gaps


def covered_ranges(units: list[Unit]) -> list[tuple[int, int]]:
    out = []
    for u in units:
        if u.status == "fuzzy":
            out.append((u.start, u.end))
        elif u.status == "exact":
            for p in u.parts:
                if p.start is not None:
                    out.append((p.start, p.end))
    return out
