"""Refresh a web-article pack (``irpacks refresh PACK``): capture today's pages, build them as a *candidate* next to
the current build, and report what changed.

Nothing is adopted unless asked:
* the tracked recipe, pack contract and lock change only with ``--adopt``;
* ``build/local/<pack>`` is never overwritten;
* the candidate is written to ``build/candidates/<pack>-<date>/``;
* the change report goes to ``build/updates/<pack>-<date>-changes.{md,json}``.

Adoption rewrites the snapshot date in the recipe (source IDs, local paths) and in the pack contract (edition,
edition identifier, retrieval date, inputs), then regenerates the lock. The maintainer reviews the git diff and
rebuilds.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import diffdoc, packbuild, sources
from .util import PROJECT_ROOT, sha256_file, utc_now, write_json

Runner = Callable[[list[str]], tuple[int, str]]


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)


def refresh(pack_id: str, *, date: str | None = None, runner: Runner | None = None, adopt: bool = False,
            log=print, jobs: int = 8) -> dict:
    run = runner or _run
    pack = packbuild.validate_pack_contract(pack_id)
    pdir = sources.pack_dir(pack_id)
    recipe = sources.load_recipe(pack_id, pack)
    if recipe["route"] not in packbuild.WEB_ROUTES:
        raise SystemExit(f"{pack_id}: refresh applies to web-article packs; for documents use `irpacks check-updates`")
    rights_cfg = sources.load_yaml(pdir / pack["rights"])
    old_snap = recipe["inputs"]["snapshot"]
    date = date or dt.date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[a-z0-9-]*", date):
        raise SystemExit("--date must look like YYYY-MM-DD (optionally with a suffix, e.g. 2026-11-02b)")
    if date == old_snap:
        raise SystemExit(f"{pack_id} is already at snapshot {date}; pass another --date label")
    current = packbuild.BUILD_LOCAL / pack_id
    history = PROJECT_ROOT / "history" / pack_id / f"{pack['source'].get('edition_identifier') or old_snap}.json"
    if not (current / "document.json").exists() and not history.exists():
        raise SystemExit(f"build the current snapshot first (uv run irpacks build {pack_id}), or record its history")

    new_recipe = copy.deepcopy(recipe)
    new_recipe["inputs"]["snapshot"] = date
    todo = []
    for a, na in zip(recipe["artifacts"], new_recipe["artifacts"]):
        if a["role"] != "authoritative":
            continue
        na["source_id"] = a["source_id"].replace(old_snap, date)
        na["local_path"] = a["local_path"].replace(f"/{old_snap}/", f"/{date}/")
        todo.append((a, na))

    def capture(pair):
        a, na = pair
        dest = PROJECT_ROOT / na["local_path"]
        if dest.exists():
            return False, ""
        dest.parent.mkdir(parents=True, exist_ok=True)
        rc, out = run(["curl", "-sS", "-L", "--fail", "--retry", "3", "--max-time", "120", "-o", str(dest),
                       "-D", str(dest) + ".headers", a["url"]])
        if rc != 0 or not dest.exists():
            return False, f"{a['url']}: download failed: {(out or '').strip()[-200:]}"
        return True, ""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        got = list(ex.map(capture, todo))
    failed = [err for _, err in got if err]
    if failed:
        raise SystemExit("\n".join(failed[:20]))
    fetched = [na["local_path"] for (a, na), (new, _) in zip(todo, got) if new]
    arts = []
    for a, na in todo:
        dest = PROJECT_ROOT / na["local_path"]
        arts.append({**na, "source_id": na["source_id"], "unit_id": a["unit_id"], "url": a["url"], "path": str(dest),
                     "sha256": sha256_file(dest), "size_bytes": dest.stat().st_size, "local_path": na["local_path"],
                     "retrieved_at": utc_now()})
    out_dir = PROJECT_ROOT / "build" / "candidates" / f"{pack_id}-{date}"
    res = packbuild._build_web(pack_id, pack, new_recipe, rights_cfg, pdir, time.time(), log, arts=arts, out_dir=out_dir)
    if (current / "document.json").exists():  # wording-level comparison with the current build
        report = diffdoc.diff_documents(diffdoc.load_document(current), diffdoc.load_document(out_dir))
        md = diffdoc.render_markdown(report)
    else:  # e.g. a scheduled run on a fresh clone: compare with the tracked, text-free history record
        report = diffdoc.diff_fingerprints(json.loads(history.read_text(encoding="utf-8")), diffdoc.fingerprint(out_dir))
        report["basis"] = f"history record {history.relative_to(PROJECT_ROOT).as_posix()} (section hashes; no wording)"
        report["summary"] = {"units_with_changes": len(report["units"]), "units_added": len(report["units_added"]),
                             "units_removed": len(report["units_removed"])}
        md = diffdoc.render_fingerprint_markdown(report)
    upd = PROJECT_ROOT / "build" / "updates"
    write_json(upd / f"{pack_id}-{date}-changes.json", report)
    (upd / f"{pack_id}-{date}-changes.md").write_text(md, encoding="utf-8")
    result = {"pack_id": pack_id, "from_snapshot": old_snap, "to_snapshot": date, "downloaded": len(fetched),
              "candidate": out_dir.relative_to(PROJECT_ROOT).as_posix(),
              "candidate_validation_passed": res["validation"]["passed"], "summary": report["summary"],
              "report": f"build/updates/{pack_id}-{date}-changes.md", "adopted": False}
    if adopt:
        if not res["validation"]["passed"]:
            raise SystemExit("candidate failed validation; not adopting")
        _adopt(pdir, pack, old_snap, date)
        sources.generate_lock()
        result["adopted"] = True
        result["next"] = f"review `git diff`, then: uv run irpacks build {pack_id} && uv run irpacks snapshot {pack_id}"
    return result


def _adopt(pdir: Path, pack: dict, old: str, new: str) -> None:
    rp = pdir / pack["recipe"]
    rp.write_text(rp.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    af = sources.load_yaml(rp).get("artifacts_file")
    if af and (pdir / af).exists():  # the generated article list carries the snapshot in IDs and paths
        (pdir / af).write_text((pdir / af).read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    pp = pdir / "pack.yaml"
    lines = []
    for ln in pp.read_text(encoding="utf-8").split("\n"):
        if re.match(r"\s*(edition|edition_identifier|retrieval_date|- source_id):", ln):
            ln = ln.replace(old, new)
        lines.append(ln)
    pp.write_text("\n".join(lines), encoding="utf-8")
