"""Article discovery for web packs (``irpacks sources discover PACK``): Microsoft Learn tables of contents -> the
pinned article list.

A recipe's ``discovery`` block declares, per docset, which part of its published table of contents (``toc.json``)
belongs in the pack:

    discovery:
      base: https://learn.microsoft.com/en-us/
      rules:
        - toc: defender-endpoint                # fetches <base>defender-endpoint/toc.json
          prefix: mde                           # unit IDs become mde-<slug>
          include: ['> Investigate and respond to threats( >|$)']   # regexes over the TOC path "A > B > C"
          exclude: ['Copilot']
          url_prefix: https://learn.microsoft.com/en-us/defender-endpoint/   # default: the TOC's directory
      pinned: [...]                             # articles outside any TOC rule, kept as listed

Discovery captures every matching page into ``input/<pack>/<snapshot>/`` (opt-in network use, like
``sources fetch``), keeps the pages that are articles, and writes ``packs/<pack>/articles.yaml``:
* ``artifacts``: one locked input per article. Existing unit IDs are kept for URLs already listed, so element IDs,
  evaluation evidence and history stay stable.
* ``excluded``: TOC entries that are not articles (landing pages, external links, other packs' articles), each
  with a reason.

Nothing is built and the lock is not regenerated; review the diff, then ``irpacks sources lock``. The TOC is data:
titles and links are matched as text, never followed beyond the listed host.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml

from . import sources
from .util import PROJECT_ROOT, utc_now

Runner = Callable[[list[str]], tuple[int, str]]
NOT_ARTICLE_TOPICS = {"landing-page", "hub-page", "overview-page"}
# Where each site publishes its table of contents. Google DevSite has no TOC file: the rule's ``toc`` names a page,
# and that page's side navigation (``devsite-book-nav``) is read as the TOC.
TOC_FILES = {"learn.microsoft.com": "toc.json", "docs.aws.amazon.com": "toc-contents.json", "docs.cloud.google.com": None}
DOC_HOSTS = set(TOC_FILES) | {"www.first.org"}  # documentation hosts; first.org publishes no TOC (pinned pages only)


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout)


def fetch_page(url: str, dest: Path, run: Runner) -> tuple[bool, str, str]:
    """Download one page with its headers; returns (ok, final URL after redirects, detail)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, out = run(["curl", "-sS", "-L", "--fail", "--retry", "3", "--max-time", "120", "-o", str(dest),
                   "-D", str(dest) + ".headers", "-w", "%{url_effective}", url])
    if rc != 0 or not dest.exists():
        return False, url, (out or "").strip()[-200:]
    return True, _canonical(out.strip() or url) or url, ""


def _canonical(url: str, host: str | None = None) -> str | None:
    """The page URL without query, fragment or trailing slash (Learn: with the en-us locale); None when the link
    leaves the documentation host (``host``, default: any of the known documentation hosts)."""
    s = urlsplit(url)
    if s.scheme not in ("http", "https") or s.hostname not in ({host} if host else DOC_HOSTS):
        return None
    path = s.path
    if s.hostname == "learn.microsoft.com" and not re.match(r"^/[a-z]{2}-[a-z]{2}/", path):
        path = "/en-us" + path
    return urlunsplit(("https", s.hostname, path.rstrip("/"), "", ""))


def toc_entries(toc: dict) -> list[tuple[str, str]]:
    """(TOC path 'A > B > C', href) for every entry with a link, in TOC order."""
    out: list[tuple[str, str]] = []

    def walk(items, path):
        for x in items:
            p = path + [str(x.get("toc_title") or x.get("title") or x.get("name") or "").strip()]
            if x.get("href"):
                out.append((" > ".join(p), x["href"]))
            walk(x.get("children") or x.get("contents") or [], p)
    walk(toc.get("items") or toc.get("contents") or [], [])  # Learn: items/children; AWS: contents/contents
    return out


