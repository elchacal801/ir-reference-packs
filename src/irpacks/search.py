"""Local keyword search over pack chunk corpora (SQLite FTS5, BM25).

The index contains only chunk records (one designated narrative corpus per
pack plus explicitly classified excluded corpora, filtered at query time).
Full-book and chapter Markdown are never indexed, so passages are not
duplicated.  Search is read-only and never interprets or executes passage
content.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

FTS_TOKENIZER = "porter unicode61 remove_diacritics 2"
INDEX_SCHEMA_VERSION = "1"

COLUMNS = [
    ("chunk_id", "TEXT PRIMARY KEY"), ("pack_id", "TEXT"), ("pack_version", "TEXT"), ("document_id", "TEXT"),
    ("edition_id", "TEXT"), ("variant", "TEXT"), ("corpus", "TEXT"), ("include_default", "INTEGER"),
    ("exclusion_reason", "TEXT"), ("content_type", "TEXT"), ("authority_class", "TEXT"), ("review_status", "TEXT"),
    ("superseded", "INTEGER"), ("rights_state", "TEXT"), ("distribution_policy", "TEXT"), ("applicability", "TEXT"),
    ("heading_path", "TEXT"), ("citation", "TEXT"), ("text", "TEXT"), ("token_count", "INTEGER"),
    ("fragment_of", "TEXT"), ("overlap_from", "TEXT"), ("ord", "INTEGER"), ("record", "TEXT"),
]


def fts5_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.close()
        return True
    except sqlite3.OperationalError:
        return False


_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:https?|mailto):[^)\s]*\)")
_URL = re.compile(r"<https?://[^>\s]*>|https?://[^\s)\]>]+")


def plain_text(md: str) -> str:
    """Chunk text as indexed (index_text "plain"): link targets and bare URLs removed, link text kept. URLs are a
    sixth of web articles' Markdown; indexed, they lengthen chunks and match path words ("azure", "logs")."""
    return _URL.sub(" ", _MD_LINK.sub(r"\1", md))


