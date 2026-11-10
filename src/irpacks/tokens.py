"""Deterministic, dependency-free token counting.

``irpacks-approx-bpe`` approximates subword (BPE-style) token counts without
downloading a model tokenizer.  Each run of word characters counts as
ceil(len / 10) tokens when purely alphabetic and lower/title case (natural
words), ceil(len / 6) for other alphabetic runs (camelCase, ALLCAPS), and
ceil(len / 3) when it contains digits (hashes, IDs, timestamps are token-dense
in BPE vocabularies); every other non-space character counts as one token.  It is
used for chunk budgeting only.  If an embedding or generation model is added,
its own tokenizer and real input limit must be configured and recorded
instead; counts from this tokenizer are not that model's counts.
"""

from __future__ import annotations

import math
import re

TOKENIZER_ID = "irpacks-approx-bpe"
TOKENIZER_REVISION = "2"
_TOK = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _run_tokens(tok: str) -> int:
    if not (tok[0].isalnum() or tok[0] == "_"):
        return 1
    if any(c.isdigit() for c in tok):
        return math.ceil(len(tok) / 3)
    if tok.isalpha() and (tok.islower() or tok.istitle()):
        return math.ceil(len(tok) / 10)
    return math.ceil(len(tok) / 6)


def count(text: str) -> int:
    return sum(_run_tokens(m.group(0)) for m in _TOK.finditer(text))


def identity() -> dict:
    return {"id": TOKENIZER_ID, "revision": TOKENIZER_REVISION,
            "kind": "approximation", "note": "character-class run heuristic; not a model tokenizer"}