def devsite_nav_entries(html: str) -> list[tuple[str, str]]:
    """(TOC path, href) for every linked entry of a DevSite page's side navigation, in navigation order. Each item's
    own title is its ``.devsite-nav-text`` (group items have a title but no link)."""
    import lxml.html
    nav = lxml.html.fromstring(html).xpath("//devsite-book-nav")
    if not nav:
        return []

    def own(li, xp):  # descendants whose nearest <li> is this item
        return [e for e in li.xpath(xp) if next(iter(e.iterancestors("li")), None) is li]
    out: list[tuple[str, str]] = []

    def walk(items, path):
        for li in items:
            t = own(li, './/*[contains(@class, "devsite-nav-text")]')
            title = " ".join(t[0].text_content().split()) if t else ""
            a = own(li, './/a[@href][contains(@class, "devsite-nav-title")]')
            p = path + [title] if title else path
            if a and title:
                out.append((" > ".join(p), a[0].get("href")))
            walk(own(li, ".//li"), p)
    walk([li for li in nav[0].xpath(".//li") if not list(li.iterancestors("li"))], [])
    return out


def load_toc(rule: dict, base: str, cache_name: str, run: Runner) -> tuple[str, list[tuple[str, str]] | None, str]:
    """Fetch one rule's table of contents: (TOC URL, entries or None on failure, failure detail)."""
    host = urlsplit(base).hostname
    toc_file = rule.get("toc_file") or TOC_FILES.get(host, "toc.json")
    toc_url = f"{base}{rule['toc']}/{toc_file}" if toc_file else f"{base}{rule['toc']}"
    tmp = PROJECT_ROOT / "build" / "cache" / "toc" / (cache_name + (".json" if toc_file else ".html"))
    ok, _, detail = fetch_page(toc_url, tmp, run)
    if not ok:
        return toc_url, None, detail
    text = tmp.read_text(encoding="utf-8")
    return toc_url, (toc_entries(json.loads(text)) if toc_file else devsite_nav_entries(text)), ""


def select(entries: list[tuple[str, str]], rule: dict, toc_url: str) -> list[tuple[str, str | None, str]]:
    """Apply one rule: (TOC path, canonical URL or None, reason when skipped) for every entry the rule includes."""
    inc = [re.compile(p, re.I) for p in rule.get("include", [".*"])]
    exc = [re.compile(p, re.I) for p in rule.get("exclude", [])]
    prefix = rule.get("url_prefix") or toc_url.rsplit("/", 1)[0] + "/"
    out = []
    host = urlsplit(toc_url).hostname
    for path, href in entries:
        if not any(r.search(path) for r in inc) or any(r.search(path) for r in exc):
            continue
        url = _canonical(urljoin(toc_url, href), host)
        if url is None:
            out.append((path, None, "external link"))
        elif not (url + "/").startswith(prefix):
            out.append((path, url, f"outside {prefix}"))
        elif url + "/" == prefix:
            out.append((path, url, "docset landing page"))
        else:
            out.append((path, url, ""))
    return out


def _slug(url: str) -> str:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    last = re.sub(r"\.html?$", "", segs[-1].lower()) if segs else "page"
    return re.sub(r"[^a-z0-9]+", "-", last).strip("-")[:70] or "page"


def _classify(path: Path, url: str, route: str) -> str:
    """'' for an article page; otherwise why the page is not one."""
    from .route_learn import PROFILES, _Article, meta_for
    import lxml.html
    html = path.read_text(encoding="utf-8", errors="replace")
    prof = PROFILES[route]
    pm = meta_for(html, prof)
    if (pm.get("ms_topic") or "").lower() in NOT_ARTICLE_TOPICS:
        return f"not an article (ms.topic {pm['ms_topic']})"
    try:
        _Article("x", url, lxml.html.fromstring(html), pm, "x", prof)
    except ValueError:
        return "not an article page (no article body)"
    return ""


