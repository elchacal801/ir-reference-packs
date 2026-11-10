"""Local and public-candidate archives (``irpacks package``).

* ``dist/archives/ir-reference-packs-local-<version>.tar.gz`` holds the complete local builds
  (``build/local/<pack>/``) plus the lock and schemas. It is **for the maintainer's own use and is never
  distributed**: it contains everything the local policy allows, including material not cleared for public
  release. It never contains the input PDFs, HTML or EPUB (``input/`` is excluded).
* ``dist/archives/ir-reference-packs-public-<version>.tar.gz`` holds only the validated public candidates
  (``dist/public/packs/<pack>/``) plus LICENSE, NOTICE, schemas and the lock. Before writing it, every member is
  checked again: file types on the allowlist, no source documents, no ``input/`` paths. Each pack must have passed
  its public export checks.

Archives are deterministic: sorted members, fixed mtime (``SOURCE_DATE_EPOCH`` or the newest pack review date),
uid/gid 0, and a gzip header without timestamp or name. ``SHA256SUMS`` and a JSON manifest are written next to them.
Packaging does not publish anything.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import yaml

from .public import ALLOWED_SUFFIXES
from .util import PROJECT_ROOT, sha256_file, utc_now, write_json

ARCHIVES = PROJECT_ROOT / "dist" / "archives"
ROOT_FILES = ["LICENSE", "NOTICE", "README.md", "sources.lock.json"]


def _epoch() -> int:
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    dates = []
    for p in (PROJECT_ROOT / "packs").glob("*/pack.yaml"):
        d = (yaml.safe_load(p.read_text(encoding="utf-8")).get("review") or {}).get("date")
        if d:
            dates.append(str(d))
    day = max(dates) if dates else "2026-01-01"
    return int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp())


def _members(base: Path, prefix: str, skip=lambda p: False) -> list[tuple[str, Path]]:
    return [(f"{prefix}/{p.relative_to(base).as_posix()}", p) for p in sorted(base.rglob("*"))
            if p.is_file() and not skip(p)]


def _write_tar(dest: Path, members: list[tuple[str, Path]], epoch: int) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, path in sorted(members):
            info = tarfile.TarInfo(name)
            info.size = path.stat().st_size
            info.mtime = epoch
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
    with open(dest, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        gz.write(buf.getvalue())
    shown = dest.relative_to(PROJECT_ROOT).as_posix() if dest.is_relative_to(PROJECT_ROOT) else str(dest)
    return {"path": shown, "sha256": sha256_file(dest),
            "size_bytes": dest.stat().st_size, "members": len(members)}


def _common(prefix: str) -> list[tuple[str, Path]]:
    out = [(f"{prefix}/{f}", PROJECT_ROOT / f) for f in ROOT_FILES if (PROJECT_ROOT / f).exists()]
    out += _members(PROJECT_ROOT / "schemas", f"{prefix}/schemas")
    return out


def check_public_members(members: list[tuple[str, Path]]) -> list[str]:
    problems = []
    for name, _ in members:
        suffix = Path(name).suffix.lower()
        base = Path(name).name
        if suffix in (".pdf", ".epub", ".html", ".htm"):
            problems.append(f"source document in public archive: {name}")
        elif suffix not in ALLOWED_SUFFIXES and base not in ("LICENSE", "NOTICE"):
            problems.append(f"file type not on the public allowlist: {name}")
        if "/input/" in f"/{name}" or "/build/local/" in f"/{name}":
            problems.append(f"local-only path in public archive: {name}")
    return problems


def package_all(version: str | None = None) -> dict:
    from . import __version__
    version = version or __version__
    epoch = _epoch()
    local_dirs = sorted(p for p in (PROJECT_ROOT / "build" / "local").iterdir()
                        if (p / "manifest.json").exists() and not p.name.endswith("-pilot"))
    public_dirs = sorted(p for p in (PROJECT_ROOT / "dist" / "public" / "packs").iterdir()
                         if (p / "manifest.json").exists()) if (PROJECT_ROOT / "dist" / "public" / "packs").exists() else []
    result: dict = {"created_at": utc_now(), "version": version, "source_date_epoch": epoch, "archives": []}

    pref = f"ir-reference-packs-local-{version}"
    local = _common(pref)
    for d in local_dirs:
        local += _members(d, f"{pref}/packs/{d.name}")
    rec = _write_tar(ARCHIVES / f"{pref}.tar.gz", local, epoch)
    rec.update(kind="local", distribution="do not distribute: local build, may include material not cleared for "
                                          "public release; contains no input PDF/HTML/EPUB",
               packs=[d.name for d in local_dirs])
    result["archives"].append(rec)

    pref = f"ir-reference-packs-public-{version}"
    public = _common(pref)
    blocked = []
    for d in public_dirs:
        qa = d / "qa" / "validation.json"
        ok = qa.exists() and json.loads(qa.read_text(encoding="utf-8")).get("passed") is True
        if not ok:
            blocked.append(d.name)
            continue
        public += _members(d, f"{pref}/packs/{d.name}")
    problems = check_public_members(public)
    if problems or not public_dirs:
        result["archives"].append({"kind": "public_candidate", "written": False,
                                   "problems": problems or ["no public exports; run `irpacks export-public PACK`"]})
    else:
        rec = _write_tar(ARCHIVES / f"{pref}.tar.gz", public, epoch)
        rec.update(kind="public_candidate", written=True, packs=[d.name for d in public_dirs if d.name not in blocked],
                   blocked_packs=blocked,
                   distribution="candidate only: not published; see each pack's PUBLIC_DISTRIBUTION_REPORT.md")
        result["archives"].append(rec)
    sums = [f"{a['sha256']}  {Path(a['path']).name}" for a in result["archives"] if a.get("sha256")]
    (ARCHIVES / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    write_json(ARCHIVES / "archives.json", result)
    return result
