"""Optional dense (embedding) retrieval for the combined index: ranking v3's semantic component.

A small local embedding model (BAAI bge-small-en-v1.5, MIT licence, ONNX) encodes every chunk once at index time
(``irpacks index --dense``) and each query at search time. Nothing is sent anywhere: the model runs locally through
onnxruntime. The model files are fetched explicitly (``irpacks models fetch``), pinned by sha256 in
``models.lock.json``, and never downloaded implicitly.

Needs the optional extra: ``uv sync --extra dense`` (onnxruntime, tokenizers, numpy). Without it, or without
vectors in the index, search falls back to the lexical ranking and says so in its response.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
from pathlib import Path

from .util import PROJECT_ROOT, sha256_file

MODEL_ID = "bge-small-en-v1.5"
MODELS_LOCK = PROJECT_ROOT / "models.lock.json"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # bge v1.5 retrieval instruction
MAX_TOKENS = 512
DIM = 384


def model_dir() -> Path:
    return Path(os.environ.get("IRPACKS_MODEL_DIR") or PROJECT_ROOT / "build" / "models" / MODEL_ID)


def available() -> tuple[bool, str]:
    """(True, "") when the extra is installed and the pinned model files are present and match the lock."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return False, "dense retrieval needs the optional extra: uv sync --extra dense"
    lock = json.loads(MODELS_LOCK.read_text(encoding="utf-8"))["models"][MODEL_ID]
    for name, meta in lock["files"].items():
        f = model_dir() / name
        if not f.exists():
            return False, f"model file {f} missing: run `irpacks models fetch`"
    return True, ""


def verify_model() -> list[dict]:
    lock = json.loads(MODELS_LOCK.read_text(encoding="utf-8"))["models"][MODEL_ID]
    out = []
    for name, meta in lock["files"].items():
        f = model_dir() / name
        actual = sha256_file(f) if f.exists() else None
        out.append({"file": name, "status": "ok" if actual == meta["sha256"] else ("absent" if actual is None else "MISMATCH")})
    return out


def fetch_model(run=None) -> list[dict]:
    """Opt-in download of the pinned model files (curl), each verified against models.lock.json."""
    import subprocess
    lock = json.loads(MODELS_LOCK.read_text(encoding="utf-8"))["models"][MODEL_ID]
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for name, meta in lock["files"].items():
        f = d / name
        if not (f.exists() and sha256_file(f) == meta["sha256"]):
            r = subprocess.run(["curl", "-sS", "-L", "--fail", "--retry", "3", "-o", str(f), meta["url"]],
                               capture_output=True, text=True)
            if r.returncode != 0:
                out.append({"file": name, "status": "error", "detail": r.stderr.strip()[:200]})
                continue
        ok = sha256_file(f) == meta["sha256"]
        if not ok:
            f.unlink()
        out.append({"file": name, "status": "ok" if ok else "MISMATCH (deleted)"})
    return out


class Encoder:
    def __init__(self, directory: Path | None = None):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        d = directory or model_dir()
        lock = json.loads(MODELS_LOCK.read_text(encoding="utf-8"))["models"][MODEL_ID]
        self.np = np
        self.tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self.tok.enable_truncation(max_length=MAX_TOKENS)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(str(d / lock["onnx_file"]), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}

    def encode(self, texts: list[str], batch: int = 32):
        np = self.np
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        order = np.argsort([len(t) for t in texts])  # batch texts of similar length
        for s in range(0, len(order), batch):
            idx = order[s:s + batch]
            enc = self.tok.encode_batch([texts[i] for i in idx])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            cls = self.sess.run(None, feeds)[0][:, 0]  # CLS pooling (bge)
            out[idx] = cls / np.linalg.norm(cls, axis=1, keepdims=True)
        return out


@functools.lru_cache(maxsize=1)
def encoder() -> Encoder:
    return Encoder()


def model_label() -> str:
    lock = json.loads(MODELS_LOCK.read_text(encoding="utf-8"))["models"][MODEL_ID]
    return f"{MODEL_ID} ({lock['onnx_file']}, sha256 {lock['files'][lock['onnx_file']]['sha256'][:12]})"


def chunk_text(heading_path: list[str] | str, plain: str) -> str:
    hp = heading_path if isinstance(heading_path, str) else " > ".join(heading_path)
    return f"{hp}\n{plain}"


def store_vectors(con: sqlite3.Connection, ids: list[str], vectors, text_keys: list[str] | None = None) -> None:
    """One vector per chunk, with the sha256 of the text it embeds, so a rebuild can reuse unchanged vectors."""
    con.execute("CREATE TABLE chunk_vectors (chunk_id TEXT PRIMARY KEY, vec BLOB, text_sha256 TEXT)")
    con.executemany("INSERT INTO chunk_vectors VALUES (?, ?, ?)",
                    [(cid, vectors[i].astype("float32").tobytes(), (text_keys or [None] * len(ids))[i])
                     for i, cid in enumerate(ids)])


@functools.lru_cache(maxsize=4)
def _matrix(db_path: str, mtime: float):
    import numpy as np
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT chunk_id, vec FROM chunk_vectors").fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    if not rows:
        return None
    return [r[0] for r in rows], np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])


def vectors(db_path: str | Path):
    p = str(Path(db_path).resolve())
    return _matrix(p, os.path.getmtime(p))


def query_scores(db_path: str | Path, query: str, top: int,
                 allowed: set[str] | None = None) -> tuple[dict[str, float], str]:
    """Cosine similarity of the query to every chunk: (the top chunks' scores, "" or why dense retrieval is off).
    With ``allowed``, the top is taken among those chunks only (the search's filters)."""
    ok, why = available()
    if not ok:
        return {}, why
    m = vectors(db_path)
    if m is None:
        return {}, "the index has no chunk vectors: build it with `irpacks index --dense`"
    ids, mat = m
    q = encoder().encode([QUERY_PREFIX + query])[0]
    sims = mat @ q
    import numpy as np
    if allowed is not None:
        sims = np.where(np.fromiter((i in allowed for i in ids), bool, len(ids)), sims, -np.inf)
    best = np.argsort(-sims)[:top]
    return {ids[i]: float(sims[i]) for i in best if np.isfinite(sims[i])}, ""