def _previous_vectors(db_path: Path) -> dict:
    """{chunk_id: (model label, sha256 of the embedded text, vector)} from an existing index, when it has them."""
    if not db_path.exists():
        return {}
    import numpy as np
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        label = dict(con.execute("SELECT key, value FROM meta")).get("dense_model")
        cols = [r[1] for r in con.execute("PRAGMA table_info(chunk_vectors)")]
        if not label or "text_sha256" not in cols:
            return {}
        return {cid: (label, key, np.frombuffer(vec, dtype=np.float32))
                for cid, key, vec in con.execute("SELECT chunk_id, text_sha256, vec FROM chunk_vectors")}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def build_index(db_path: str | Path, chunk_files: list[str | Path], meta: dict | None = None,
                pack_aliases: dict[str, list[str]] | None = None, index_text: str = "plain", dense: bool = False,
                log=print) -> dict:
    """``index_text``: "plain" indexes ``plain_text`` of each chunk (ranking v3); "markdown" indexes the Markdown as
    stored (v2). The stored chunk text is the Markdown either way. ``dense``: also store each chunk's embedding
    (dense.py; needs the dense extra and the pinned model)."""
    db_path = Path(db_path)
    previous = _previous_vectors(db_path) if dense else {}  # reused where the embedded text and model are unchanged
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.execute(f"CREATE TABLE chunks ({', '.join(f'{c} {t}' for c, t in COLUMNS)})")
    con.execute(f"CREATE VIRTUAL TABLE chunks_fts USING fts5(heading_path, text, content='chunks', "
                f"content_rowid='rowid', tokenize='{FTS_TOKENIZER}')")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE packs (pack_id TEXT PRIMARY KEY, source_aliases TEXT)")
    n = 0
    packs = set()
    for f in chunk_files:
        with open(f, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if not line.strip():
                    continue
                r = json.loads(line)
                packs.add(r["pack_id"])
                row = {
                    "chunk_id": r["chunk_id"], "pack_id": r["pack_id"], "pack_version": r["pack_version"],
                    "document_id": r["document_id"], "edition_id": r["edition_id"], "variant": r.get("variant"),
                    "corpus": r["corpus"], "include_default": 1 if r["include_in_default_retrieval"] else 0,
                    "exclusion_reason": r.get("exclusion_reason"), "content_type": r["content_type"],
                    "authority_class": r["authority_class"], "review_status": r["review_status"],
                    "superseded": 1 if r["superseded"] else 0, "rights_state": r["distribution"]["rights_state"],
                    "distribution_policy": r["distribution"]["policy"],
                    "applicability": json.dumps(r.get("applicability") or []),
                    "heading_path": " > ".join(r["heading_path"]), "citation": json.dumps(r["citation"]),
                    "text": r["text"], "token_count": r["token_count"],
                    "fragment_of": (r.get("fragment") or {}).get("of_element_id"),
                    "overlap_from": (r.get("overlap") or {}).get("from_chunk_id"), "ord": i,
                    "record": line.strip(),
                }
                cols = list(row)
                con.execute(f"INSERT INTO chunks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                            [row[c] for c in cols])
                n += 1
    if index_text == "plain":
        con.executemany("INSERT INTO chunks_fts(rowid, heading_path, text) VALUES (?, ?, ?)",
                        [(rid, hp, plain_text(t)) for rid, hp, t in con.execute("SELECT rowid, heading_path, text FROM chunks")])
    elif index_text == "markdown":
        con.execute("INSERT INTO chunks_fts(rowid, heading_path, text) SELECT rowid, heading_path, text FROM chunks")
    else:
        raise ValueError(f"index_text must be 'plain' or 'markdown', not {index_text!r}")
    con.executemany("INSERT INTO packs VALUES (?, ?)",
                    [(p, json.dumps((pack_aliases or {}).get(p, []))) for p in sorted(packs)])
    dense_model = None
    if dense:
        from . import dense as dense_mod
        ok, why = dense_mod.available()
        if not ok:
            raise SystemExit(why)
        rows = con.execute("SELECT chunk_id, heading_path, text FROM chunks ORDER BY rowid").fetchall()
        texts = [dense_mod.chunk_text(hp, plain_text(t)) for _, hp, t in rows]
        label = dense_mod.model_label()
        keys = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
        todo = [i for i, (r, key) in enumerate(zip(rows, keys)) if previous.get(r[0], ("", "", None))[:2] != (label, key)]
        log(f"embedding {len(todo)} of {len(rows)} chunks with {dense_mod.MODEL_ID}"
            + (f"; {len(rows) - len(todo)} reused from the previous index" if len(todo) < len(rows) else "")
            + (" (a few minutes on CPU)" if len(todo) > 500 else ""))
        import numpy as np
        vecs = np.zeros((len(rows), dense_mod.DIM), dtype=np.float32)
        if todo:
            vecs[todo] = dense_mod.encoder().encode([texts[i] for i in todo])
        fresh = set(todo)
        for i, r in enumerate(rows):
            if i not in fresh:
                vecs[i] = previous[r[0]][2]
        dense_mod.store_vectors(con, [r[0] for r in rows], vecs, keys)
        dense_model = label
    info = {"index_schema_version": INDEX_SCHEMA_VERSION, "fts_tokenizer": FTS_TOKENIZER, "chunks": str(n),
            "bm25_column_weights": "heading_path=2.0,text=1.0", "index_text": index_text,
            "packs": ",".join(sorted(packs)), "sqlite_version": sqlite3.sqlite_version,
            **({"dense_model": dense_model} if dense_model else {}), **(meta or {})}
    con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in info.items()])
    con.commit()
    con.execute("VACUUM")
    con.close()
    return info


_WORD = re.compile(r"[\w][\w.\-:/@]*", re.UNICODE)

# Common English function words dropped from free-text queries (quoted phrases are kept verbatim).
STOPWORDS = frozenset("""a about above after again against all am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have having he
her here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of
off on once only or other our ours ourselves out over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up very was we were what when where which while
who whom why will with would you your yours yourself yourselves according besides""".split())

# Query-time ranking configuration. Every search response and evaluation run reports the configuration it used.
# All features are generic (no question-, chapter- or pack-specific rules); they were chosen on the tuning split
# only and frozen before the held-out split was run (see eval/README.md).
RANKING_V2: dict[str, Any] = {
    "version": "2",
    "stopwords": False,         # keep function words in the BM25 query (IDF already discounts them; removing them
                                # lowered tuning recall). STOPWORDS only delimit the content-word pairs below.
    "all_terms_first": False,   # True: chunks matching every query term rank ahead of partial matches
    "candidates": 100,          # candidate pool per query, ranked by BM25 (heading_path 2.0, text 1.0)
    # Reference lists and chapter-end notes cite titles and URLs densely and would otherwise outrank
    # substantive passages on title-word queries.
    "corpus_weights": {"notes": 0.5, "references": 0.5},
    "proximity_weight": 1.0,    # score *= 1 + w * (share of adjacent content-word pairs found as phrases)
    "visual_boost": 1.5,        # score multiplier for chunks with figures when the query asks for a figure/diagram
    "alias_slots": 2,           # top-k slots reserved for packs named by a declared source alias (0 = off)
}

