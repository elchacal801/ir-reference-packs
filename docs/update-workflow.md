# Update workflow (new editions, errata, upstream changes)

Updates are **checked weekly and adopted only after review**. The weekly check only reports. Nothing downloads
sources implicitly during a build, and nothing changes the lock or the recipes automatically. See
[Scheduled checks](#scheduled-checks).

Two kinds of source change differently:

* **Documents (PDF packs: `dynamic-ir`, `nist-sp800-61r3`, `cisa-ir-playbooks`, `cisa-stopransomware`).** They
  change rarely and by edition. Check HTTP validators, then adopt a new edition by hand
  ([Documents](#documents-pdf-packs)).
* **Web articles (`ms-*`, `aws-security-ir`, `gcp-security-ir` and `first-standards`).** They change continuously,
  one article at a time, and articles are added to and removed from the docsets. Check each article's content and
  the published tables of contents, build a candidate, and read a section-level change report
  ([Web articles](#web-articles)).
* **PDF collections (`swgde-forensics`).** Each document is checked by its HTTP validators, like a PDF pack, and
  the collection is refreshed and diffed like a web pack ([PDF collections](#pdf-collections)).

Both kinds use the same comparison and history tools ([Comparing builds and history](#comparing-builds-and-history)).

## Documents (PDF packs)

### 1. Check

```bash
uv run irpacks check-updates dynamic-ir          # or: GitHub Actions → check-updates → Run workflow (manual only)
```

For every locked artifact with a URL, this requests the response headers. It sends HEAD first, then falls back to
a 1-byte range GET because some servers reject HEAD (NIST answers HEAD with 404). It then compares ETag,
Last-Modified and Content-Length with the lock, and writes `build/updates/<pack>-<date>.json`.

| Status | Meaning |
|---|---|
| `unchanged` | Every validator the server returned matches the lock. |
| `changed` | At least one validator differs. The upstream file may be a new edition or a re-upload; a hash change on its own does not show which content changed. |
| `unknown` | The server returned no comparable validators. For example, the CSRC landing page returns none. |
| `error` | The request failed. |

Last check (2026-10-25): every `dynamic-ir` artifact was `unchanged`. The NIST PDF was `unchanged`, via the range
GET. The CSRC page was `unknown`.

**CISA packs.** Both PDFs were supplied by the maintainer, so the lock has no HTTP validators for them.
* www.cisa.gov answers some automated clients with 403, the maintainer's cloud sessions among them. From there
  the checks report `error`, and so does the weekly check.
* Where cisa.gov is reachable, the checks report `unknown`, since there is nothing to compare with.
* For `cisa-stopransomware` the locked URL is the guide's landing page, because no direct file URL could be
  verified. A check there says nothing about the PDF itself.

To watch for a new revision, read the landing pages: the Playbooks resource page and the #StopRansomware Guide page
(revision date and version in the change record). When one appears, register the new PDF as a new source identity,
as in step 3 below.

### 2. Fetch a candidate (still no lock change)

```bash
uv run irpacks check-updates dynamic-ir --download
```

This downloads changed artifacts into `input/<pack>/candidates/<date>/` and records their sha256. It also proposes
a new `source_id`, such as `dynamic-ir/pdf/candidate-2026-11-02`, and writes the proposal to the report.

### 3. Review and adopt (a reviewed change)

1. **Identify the edition.** Read the edition stamp, copyright page and errata page, and compare page counts and
   bookmarks: `uv run irpacks inspect-pdf <file>`.
2. **Add the new source identity.** Add an artifact entry to `recipe.yaml` with the new `source_id` and
   `local_path`, and keep the old entry. If it is a new edition, update `pack.yaml`: `edition_identifier`,
   `retrieval_date`, the `superseded` block, and the pack version (a major or minor bump). Then run
   `uv run irpacks sources lock`.
3. **Rebuild and validate:** `uv run irpacks build <pack>`, which must give 0 errors. Investigate every new warning
   or issue in `qa/issues.jsonl`.
4. **Compare with the previous build:** `uv run irpacks diff history/<pack>/<old-edition>.json build/local/<pack>`
   (see below). Also compare chunk counts, the source-defects list and page coverage. Chunk IDs change with the
   edition by design, so downstream caches cannot mix editions.
5. **Visual review.** Render and view the pages whose elements changed, plus one page per chapter, and add the
   records to `review/visual-review.yaml`.
6. **Re-check the evaluation.** `uv run python eval/build_questions.py` fails if any evidence element or quote
   moved. Update the spec, then re-run both splits. Report changes against the previous results; do not retune on
   the held-out split.
7. **Rights.** Re-read the license and rights pages. A new edition can change the grant or its exceptions.
8. **Public candidate:** `uv run irpacks export-public <pack>`, then `uv run irpacks package`. Publishing remains
   a separate, explicit decision.
9. **History and changelog.** Run `uv run irpacks snapshot <pack>`, commit the new `history/` record, and record the
   new pack version, the source identities and what changed in the changelog.

## Web articles

The web packs are:
* `ms-learn-ir`, `ms-defender`, `ms-sentinel`, `ms-purview-exchange`, `ms-windows-auditing` and `ms-azure-ir`;
* `aws-security-ir`, `gcp-security-ir` and `first-standards`.

The examples below use `ms-learn-ir`; every command takes any of them.

### Which articles are in a pack: discovery

A web pack's article list is generated, not hand-written. The recipe's `discovery` block names the docsets'
published tables of contents and selects sections of them with regular expressions over the TOC path (for example
`> Investigate and respond to threats( >|$)`). The tables of contents are:
* Microsoft Learn: `toc.json`;
* AWS: `toc-contents.json`;
* Google Cloud: the DevSite navigation in the docset's pages.

FIRST publishes no table of contents for its standards. Its pages are listed as `pinned` entries, so TOC drift is
not reported for it; a new FIRST standard or version has to be added by hand.

```bash
uv run irpacks sources discover ms-defender   # fetch the TOCs, capture the selected pages, write articles.yaml
uv run irpacks sources lock                   # after reviewing the articles.yaml diff
```

* `articles.yaml` lists one locked input per article. An article that was already listed keeps its unit ID, so
  element IDs, evaluation evidence and history records stay stable.
* TOC entries that are not taken are listed with a reason: landing pages, links to another docset, links already
  in another pack, broken links, and redirects to an article that is already listed.
* An article belongs to one pack. Discovery skips URLs that another pack already lists.
* An article the site moved keeps its unit ID. When a listed URL is no longer selected, discovery follows it; if it
  now redirects to a URL the rules select, that URL inherits the entry and records `moved_from`. Microsoft moved the
  `ms-learn-ir` security operations guide into the Zero Trust docset this way in November 2026.

### 1. Has anything changed?

```bash
uv run irpacks check-updates ms-learn-ir
```

For each article, this downloads the page and reads the version metadata the site publishes in it. Microsoft
Learn publishes:
* `git_commit_id`;
* `updated_at`;
* `ms.date`;
* the title;
* a SHA-256 of the article body text.

AWS documentation and FIRST pages publish no commit or update time, so only the title and the body hash are
compared for them. Google Cloud pages publish a "Last updated" date, which is compared as the update time.

The check compares these with the observations recorded in `sources.lock.json`. Raw page bytes are not compared,
because a site template change alters every page without changing any article. The download is deleted afterwards.
With `--download`, a page that is not `unchanged` is kept under `input/<pack>/candidates/<date>/`.

For packs with discovery rules, the check also fetches the tables of contents (no pages). It reports:
* `new in TOC`: an article listed in a selected TOC section that is not in the pack. Run `sources discover` to
  add it.
* `no longer in TOC`: a pinned article that the selected TOC sections no longer list. It may have been retired or
  moved.

| Status | Meaning |
|---|---|
| `unchanged` | Same source commit and same article text. |
| `content_changed` | The article title or body text differs. The report lists which fields moved. |
| `metadata_changed` | The commit or update time moved, but the article text is identical. This is typical of a metadata-only commit; there is nothing to re-read. |
| `page_template_changed` | The page bytes differ but the article, commit and dates do not. Only the site template changed. |
| `error` | The download failed. |

The command prints a summary: counts by status, each source that changed, and the TOC drift. Pages that are only
`page_template_changed` are counted, not listed. The full report is written to `build/updates/<pack>-<date>.json`
(`--json` prints it). Nothing else changes.

FIRST pages carry new script nonces in every response, so each check reports all of them as
`page_template_changed`. Google Cloud pages did the same within hours of their capture. Both mean the article is
unchanged.

Last checks (2026-10-25): `ms-learn-ir` (16 articles at the time) and `ms-sentinel` (99 articles), all
`unchanged`. Same-day re-captures were byte-identical (16 Learn pages, and an AWS page spot-checked), so
`sources verify` should reproduce the lock until a page is edited. A `refresh` of `ms-sentinel` against its history
record reported 0 changed units.

### 2. What changed? Build a candidate and read the report

```bash
uv run irpacks refresh ms-learn-ir                      # snapshot label defaults to today's date
uv run irpacks refresh ms-learn-ir --date 2026-11-02    # or name it
```

This command:
1. captures every article into `input/ms-learn-ir/<date>/`;
2. builds and validates a **candidate** pack in `build/candidates/ms-learn-ir-<date>/`. The current build in
   `build/local/ms-learn-ir/` is not touched;
3. compares the candidate with the current build and writes `build/updates/ms-learn-ir-<date>-changes.md` (and
   `.json`). Without a local build (for example on a fresh clone, or in the scheduled check), it compares the
   candidate with the tracked history record `history/<pack>/<edition>.json` instead. That report names the units
   and sections that changed, but shows no wording diff, because history records hold hashes, not text.

The change report lists articles added or removed, and changed metadata (commit, update time, title). For each
article it then lists the sections added, removed and retitled, and every changed block with a word-level diff.
Each change has a severity, and the most important come first:

| Severity | Changes |
|---|---|
| `high` | A code block or table added, removed or changed; text added, removed or changed inside a warning, caution or important note; a step of a numbered procedure added, removed or changed; a removed section that contained code, a table or a warning. These are the changes most likely to alter what a responder does. |
| `medium` | Any other block (paragraph, list item, note, figure alt text) added, removed or changed; a section added; any other section removed. |
| `low` | A section retitled under the same anchor; a block whose words are unchanged (whitespace, punctuation or quote style only). |

Severity is a reading aid for the reviewer, not a judgement that a change is correct or important.

A real refresh on 2026-10-25 re-captured all 16 articles and reported 0 changes, which shows that the comparison
does not raise false positives on unchanged pages. The test suite exercises an actual change: a synthetic page in
Learn's markup whose second version changes a command, adds a warning to a step, removes one section and retitles
another (`tests/test_learn_changes.py`).

### 3. Adopt (a reviewed change)

1. Read the change report. For each `high` change, open the article and confirm it.
2. Re-check the evaluation questions whose evidence is in a changed section: `uv run python eval/build_questions.py
   --spec eval/questions-ms.spec.yaml` fails if a quote moved.
3. Adopt the snapshot:

   ```bash
   uv run irpacks refresh ms-learn-ir --date 2026-11-02 --adopt
   ```

   This requires the candidate to pass validation. It rewrites the snapshot date in `recipe.yaml` (source IDs and
   local paths) and `pack.yaml` (edition, edition identifier, retrieval date, inputs), and regenerates the lock.
4. Review `git diff`, then rebuild, record history and commit:

   ```bash
   uv run irpacks build ms-learn-ir && uv run irpacks snapshot ms-learn-ir
   ```

5. Re-check rights if an article moved to another docset or repository, and update the changelog.

## PDF collections

`swgde-forensics` holds 15 SWGDE documents, each a hash-pinned PDF with its document number, version and date.
* **Check.** `uv run irpacks check-updates swgde-forensics` compares each PDF's HTTP validators (Last-Modified,
  Content-Length) with the lock. SWGDE sends no ETag. Last check (2026-10-25): 15 `unchanged`.
* **A changed PDF.** SWGDE gives each version its own number suffix (for example `16-F-002-2.1`). The file
  URLs are upload paths (`…/wp-content/uploads/2026/02/…`), so a new version is likely to appear at a new URL
  while the old one keeps serving the old file, and the check would not see it. Also read the "published
  complete listing" pages linked from each artifact's `landing_page`, for new versions and new documents.
* **Refresh and diff.** `uv run irpacks refresh swgde-forensics` re-downloads every PDF, builds a candidate and
  writes a section-level change report, as for a web pack. A new version at a new URL is adopted by editing the
  artifact's `url`, `title`, `document_number`, `version` and `document_date` in `recipe.yaml`, then running
  `sources lock`, `build` and `snapshot`. The rights statement on the cover page must be re-read.

## Comparing builds and history

```bash
uv run irpacks diff build/candidates/ms-learn-ir-2026-11-02 build/local/ms-learn-ir   # two builds
uv run irpacks diff history/ms-learn-ir/2026-10-25.json build/local/ms-learn-ir         # a past edition and a build
uv run irpacks diff OLD NEW --out build/updates/my-report        # also writes my-report.md and my-report.json
uv run irpacks snapshot ms-learn-ir                               # history/ms-learn-ir/<edition>.json
```

`diff` takes build directories, `document.json` files, or history records. It compares canonical documents,
not Markdown, so formatting changes in an export never appear as content changes.

`history/<pack>/<edition>.json` is tracked in git. It holds, per unit, the identity metadata and, per section, a
content hash, so a later edition can be compared after the old input files are gone. It stores **no source text**.
For units not cleared for redistribution (`rights.state` other than `redistributable`), it also stores no titles,
and section IDs are replaced by their hashes, because a section ID derived from a heading would otherwise reveal
that heading. A history comparison therefore says *which* sections changed, but shows no wording diff. For the
wording, compare two builds.

Records so far: one per pack and edition.
* `dynamic-ir/20260917164224.json` and `nist-sp800-61r3/800-61r3.json`;
* `cisa-ir-playbooks/2021-11.json` and `cisa-stopransomware/v3.0-2023-10.json`;
* `<pack>/2026-10-25.json` for each web pack and for `swgde-forensics`. Every pack's default rights state
  is `redistributable`, so these records hold section IDs and titles.

## Errata without a new file

If the publisher posts errata without re-issuing the PDF, keep the source text as printed. Record each erratum
in `review/source-defects.yaml` (or a future `errata.yaml`) with its errata URL and date. Corrections never
overwrite source text silently. The same applies to defects in published web pages: `ms-learn-ir` records a broken
list item in Microsoft's phishing playbook rather than correcting it.

## Scheduled checks

The maintainer enabled a weekly check on 2026-10-25. It runs every Monday at 12:34 UTC, in two forms:

* **A scheduled AI-agent task** (Claude, "Weekly IR source change check"), run from the maintainer's account. It
  works through the packs on the default branch:
  1. it runs `check-updates` for every pack;
  2. it runs `refresh` (never `--adopt`) for web and PDF-collection packs that show content changes or TOC drift;
  3. it replies with a short report and sends a push notification.

  It commits nothing and opens no issues or pull requests.
* **The `check-updates` GitHub workflow** has the same weekly schedule, plus a manual trigger (one pack or
  `all`). It uploads the summaries and reports as a workflow artifact.

Each run makes one request per locked web article, plus one per table of contents. That is about 1,500 page
requests a week to learn.microsoft.com, docs.aws.amazon.com, docs.cloud.google.com and www.first.org, and HEAD
requests for the PDFs (including 15 to swgde.org). The scheduled task
checks the packs on the default branch, so a pack is covered only after it is merged there. Adopting a change
stays a reviewed, manual step (`refresh --adopt`, then a pull request).

## Upstream dependencies

* `uv.lock` pins Python dependencies. Update with `uv lock --upgrade-package <name>`.
* Then run the tests, and rebuild every pack to confirm identical IDs and hashes. Any difference must be explained
  in the changelog.
