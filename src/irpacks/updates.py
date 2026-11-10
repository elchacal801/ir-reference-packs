"""Opt-in upstream update check (``irpacks check-updates PACK [--download]``).

Never runs during build, validation, search or CI (the CI workflow is offline; the update workflow is
``workflow_dispatch`` only). For each locked source with a URL it requests the response headers (HEAD, redirects
followed) and compares ETag, Last-Modified and Content-Length with the values recorded at retrieval.

* ``unchanged``: every validator the server returned matches the lock;
* ``changed``: at least one validator differs; the upstream file may be a new edition or a re-upload;
* ``unknown``: the server returned no comparable validators;
* ``error``: the request failed. The lock is unaffected.

With ``--download`` a changed artifact is fetched into ``input/<pack>/candidates/<date>/``, hashed and described.
A *proposed* new source identity is written to ``build/updates/``. The lock, recipes and builds are never modified:
adopting an update is a reviewed change (new source_id, rebuild, regression comparison; see
docs/update-workflow.md). A hash change on its own does not show which content changed.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path
from typing import Callable

from .sources import load_lock
from .util import PROJECT_ROOT, sha256_file, utc_now, write_json

VALIDATORS = ("etag", "last-modified", "content-length")
Runner = Callable[[list[str]], tuple[int, str]]


def _default_runner(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)


def parse_headers(raw: str) -> dict[str, str]:
    """Headers of the final response in a curl -I -L / -D - transcript (proxy CONNECT blocks skipped).
    The status code is returned under the pseudo-header ":status"."""
    blocks = [b for b in re.split(r"\r?\n\r?\n", raw.strip()) if b.strip().upper().startswith("HTTP/")]
    blocks = [b for b in blocks if "connection established" not in b.splitlines()[0].lower()]
    if not blocks:
        return {}
    lines = blocks[-1].splitlines()
    m = re.match(r"HTTP/\S+\s+(\d{3})", lines[0])
    out = {":status": m.group(1) if m else ""}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    if out[":status"] == "206" and "content-range" in out:  # range probe: total size from Content-Range
        total = out["content-range"].rsplit("/", 1)[-1]
        if total.isdigit():
            out["content-length"] = total
    return out


def probe(url: str, run: "Runner") -> tuple[str, dict[str, str], str]:
    """HEAD first; if the server rejects HEAD (some answer 403/404/405), fall back to a 1-byte range GET."""
    rc, out = run(["curl", "-sS", "-I", "-L", "--max-time", "60", url])
    h = parse_headers(out) if rc == 0 else {}
    if h.get(":status", "").startswith("2"):
        return "HEAD", h, ""
    rc2, out2 = run(["curl", "-sS", "-L", "--max-time", "60", "-r", "0-0", "-D", "-", "-o", "/dev/null", url])
    h2 = parse_headers(out2) if rc2 == 0 else {}
    if h2.get(":status", "").startswith("2"):
        return "GET bytes=0-0", h2, ""
    return "", {}, (f"HEAD status {h.get(':status') or rc}; range GET status {h2.get(':status') or rc2}: "
                    + (out2 or out).strip()[-200:])


def compare(locked: dict | None, live: dict[str, str]) -> tuple[str, list[dict]]:
    locked = locked or {}
    diffs, compared = [], 0
    for k in VALIDATORS:
        if k in live and locked.get(k) is not None:
            compared += 1
            if live[k] != locked[k]:
                diffs.append({"header": k, "locked": locked[k], "upstream": live[k]})
    if diffs:
        return "changed", diffs
    return ("unchanged" if compared else "unknown"), diffs


def check_updates(pack_id: str, *, download: bool = False, runner: Runner | None = None,
                  out_dir: Path | None = None, jobs: int = 8) -> dict:
    run = runner or _default_runner
    lock = load_lock()
    entries = [s for s in lock["sources"] if s["pack_id"] == pack_id and s.get("url")]
    results = []
    articles = [s for s in entries if (s.get("observations") or {}).get("body_text_sha256")]
    if articles:  # captured web articles: compare their content (in parallel; one GET per article)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            results.extend(ex.map(lambda s: _check_article(s, run, download), articles))
    for s in entries:
        if (s.get("observations") or {}).get("body_text_sha256"):
            continue
        method, live, err = probe(s["url"], run)
        if not method:
            results.append({"source_id": s["source_id"], "url": s["url"], "status": "error", "detail": err})
            continue
        status, diffs = compare(s.get("http"), live)
        rec = {"source_id": s["source_id"], "url": s["url"], "status": status, "method": method,
               "differences": diffs,
               "upstream": {k: live.get(k) for k in VALIDATORS + ("content-type",) if k in live}}
        if download and status == "changed":
            rec["candidate"] = _download_candidate(s, run)
        results.append(rec)
    summary = {"checked_at": utc_now(), "pack_id": pack_id, "sources_checked": len(entries),
               "changed": [r["source_id"] for r in results if r["status"] in ("changed", "content_changed")],
               "metadata_only": [r["source_id"] for r in results if r["status"] == "metadata_changed"],
               "errors": [r["source_id"] for r in results if r["status"] == "error"],
               "results": results,
               **_toc_drift(pack_id, run),
               "actions_not_taken": ["sources.lock.json not modified", "recipes not modified", "no rebuild",
                                     "nothing published"]}
    dest = (out_dir or PROJECT_ROOT / "build" / "updates") / f"{pack_id}-{dt.date.today().isoformat()}.json"
    write_json(dest, summary)
    summary["report"] = str(dest.relative_to(PROJECT_ROOT)) if dest.is_relative_to(PROJECT_ROOT) else str(dest)
    return summary


def _toc_drift(pack_id: str, run: Runner) -> dict:
    """Articles added to or removed from the selected sections of the published TOCs (packs with discovery rules)."""
    try:
        from .discover import toc_drift
        return toc_drift(pack_id, run)
    except (OSError, ValueError, KeyError) as exc:  # pragma: no cover - reported, never fatal
        return {"toc_errors": [{"detail": str(exc)[:200]}]}


def _check_article(s: dict, run: Runner, download: bool) -> dict:
    """Re-fetch a captured web article and compare what matters, not bytes: source commit, update time and a hash
    of the article body text. Page-template changes (scripts, navigation) do not count as content changes."""
    from .route_learn import observe
    day = dt.date.today().isoformat()
    dest = PROJECT_ROOT / "input" / s["pack_id"] / "candidates" / day / Path(s["local_path"]).parent.name / Path(s["local_path"]).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, out = run(["curl", "-sS", "-L", "--fail", "--max-time", "120", "-o", str(dest), "-D", str(dest) + ".headers",
                   s["url"]])
    if rc != 0 or not dest.exists():
        return {"source_id": s["source_id"], "url": s["url"], "status": "error", "detail": (out or "").strip()[-300:]}
    try:
        live = observe(dest, s["url"])
    except ValueError as exc:
        return {"source_id": s["source_id"], "url": s["url"], "status": "error", "detail": f"not an article page: {exc}"}
    old = s["observations"]
    diffs = [{"field": k, "locked": old.get(k), "upstream": live.get(k)}
             for k in ("git_commit_id", "updated_at", "ms_date", "title", "body_text_sha256") if old.get(k) != live.get(k)]
    fields = {d["field"] for d in diffs}
    status = ("content_changed" if "body_text_sha256" in fields or "title" in fields
              else "metadata_changed" if fields else
              "unchanged" if sha256_file(dest) == s["sha256"] else "page_template_changed")
    rec = {"source_id": s["source_id"], "url": s["url"], "status": status, "method": "GET + article fingerprint",
           "differences": diffs, "upstream": {k: live.get(k) for k in ("git_commit_id", "updated_at", "ms_date")}}
    if download and status != "unchanged":
        rec["candidate"] = {"status": "downloaded", "local_path": dest.relative_to(PROJECT_ROOT).as_posix(),
                            "sha256": sha256_file(dest),
                            "note": "Build and diff it with `irpacks refresh <pack>`; nothing was adopted."}
    else:
        dest.unlink(missing_ok=True)
        Path(str(dest) + ".headers").unlink(missing_ok=True)
    return rec


def _download_candidate(s: dict, run: Runner) -> dict:
    day = dt.date.today().isoformat()
    dest = PROJECT_ROOT / "input" / s["pack_id"] / "candidates" / day / Path(s["local_path"]).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, out = run(["curl", "-sS", "-L", "--fail", "--max-time", "600", "-o", str(dest), "-D", str(dest) + ".headers",
                   s["url"]])
    if rc != 0 or not dest.exists():
        return {"status": "error", "detail": out.strip()[-300:]}
    sha = sha256_file(dest)
    return {"status": "downloaded", "local_path": dest.relative_to(PROJECT_ROOT).as_posix(), "sha256": sha,
            "same_bytes_as_lock": sha == s["sha256"], "size_bytes": dest.stat().st_size,
            "proposed_source_id": f"{s['source_id'].rsplit('/', 1)[0]}/candidate-{day}",
            "note": "Register as a NEW source identity after review; never overwrite the locked one."}