# Ranking v3 (2026-10-25), chosen on the tuning pool (eval/diagnose_ranks.py; eval/README.md) and frozen before
# the sealed held-out set x was run. All features are generic: no question-, pack- or document-specific rule.
# dense_within_filters was added after the first freeze and before the held-out run, as a fix for filtered and
# per-source searches; on the tuning pool it changed one rank below the top 5 and nothing else.
RANKING_V3: dict[str, Any] = {
    **RANKING_V2,
    "version": "3",
    "alias_strip": "auto",      # keep a named source's words when they are the question's subject
    "alias_candidates": 20,     # + the best candidates of each named pack
    "alias_mode": "per_pack",   # reserved slots go round-robin to every named pack
    "alias_window": 5,          # ... within the top 5, whatever k is
    "rerank_weight": 0.2,       # best passage's IDF-weighted coverage of the question's concepts (with synonyms)
    "dense_weight": 0.7,        # cosine similarity of local bge-small embeddings (dense.py); BM25 keeps 0.1
    "dense_candidates": 100,    # dense top candidates joining the BM25 pool
    "dense_within_filters": True,  # ... taken among the chunks the search's filters allow (packs, corpus, ...)
    # without the dense extra or chunk vectors: the lexical blend measured best on the tuning pool
    "lexical_fallback": {"rerank_weight": 0.3, "dense_weight": 0.0},
}

# Ranking v4 (2026-10-27), chosen on the tuning pool (every set but the sealed set y, plus the practitioner test's
# questions) and frozen before y was run. Generic changes only. The index side of v4: web navigation sections out of
# default retrieval (packbuild.NAVIGATION_TITLES) and more source names ("the book", "NIST SP 800-61").
RANKING: dict[str, Any] = {
    **RANKING_V3,
    "version": "4",
    "v4_tokens": True,          # compound tokens match by their parts; comparison words ("say", "compare") are no content
    "dense_norm": "minmax",     # similarity rescaled over the dense top: v3's raw cosine barely separated its best match
    "dense_weight": 0.4,        # ... so it needs less weight; BM25 0.3, best-passage coverage 0.3
    "rerank_weight": 0.3,
    "similarity_levels": True,  # coverage levels also use the best result's similarity (SIMILARITY_LOW/HIGH)
}

VISUAL_WORDS = frozenset("diagram diagrams figure figures illustration illustrated chart charts image images picture "
                         "graphic graphics depicted depicts depict visual visualization screenshot".split())


def query_terms(q: str, stopwords: bool = True) -> tuple[list[str], list[str]]:
    """Return (terms, adjacent pairs) as safe FTS5 phrase strings. User-quoted phrases are kept verbatim."""
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    terms, pairs, prev = [], [], None
    for p in phrases:
        toks = re.findall(r"\w+", p)
        if toks:
            terms.append('"' + " ".join(toks) + '"')
    for w in _WORD.findall(rest):
        toks = re.findall(r"\w+", w)
        if not toks:
            continue
        if len(toks) == 1 and toks[0].lower() in STOPWORDS:
            prev = None  # pairs are content-word pairs and never bridge a function word
            if stopwords:
                continue
        else:
            if prev is not None:
                pairs.append('"' + " ".join(prev + toks) + '"')
            prev = toks
        terms.append('"' + " ".join(toks) + '"')  # e.g. "T1539" or "800 61r3" (adjacent tokens)
    return list(dict.fromkeys(terms)), list(dict.fromkeys(pairs))


def to_fts_query(q: str, mode: str = "and", stopwords: bool = True) -> str:
    """Convert free text to a safe FTS5 query (terms quoted; phrases kept)."""
    terms, _ = query_terms(q, stopwords)
    if not terms:
        return ""
    return (" AND " if mode == "and" else " OR ").join(terms)


