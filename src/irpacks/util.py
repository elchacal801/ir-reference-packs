"""Shared helpers: hashing, canonical JSON, deterministic IDs, text keys."""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def sha256_file(path: str | os.PathLike, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(*parts: Any, n: int = 12) -> str:
    """Deterministic short hash of the given parts (joined with a separator)."""
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8"))
    return h.hexdigest()[:n]


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(obj: Any) -> str:
    """Stable JSON serialisation used for hashing and deterministic files."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_json(path: str | os.PathLike, obj: Any, *, indent: int | None = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if str(path).endswith(".gz"):
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
    else:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=indent, sort_keys=False)
            fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str | os.PathLike) -> Any:
    path = str(path)
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_jsonl(path: str | os.PathLike, rows: Iterable[dict]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=False))
            fh.write("\n")
            n += 1
    return n


def read_jsonl(path: str | os.PathLike) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def rel(path: str | os.PathLike, root: str | os.PathLike = PROJECT_ROOT) -> str:
    """Portable POSIX path relative to the project root (never absolute)."""
    p = Path(path).resolve()
    try:
        return p.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Text keys used for source alignment.  These are *comparison* forms only; they
# never replace source text.

_WS = re.compile(r"\s+")


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def is_key_char(ch: str) -> bool:
    """Alphanumeric characters participate in the alignment key."""
    return ch.isalnum()


def text_key(text: str) -> str:
    """Case-folded alphanumeric skeleton of text (NFKC, whitespace/punct removed)."""
    t = nfkc(text).casefold()
    return "".join(ch for ch in t if ch.isalnum())


def visible_key(text: str) -> str:
    """Case-folded non-whitespace characters (punctuation retained) after NFKC."""
    return _WS.sub("", nfkc(text).casefold())


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def slugify(text: str, maxlen: int = 60) -> str:
    t = nfkc(text).lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:maxlen].rstrip("-") or "x"
