"""Source registry: pinned source identities (``sources.lock.json``).

Source pinning is separate from dependency locking (``uv.lock``).  A hash
proves byte identity only, not content correctness, endorsement or rights.
Fetching is opt-in (``irpacks sources fetch``) and never runs during builds,
validation or search.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .util import PROJECT_ROOT, sha256_file, utc_now, write_json

LOCK_PATH = PROJECT_ROOT / "sources.lock.json"


def load_yaml(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def pack_dir(pack_id: str) -> Path:
    return PROJECT_ROOT / "packs" / pack_id


def load_recipe(pack_id: str, pack: dict | None = None) -> dict:
    """The pack's recipe. A recipe with ``artifacts_file`` takes its article list (``artifacts``, ``excluded``) from
    that generated file (``irpacks sources discover``) so the hand-written recipe keeps its comments."""
    pdir = pack_dir(pack_id)
    pack = pack or load_yaml(pdir / "pack.yaml")
    recipe = load_yaml(pdir / pack["recipe"])
    if recipe.get("artifacts_file"):
        af = pdir / recipe["artifacts_file"]
        listed = load_yaml(af) if af.exists() else {}
        recipe["artifacts"] = list(recipe.get("artifacts") or []) + list(listed.get("artifacts") or [])
        recipe["excluded"] = listed.get("excluded") or []
    return recipe


def all_pack_ids() -> list[str]:
    return sorted(p.name for p in (PROJECT_ROOT / "packs").iterdir() if (p / "pack.yaml").exists())


def _headers(path: Path) -> dict | None:
    """HTTP response headers recorded by curl -D next to a downloaded file."""
    cands = [Path(str(path) + ".headers"), path.with_suffix(".headers")]
    if path.suffix == ".pdf":
        cands.append(path.parent / "pdf.headers")
    if path.suffix == ".html":
        cands.append(path.parent / (path.stem.replace("final", "final") + ".headers"))
    for cand in cands:
        if not cand.exists():
            continue
        text = cand.read_text(encoding="utf-8", errors="replace")
        blocks = [b for b in re.split(r"\r?\n\r?\n", text) if b.strip()]
        last = blocks[-1] if blocks else ""
        h = {}
        for line in last.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                h[k.strip().lower()] = v.strip()
        return {k: h.get(k) for k in ("last-modified", "etag", "content-length", "content-type", "date") if k in h}
    return None


def _mtime_utc(path: Path) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _pdf_observations(path: Path) -> dict:
    obs: dict[str, Any] = {}
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        obs["pages"] = len(pdf)
        obs["bookmarks"] = len(list(pdf.get_toc()))
        meta = pdf.get_metadata_dict()
        obs["metadata"] = {k: v for k, v in meta.items() if v}
        pdf.close()
    except Exception as exc:  # pragma: no cover - observational only
        obs["error"] = str(exc)
    return obs


def _html_observations(path: Path, url: str | None) -> dict:
    """Version metadata of a captured web article (Learn pages carry git_commit_id / updated_at / ms.date)."""
    try:
        from .route_learn import observe
        return observe(path, url or "https://learn.microsoft.com/")
    except Exception as exc:  # pragma: no cover - observational only
        return {"error": str(exc)[:200]}


def generate_lock(retrieved_at: dict[str, str] | None = None) -> dict:
    """Hash every artifact declared by pack recipes and write sources.lock.json."""
    old = {}
    if LOCK_PATH.exists():
        old = {s["source_id"]: s for s in json.loads(LOCK_PATH.read_text())["sources"]}
    sources = []
    for pid in all_pack_ids():
        recipe = load_recipe(pid)
        for a in recipe.get("artifacts", []):
            p = PROJECT_ROOT / a["local_path"]
            prev = old.get(a["source_id"], {})
            rec = {
                "source_id": a["source_id"], "pack_id": pid, "role": a["role"],
                "status": "available" if p.exists() else "not_available_locally",
                "sha256": sha256_file(p) if p.exists() else prev.get("sha256", "0" * 64),
                "size_bytes": p.stat().st_size if p.exists() else prev.get("size_bytes"),
                "media_type": a.get("media_type"), "url": a.get("url"), "resolved_from": a.get("resolved_from"),
                "retrieved_at": (retrieved_at or {}).get(a["source_id"]) or prev.get("retrieved_at") or
                                (_mtime_utc(p) if p.exists() else None),
                "http": _headers(p) if p.exists() else prev.get("http"),
                "local_path": a["local_path"], "edition_stamp": a.get("edition_stamp"),
                "observations": (_pdf_observations(p) if p.exists() and p.suffix.lower() == ".pdf"
                                 and a["role"] == "authoritative"
                                 else _html_observations(p, a.get("url")) if p.exists() and p.suffix == ".html"
                                 and a["role"] == "authoritative"
                                 else prev.get("observations", {})),
                "notes": a.get("notes"),
            }
            sources.append(rec)
        for a in recipe.get("known_unavailable", []):
            p = PROJECT_ROOT / a["local_path"]
            status = "not_available_locally"
            if p.exists():
                status = "available" if sha256_file(p) == a["sha256"] else "not_available_locally"
            sources.append({
                "source_id": a["source_id"], "pack_id": pid, "role": a["role"], "status": status,
                "sha256": a["sha256"], "size_bytes": a.get("size_bytes"), "media_type": "application/pdf",
                "url": None, "resolved_from": None, "retrieved_at": None, "http": None,
                "local_path": a["local_path"], "edition_stamp": a.get("edition_stamp"), "observations": {},
                "notes": a.get("notes"),
            })
    lock = {"lock_version": 1,
            "notes": "Pinned source identities. Hashes prove byte identity only; rights and correctness are "
                     "recorded separately. Paths are relative to the project root; input/ is not tracked.",
            "sources": sources}
    write_json(LOCK_PATH, lock)
    return lock


def load_lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def resolve(source_id: str, verify: bool = True) -> dict:
    lock = load_lock()
    rec = next((s for s in lock["sources"] if s["source_id"] == source_id), None)
    if rec is None:
        raise KeyError(f"source {source_id} is not registered in sources.lock.json")
    path = PROJECT_ROOT / rec["local_path"]
    if not path.exists():
        raise FileNotFoundError(f"{source_id}: expected at {rec['local_path']} (sha256 {rec['sha256']}); not present")
    if verify:
        actual = sha256_file(path)
        if actual != rec["sha256"]:
            raise ValueError(f"{source_id}: hash mismatch at {rec['local_path']}: lock {rec['sha256']}, file {actual}. "
                             "Register the file as a new source identity instead of overwriting the lock.")
    return {**rec, "path": str(path)}


# Roles whose bytes a build reads. Any other capture (landing pages, reference copies, supplements) records what was
# seen at retrieval; some servers inject per-request tokens into pages, so a re-fetch can legitimately differ.
BUILD_INPUT_ROLES = {"authoritative", "structured_candidate"}


def verify_all(pack_id: str | None = None) -> list[dict]:
    """Hash every locked source present under input/.

    Statuses: ``ok``; ``MISMATCH`` (a build input differs: stop); ``changed`` (a non-input capture differs;
    informational); ``absent`` (not fetched yet); ``not_supplied`` (registered identity that was never supplied,
    e.g. a reader's own copy; not needed for the build). ``blocking`` is true only for build-input problems.
    """
    out = []
    for s in load_lock()["sources"]:
        if pack_id and s["pack_id"] != pack_id:
            continue
        p = PROJECT_ROOT / s["local_path"]
        is_input = s["role"] in BUILD_INPUT_ROLES
        rec = {"source_id": s["source_id"], "role": s["role"], "build_input": is_input}
        if not p.exists():
            supplied = s.get("status") != "not_available_locally"
            rec.update(status="absent" if supplied else "not_supplied", blocking=is_input and supplied)
        else:
            actual = sha256_file(p)
            if actual == s["sha256"]:
                rec.update(status="ok", blocking=False)
            else:
                rec.update(status="MISMATCH" if is_input else "changed", blocking=is_input, sha256=actual)
        out.append(rec)
    return out


def fetch(pack_id: str, force: bool = False, jobs: int = 8) -> list[dict]:
    """Opt-in download of a pack's declared artifacts (uses curl; records headers; ``jobs`` downloads at a time)."""
    from concurrent.futures import ThreadPoolExecutor
    recipe = load_recipe(pack_id)

    def one(a: dict) -> dict:
        dest = PROJECT_ROOT / a["local_path"]
        if dest.exists() and not force:
            return {"source_id": a["source_id"], "status": "present"}
        if a.get("download") is False:  # the URL is a landing page, not the file: the maintainer supplies the bytes
            return {"source_id": a["source_id"], "status": "supply_manually", "local_path": a["local_path"],
                    "url": a["url"]}
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        cmd = ["curl", "-sS", "-L", "--fail", "--retry", "3", "-o", str(tmp), "-D", str(dest) + ".headers", a["url"]]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return {"source_id": a["source_id"], "status": "error", "detail": r.stderr.strip()[:300]}
        tmp.replace(dest)
        return {"source_id": a["source_id"], "status": "downloaded", "retrieved_at": utc_now(),
                "sha256": sha256_file(dest)}
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        return list(ex.map(one, [a for a in recipe.get("artifacts", []) if a.get("url")]))