# ------------------------------------------------------------------ v3 reranking helpers
# Incident-response vocabulary: words in one group stand for the same concept when the reranker measures how much
# of a question a passage covers ("confirm containment worked" ~ "validate containment effectiveness"). Generic
# terms of the field, not phrases from any evaluation question; extend with care and re-run the tuning pool.
SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("contain", "containment", "isolate", "isolation", "quarantine", "disconnect"),
    ("verify", "validate", "confirm", "check", "test"),
    ("effective", "effectiveness", "worked", "succeed", "successful"),
    ("revoke", "invalidate", "terminate", "disable"),
    ("compromise", "compromised", "breach", "breached", "takeover", "hacked"),
    ("steal", "stolen", "theft", "exfiltrate", "exfiltration", "leak", "leaked", "exposed"),
    ("replay", "replayed", "reuse", "reused"),
    ("delete", "deletion", "remove", "purge", "wipe", "destroy"),
    ("acquire", "acquisition", "collect", "collection", "capture", "image", "snapshot", "copy"),
    ("memory", "ram", "volatile"),
    ("emergency", "break-glass", "breakglass"),
    ("admin", "administrator", "privileged"),
    ("mfa", "multifactor", "multi-factor", "2fa"),
    ("attacker", "adversary", "intruder", "actor"),
    ("notify", "notification", "report", "reporting"),
    ("shutdown", "power", "poweroff"),
    ("indicator", "ioc", "iocs"),
    ("log", "logs", "logging", "audit", "trail"),
    ("first", "initial", "immediately", "immediate"),
    ("phish", "phishing"),
    ("backup", "backups", "restore", "recovery"),
    ("forward", "forwarding", "redirect"),
    ("detect", "detection", "alert", "finding"),
)
_SUFFIXES = (("ational", "ate"), ("ization", "ize"), ("iveness", "ive"), ("fulness", "ful"), ("ousness", "ous"),
             ("ation", "ate"), ("ments", ""), ("ment", ""), ("ness", ""), ("ities", ""), ("ity", ""), ("ings", ""),
             ("ing", ""), ("ied", "y"), ("ies", "y"), ("ed", ""), ("es", ""), ("s", ""))


def stem(w: str) -> str:
    """A light suffix stripper (query and passages go through the same function, so only consistency matters)."""
    w = w.lower()
    for suf, rep in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not (suf == "s" and w.endswith("ss")):
            w = w[: len(w) - len(suf)] + rep
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    if w.endswith("i") and len(w) > 3:
        w = w[:-1] + "y"
    return w


_SYN = {stem(w): frozenset(stem(x) for x in group) for group in SYNONYMS for w in group}


# v4: words that frame a comparison or a request ("what do X and Y each say about ...") rather than its subject
COMPARISON_WORDS = frozenset("compare compared comparison versus vs each say says said recommend recommends "
                             "recommended advise advises according both difference differences differ tell".split())
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:_/+-]*[A-Za-z0-9]|[A-Za-z0-9]")
_COMPOUND = re.compile(r"[:+/_.\-]+")


def _parts(tok: str) -> list[str]:
    """The parts of a compound token ("tlp:amber+strict" -> tlp, amber, strict), when it has two or more."""
    ps = [x for x in _COMPOUND.split(tok) if len(x) >= 2]
    return ps if len(ps) >= 2 else []


def concepts(q: str, v4: bool = False) -> list[tuple[str, frozenset]]:
    """(surface word, what counts as a match) for each content word of the question, in order. A match is a stem in
    the passage; with v4 also all the parts of a compound word ("AMBER+STRICT" matches "TLP:AMBER+STRICT"), and
    comparison words are not content."""
    out, seen = [], set()
    for w in _TOKEN.findall(q):
        lw = w.lower()
        if lw in STOPWORDS or v4 and lw in COMPARISON_WORDS or len(lw) < 2 and not lw.isdigit():
            continue
        st = stem(lw)
        if st in seen:
            continue
        seen.add(st)
        alts = _SYN.get(st, frozenset()) | {st}
        if v4 and _parts(lw):
            alts = alts | {("&",) + tuple(stem(x) for x in _parts(lw))}
        out.append((w, frozenset(alts)))
    return out


def _stems(text: str, v4: bool = False) -> set[str]:
    toks = _TOKEN.findall(text.lower())
    out = {stem(w) for w in toks}
    if v4:  # the parts of compound tokens count on their own too
        out |= {stem(x) for w in toks for x in _parts(w)}
    return out