def discover(pack_id: str, *, runner: Runner | None = None, jobs: int = 8, log=print) -> dict:
    run = runner or _run
    pdir = sources.pack_dir(pack_id)
    pack = sources.load_yaml(pdir / "pack.yaml")
    recipe = sources.load_yaml(pdir / pack["recipe"])
    disc = recipe.get("discovery")
    if not disc:
        raise SystemExit(f"{pack_id}: recipe has no discovery block")
    snapshot = recipe["inputs"]["snapshot"]
    base = disc.get("base", "https://learn.microsoft.com/en-us/")
    af = pdir / recipe.get("artifacts_file", "articles.yaml")
    current = sources.load_yaml(af).get("artifacts", []) if af.exists() else list(recipe.get("artifacts", []))
    by_url = {a["url"]: a for a in current}
    claimed = {}  # URLs that belong to other packs
    for pid in sources.all_pack_ids():
        if pid != pack_id:
            for a in sources.load_recipe(pid).get("artifacts", []):
                if a.get("url"):
                    claimed[_canonical(a["url"]) or a["url"]] = pid

    wanted: list[dict] = []
    excluded: list[dict] = []
    seen: set[str] = set()
    for rule in disc.get("rules", []):
        toc_url, entries, detail = load_toc(rule, base, rule["toc"].replace("/", "_"), run)
        if entries is None:
            raise SystemExit(f"{toc_url}: {detail}")
        picked = select(entries, rule, toc_url)
        log(f"  {rule['toc']}: {len(entries)} TOC entries, {len(picked)} selected")
        for path, url, reason in picked:
            if reason:
                excluded.append({"toc": rule["toc"], "toc_path": path, "url": url, "reason": reason})
            elif url in claimed:
                excluded.append({"toc": rule["toc"], "toc_path": path, "url": url, "reason": f"in pack {claimed[url]}"})
            elif url not in seen:
                seen.add(url)
                wanted.append({"url": url, "toc": rule["toc"], "toc_path": path, "prefix": rule["prefix"],
                               "docset": rule.get("docset", rule["toc"].split("/")[-1])})
    for p in disc.get("pinned", []):
        url = _canonical(p["url"])
        if url and url not in seen:
            seen.add(url)
            wanted.append({"url": url, "toc": None, "toc_path": None, "prefix": p.get("prefix", "pinned"),
                           "docset": p.get("docset", "pinned"), "unit_id": p.get("unit_id")})

    # an article the site moved keeps its unit ID: a listed URL that no TOC or pin selects any more is followed, and
    # if it now redirects to a selected URL, that URL inherits the old entry
    moved = {}
    picked_urls = {w["url"] for w in wanted}
    orphans = [a for a in current if a["url"] not in picked_urls and a.get("redirected_from") not in picked_urls]
    with tempfile.TemporaryDirectory() as tmp:
        def follow(a):
            return a, *fetch_page(a["url"], Path(tmp) / f"{a['unit_id']}.html", run)
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for a, ok, final, _ in ex.map(follow, orphans):
                if ok and final != a["url"] and final in picked_urls and final not in by_url:
                    by_url[final] = a
                    moved[a["url"]] = final

    # unit IDs: keep existing ones; new ones are <prefix>-<last path segment>, widened on collision
    used = {a["unit_id"] for a in current}
    for w in wanted:
        old = by_url.get(w["url"])
        if old:
            w.update(unit_id=old["unit_id"], source_id=old["source_id"], local_path=old["local_path"])
            continue
        uid = w.get("unit_id") or f"{w['prefix']}-{_slug(w['url'])}"
        n = 2
        while uid in used:
            uid = f"{w['prefix']}-{_slug(w['url'])}-{n}"
            n += 1
        used.add(uid)
        slug = uid[len(w["prefix"]) + 1:]
        w.update(unit_id=uid, source_id=f"{pack_id}/{w['docset']}/{slug}/{snapshot}",
                 local_path=f"input/{pack_id}/{snapshot}/{w['docset']}/{slug}.html")

    def capture(w):
        dest = PROJECT_ROOT / w["local_path"]
        if dest.exists():
            return w, True, w["url"], ""
        return (w, *fetch_page(w["url"], dest, run))
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(capture, wanted))

    artifacts, final_seen = [], set()
    for w, ok, final, detail in results:
        if not ok:
            excluded.append({"toc": w["toc"], "toc_path": w["toc_path"], "url": w["url"], "reason": f"download failed: {detail}"})
            continue
        reason = _classify(PROJECT_ROOT / w["local_path"], w["url"], recipe["route"])
        if not reason and final != w["url"]:
            reason = f"redirects to {final}" if final in seen or final in claimed or final in final_seen else ""
            if not reason:
                w["redirected_from"], w["url"] = w["url"], final
        if reason:
            excluded.append({"toc": w["toc"], "toc_path": w["toc_path"], "url": w["url"], "reason": reason})
            (PROJECT_ROOT / w["local_path"]).unlink(missing_ok=True)
            Path(str(PROJECT_ROOT / w["local_path"]) + ".headers").unlink(missing_ok=True)
            continue
        final_seen.add(w["url"])
        art = {"source_id": w["source_id"], "role": "authoritative", "unit_id": w["unit_id"], "url": w["url"],
               "local_path": w["local_path"], "media_type": "text/html"}
        if w.get("toc_path"):
            art["toc"] = f"{w['toc']}: {w['toc_path']}"
        if w.get("redirected_from"):
            art["redirected_from"] = w["redirected_from"]
        moved_from = next((o for o, n in moved.items() if n == w["url"]), None)
        if moved_from:
            art["moved_from"] = moved_from
        artifacts.append(art)
    out = {"generated_by": "irpacks sources discover", "generated_at": utc_now(), "snapshot": snapshot,
           "note": "Generated from the discovery rules in recipe.yaml; review changes before locking.",
           "artifacts": artifacts, "excluded": excluded}
    af.write_text("# GENERATED by `irpacks sources discover " + pack_id + "` — edit the discovery rules in recipe.yaml.\n"
                  + yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    added = [a["url"] for a in artifacts if a["url"] not in by_url]
    removed = [u for u in by_url if u not in {a["url"] for a in artifacts} and u not in moved]
    return {"pack_id": pack_id, "articles": len(artifacts), "excluded": len(excluded), "added": len(added),
            "removed": removed, "moved": moved, "file": af.relative_to(PROJECT_ROOT).as_posix()}


def toc_drift(pack_id: str, runner: Runner | None = None) -> dict:
    """Compare the published TOCs with the pinned article list (no page downloads): articles Microsoft added to or
    removed from the selected TOC sections since discovery. Used by ``check-updates``."""
    run = runner or _run
    pdir = sources.pack_dir(pack_id)
    recipe = sources.load_yaml(pdir / sources.load_yaml(pdir / "pack.yaml")["recipe"])
    disc = recipe.get("discovery")
    if not disc:
        return {}
    base = disc.get("base", "https://learn.microsoft.com/en-us/")
    pinned = sources.load_recipe(pack_id)
    known = {a["url"] for a in pinned.get("artifacts", [])} | {a.get("redirected_from") for a in pinned.get("artifacts", [])}
    known |= {e["url"] for e in pinned.get("excluded", []) if e.get("url")}
    listed: set[str] = set()
    new, errors = [], []
    for rule in disc.get("rules", []):
        toc_url, entries, detail = load_toc(rule, base, "check-" + rule["toc"].replace("/", "_"), run)
        if entries is None:
            errors.append({"toc": rule["toc"], "detail": detail})
            continue
        for path, url, reason in select(entries, rule, toc_url):
            if url and not reason:
                listed.add(url)
                if url not in known:
                    new.append({"toc": rule["toc"], "toc_path": path, "url": url})
    toc_rules = {r["toc"] for r in disc.get("rules", [])} - {e["toc"] for e in errors}
    gone = [a["url"] for a in pinned.get("artifacts", [])
            if (a.get("toc") or "").split(":")[0] in toc_rules and a["url"] not in listed
            and a.get("redirected_from") not in listed]
    return {"toc_new_articles": new, "toc_removed_articles": gone, "toc_errors": errors}