def _hit(alts: frozenset, st: set[str]) -> bool:
    return any(a in st if isinstance(a, str) else all(x in st for x in a[1:]) for a in alts)


def concept_weights(con, cons: list) -> list[float]:
    """IDF of each concept's surface word in the index (BM25-style), so rare words count more."""
    n_docs = con.execute("SELECT count(*) FROM chunks").fetchone()[0] or 1
    out = []
    for w, _ in cons:
        try:
            df = con.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                             ['"' + w.replace('"', "") + '"']).fetchone()[0]
        except sqlite3.OperationalError:
            df = 0
        out.append(math.log((n_docs - df + 0.5) / (df + 0.5) + 1))
    return out


def match_signals(heading: str, text: str, cons: list, weights: list[float], v4: bool = False) -> dict:
    """Which of the question's key words a chunk contains (heading included), and its best passage's coverage."""
    st = _stems(heading, v4) | _stems(text, v4)
    return {"coverage": round(best_passage_coverage(heading, text, cons, weights, v4), 2),
            "matched_terms": [w for w, alts in cons if _hit(alts, st)],
            "missing_terms": [w for w, alts in cons if not _hit(alts, st)]}


def best_passage_coverage(heading: str, text: str, cons: list, weights: list[float], v4: bool = False) -> float:
    """Highest IDF-weighted share of the question's concepts found in one passage of the chunk (its heading path
    counts for every passage). A long chunk with one precise passage is not diluted by the rest of its text."""
    total = sum(weights) or 1.0
    head = _stems(heading, v4)
    blocks = [b for b in re.split(r"\n\s*\n|\n(?=[-*•]|\d+\.)", text) if b.strip()] or [text]
    best = 0.0
    for b in blocks:
        st = head | _stems(b, v4)
        got = sum(wt for (_, alts), wt in zip(cons, weights) if _hit(alts, st))
        best = max(best, got / total)
    return best


def named_sources(q: str, aliases: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Find declared source aliases in the query; return the query without them and the named pack ids."""
    found: list[str] = []
    spans = sorted(((a, p) for p, al in aliases.items() for a in al), key=lambda x: -len(x[0]))
    for alias, pack in spans:
        pat = re.compile(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", re.IGNORECASE)
        if pat.search(q):
            q = pat.sub(" ", q)
            if pack not in found:
                found.append(pack)
    return q, found


def search(db_path: str | Path, query: str, *, k: int = 5, packs: list[str] | None = None,
           corpus: list[str] | None = None, include_excluded: bool = False, review_status: list[str] | None = None,
           authority: list[str] | None = None, applicability: list[str] | None = None,
           pack_version: str | None = None, content_type: list[str] | None = None,
           rights_state: list[str] | None = None, dedupe: bool = True, mode: str = "auto",
           ranking: dict | None = None) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    where, params = [], []
    if not include_excluded:
        where.append("c.include_default = 1")
    for col, vals in (("pack_id", packs), ("corpus", corpus), ("review_status", review_status),
                      ("authority_class", authority), ("content_type", content_type), ("rights_state", rights_state)):
        if vals:
            where.append(f"c.{col} IN ({', '.join('?' for _ in vals)})")
            params += list(vals)
    if pack_version:
        where.append("c.pack_version = ?")
        params.append(pack_version)
    if applicability:
        where.append("(" + " OR ".join("c.applicability LIKE ?" for _ in applicability) + ")")
        params += [f'%"{a}"%' for a in applicability]
    # overrides apply to the frozen ranking they name: a configuration declaring version "2" or "3" starts from it
    base = {"2": RANKING_V2, "3": RANKING_V3}.get(str((ranking or {}).get("version")), RANKING)
    cfg = {**base, **(ranking or {})}
    aliases = {}
    if cfg["alias_slots"]:
        try:
            aliases = {p: json.loads(a) for p, a in con.execute("SELECT pack_id, source_aliases FROM packs")}
        except sqlite3.OperationalError:
            aliases = {}
    kw_query, named = named_sources(query, aliases) if aliases else (query, [])
    # alias_strip: drop the named source's words from the query (v2). "auto" keeps them when fewer than two content
    # words would remain, i.e. when the name is the subject ("What does Event Threat Detection do?").
    strip = cfg.get("alias_strip", True)  # also decides which words the match signals count
    v4 = bool(cfg.get("v4_tokens"))  # compound-token matching and comparison words (v4)
    if strip == "auto":
        strip = len(concepts(kw_query, v4)) >= 2
    terms, pairs = query_terms(kw_query if strip else query, cfg["stopwords"])
    if cfg.get("expand_synonyms") and terms:  # candidate pool also reaches passages that use another word
        extra = {w for _, alts in concepts(kw_query if strip else query) for g in SYNONYMS if stem(_) in {stem(x) for x in g}
                 for w in g if " " not in w}
        terms = list(dict.fromkeys(terms + ['"' + w + '"' for w in sorted(extra) if re.fullmatch(r"[a-z0-9]+", w)]))
    used_mode, fts_q, rows = mode, "", []
    dense_note, dense_sims = "", {}
    if terms:
        filt = ("AND " + " AND ".join(where)) if where else ""
        base_sql = (f"SELECT c.rowid AS rowid, c.*, bm25(chunks_fts, {float(cfg.get('heading_weight', 2.0))}, 1.0) AS score "
                    "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid WHERE chunks_fts MATCH ? " + filt +
                    " {pack} ORDER BY score LIMIT ?")
        n_cand = max(cfg["candidates"], k * 4)
        cand: dict[str, dict] = {}
        all_terms: set[str] = set()
        queries = {"and": " AND ".join(terms), "or": " OR ".join(terms)}
        run = (["and", "or"] if cfg["all_terms_first"] else ["or"]) if mode == "auto" else [mode]
        # alias_candidates: besides the global pool, the best candidates of each pack the query names, so a named
        # pack is never absent from the pool because other packs use the same words more often
        pools = [(None, n_cand)] + [(p, cfg["alias_candidates"]) for p in named if cfg.get("alias_candidates")]
        for pack_filter, limit in pools:
            sql = base_sql.format(pack="AND c.pack_id = ?" if pack_filter else "")
            for m in run:
                for r in con.execute(sql, [queries[m]] + params + ([pack_filter] if pack_filter else []) + [limit]).fetchall():
                    cand.setdefault(r["chunk_id"], dict(r))
                    if m == "and":
                        all_terms.add(r["chunk_id"])
        # v3: dense candidates (embedding similarity) join the pool; chunks found only this way have no BM25 score
        dense_sims, dense_note = {}, ""
        if cfg.get("dense_weight"):
            from . import dense as dense_mod
            # the dense top is taken among the chunks the filters allow (default retrieval, packs, corpus, ...), so a
            # search restricted to one pack ranks by similarity within that pack
            allowed = ({r[0] for r in con.execute("SELECT c.chunk_id FROM chunks c WHERE " + " AND ".join(where), params)}
                       if where and cfg.get("dense_within_filters") else None)
            dense_sims, dense_note = dense_mod.query_scores(db_path, query, cfg.get("dense_candidates", 100), allowed)
            if not dense_sims and cfg.get("lexical_fallback"):
                cfg = {**cfg, **cfg["lexical_fallback"]}  # dense retrieval unavailable: the lexical blend
            missing = [cid for cid in dense_sims if cid not in cand]
            for i in range(0, len(missing), 500):
                part = missing[i:i + 500]
                sql = (f"SELECT c.rowid AS rowid, c.*, 0.0 AS score FROM chunks c WHERE c.chunk_id IN "
                       f"({','.join('?' for _ in part)}) " + filt)
                for r in con.execute(sql, part + params).fetchall():
                    cand.setdefault(r["chunk_id"], dict(r))
        if mode == "and":
            all_terms = set(cand)
        fts_q = queries["or"] if mode != "and" else queries["and"]
        used_mode = mode
        # adjacency: which candidates contain each adjacent query word pair as a phrase
        pair_hits: dict[str, int] = {}
        if cfg["proximity_weight"] and pairs and cand:
            ids = [c["rowid"] for c in cand.values()]
            marks = ",".join("?" for _ in ids)
            for pq in pairs:
                for (rid,) in con.execute(f"SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid IN ({marks})",
                                          [pq] + ids):
                    pair_hits[rid] = pair_hits.get(rid, 0) + 1
        visual = any(w.lower() in VISUAL_WORDS for w in re.findall(r"\w+", kw_query))
        for c in cand.values():
            sc = -c["score"] * cfg["corpus_weights"].get(c["corpus"], 1.0)
            if pairs and cfg["proximity_weight"]:
                sc *= 1 + cfg["proximity_weight"] * pair_hits.get(c["rowid"], 0) / len(pairs)
            if visual and cfg["visual_boost"] != 1.0 and '"kind": "figure"' in c["record"]:
                sc *= cfg["visual_boost"]
            c["_score"] = sc
            c["_all_terms"] = c["chunk_id"] in all_terms
        if cfg.get("rerank_weight") and cand:
            # v3: blend the (normalised) BM25 score with the best passage's coverage of the question's concepts,
            # each concept weighted by its IDF in the index
            cons = concepts(kw_query if strip else query, v4)
            if cons:
                weights = concept_weights(con, cons)
                top = max(c["_score"] for c in cand.values()) or 1.0
                lam, mu = cfg["rerank_weight"], (cfg.get("dense_weight") or 0.0) if dense_sims else 0.0
                # dense_norm "minmax" (v4): cosine similarities fall in a narrow band, so the dense top's raw values
                # barely separate its best match from its 100th; rescaled to [0, 1] over the dense top, like BM25
                lo, hi = (min(dense_sims.values()), max(dense_sims.values())) if dense_sims else (0.0, 0.0)
                minmax = cfg.get("dense_norm") == "minmax" and hi > lo
                for c in cand.values():
                    rec = json.loads(c["record"])
                    cov = best_passage_coverage(" ".join(rec["heading_path"]), rec["text"], cons, weights, v4)
                    c["_coverage"] = round(cov, 3)
                    # cosine similarity of bge embeddings sits in [0.3, 0.9]; chunks outside the dense top get the floor
                    sim = dense_sims.get(c["chunk_id"], lo)
                    if minmax:
                        sim = (sim - lo) / (hi - lo)
                    c["_score"] = (1 - lam - mu) * c["_score"] / top + lam * cov + mu * sim
        ordered = sorted(cand.values(), key=lambda c: ((not c["_all_terms"]) if cfg["all_terms_first"] else False,
                                                      -c["_score"], c["ord"]))
        if named and cfg["alias_slots"] and len({c["pack_id"] for c in ordered}) > 1:
            # reserve slots near the top for packs the query names, without dropping their order
            window = min(k, cfg.get("alias_window") or k)  # reserved slots fall within the top `window` results
            if cfg.get("alias_mode") == "per_pack":
                # every named pack gets a slot (round-robin over the named packs, best chunk first), up to the quota;
                # a question naming two sources needs evidence from both
                quota = min(max(cfg["alias_slots"], cfg.get("alias_per_pack", 1) * len(named)), window)
                per = {p: [c for c in ordered if c["pack_id"] == p] for p in named}
                first = []
                while len(first) < quota and any(per.values()):
                    for p in named:
                        if per[p] and len(first) < quota:
                            first.append(per[p].pop(0))
            else:  # v2: the quota goes to the best chunks of any named pack
                quota = min(cfg["alias_slots"], window)
                first = [c for c in ordered if c["pack_id"] in named][:quota]
            ids = {c["chunk_id"] for c in first}
            rest = [c for c in ordered if c["chunk_id"] not in ids]
            ordered = sorted(first + rest[:max(0, window - len(first))], key=lambda c: -c["_score"]) + rest[window - len(first):]
        rows = ordered
    # match signals (informational; they never change the order): coverage of the question's key words
    # the question without the names of the sources it targets (a pack's text rarely repeats its own name)
    sig_cons = concepts(kw_query if strip and concepts(kw_query, v4) else query, v4) if cfg.get("signals", True) else []
    sig_weights = concept_weights(con, sig_cons) if sig_cons else []
    con.close()
    results, seen_frag, dropped = [], {}, []
    for r in rows:
        rec = json.loads(r["record"])
        key = None
        if dedupe:
            # collapse fragments of one unit, and an overlap chunk with its source chunk
            key = r["fragment_of"] or None
            if key and key in seen_frag:
                dropped.append({"chunk_id": r["chunk_id"], "duplicate_of": seen_frag[key], "reason": "same split unit"})
                continue
            if r["overlap_from"] and any(x["chunk_id"] == r["overlap_from"] for x in results):
                pass  # distinct passage that merely starts with overlap text: keep
        if key:
            seen_frag[key] = r["chunk_id"]
        results.append({
            "rank": len(results) + 1,
            "score": round(r["_score"], 4),
            "chunk_id": r["chunk_id"],
            "pack_id": r["pack_id"],
            "pack_version": r["pack_version"],
            "edition_id": r["edition_id"],
            "heading_path": rec["heading_path"],
            "citation": rec["citation"],
            "content_type": rec["content_type"],
            "authority_class": rec["authority_class"],
            "review_status": rec["review_status"],
            "rights_state": rec["distribution"]["rights_state"],
            "required_context_ids": rec["required_context_ids"],
            "asset_refs": rec["asset_refs"],
            "note_refs": rec["note_refs"],
            "token_count": rec["token_count"],
            "include_in_default_retrieval": rec["include_in_default_retrieval"],
            "text": rec["text"],
        })
        if sig_cons:
            results[-1]["match"] = match_signals(" ".join(rec["heading_path"]), rec["text"], sig_cons, sig_weights, v4)
        if len(results) >= k:
            break
    return {"query": query, "fts_query": fts_q, "match_mode": used_mode, "k": k, "named_sources": named,
            "ranking": cfg, "results": results, "deduplicated": dropped, "coverage": coverage_summary(results, sig_cons, _best_sim(results, dense_sims) if cfg.get("similarity_levels") and dense_sims else None),
            "dense": "on" if dense_sims else (dense_note or None)}  # None: not requested; text: why it is off


# Levels of the best result's coverage of the question's key words, set on the tuning pool. Measured with the frozen
# v3 (eval/README.md, "Coverage signal"): median best coverage 0.80 when the evidence was in the top 5, 0.74 when it
# was missed, 0.49 for unanswerable questions; on the sealed set x 0.70, 0.57 and 0.40. Coverage says how much of the
# question a passage's words address, not that the passage is correct or the best one, and it cannot tell that a
# version named in the question does not exist ("TLP 3.0" matches "3.0" elsewhere).
COVERAGE_LEVELS = ((0.75, "high"), (0.5, "medium"), (0.0, "low"))


# v4 levels, when the embedding model is on: the best result's cosine similarity to the question (bge-small-en-v1.5)
# separates answerable from unanswerable questions better than word coverage does. On the tuning pool, answerable
# questions had a median best similarity of 0.80 and unanswerable ones 0.69; below 0.73 flagged 8 of 10 unanswerable
# questions and 4 of 55 whose evidence was found. "high" also needs most of the question's key words.
SIMILARITY_LOW, SIMILARITY_HIGH = 0.73, 0.78


def coverage_summary(results: list[dict], cons: list, best_sim: float | None = None) -> dict | None:
    if not cons:
        return None
    best = max((r["match"]["coverage"] for r in results if r.get("match")), default=0.0)
    found = {w for r in results if r.get("match") for w in r["match"]["matched_terms"]}
    uncovered = [w for w, _ in cons if w not in found]
    if best_sim is None:
        level = next(name for floor, name in COVERAGE_LEVELS if best >= floor)
    else:
        level = ("low" if best_sim < SIMILARITY_LOW else
                 "high" if best >= COVERAGE_LEVELS[0][0] and best_sim >= SIMILARITY_HIGH else "medium")
    advice = {
        "high": "The best passage contains most of the question's key words. Read it in context (expand_context) "
                "and check that it answers the question before relying on it.",
        "medium": "Passages address part of the question. Check the missing terms; rephrase with the source's own "
                  "vocabulary or name the product or source to find the rest.",
        "low": "The passages are not close to the question. Read the top one or two: if they do not answer it, "
               "rephrase in the product's or source's own terms or narrow by pack; if that also fails, say that the "
               "sources do not cover it rather than answering from memory.",
    }[level]
    out = {"level": level, "best_coverage": round(best, 2), "terms_in_no_result": uncovered, "advice": advice}
    if best_sim is not None:
        out["best_similarity"] = round(best_sim, 3)
    return out


def _best_sim(results: list[dict], dense_sims: dict[str, float]) -> float:
    """The best returned result's similarity to the question (results outside the dense top get its lowest value)."""
    floor = min(dense_sims.values())
    return max((dense_sims.get(r["chunk_id"], floor) for r in results), default=floor)


def get_chunk(db_path: str | Path, chunk_id: str) -> dict | None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = con.execute("SELECT record FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
    con.close()
    return json.loads(row[0]) if row else None


def all_chunks(db_path: str | Path, pack_id: str | None = None) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if pack_id:
        rows = con.execute("SELECT record FROM chunks WHERE pack_id = ? ORDER BY ord", (pack_id,)).fetchall()
    else:
        rows = con.execute("SELECT record FROM chunks ORDER BY pack_id, ord").fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]
