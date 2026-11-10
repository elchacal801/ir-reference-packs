# Changelog

All notable changes. Engine versions and pack versions are independent. Nothing has been released or published.

## Unreleased

### Changed (`ms-learn-ir` snapshot 2026-11-10)
* **Adopted the 2026-11-10 snapshot of `ms-learn-ir`** (135 articles, was 130). Validation: 0 errors, 0 warnings.
  * Microsoft moved its security operations guide from `security/operations` into the Zero Trust docset
    (`security/zero-trust`, SecOps discipline). Nine articles moved and keep their unit IDs; five were retitled.
  * The phishing playbook was rewritten as a six-step checklist: about 60 procedural sections were removed. The
    incident response overview ("Plan incident response") and the SecOps overview were restructured.
  * Five articles added from the new TOC sections (SecOps roles, antipatterns and technology; the "minimize
    impact" scenario; protecting Azure resources from cyberattacks). Four articles that are still published but no
    longer in a TOC are pinned.
  * Source defects: the leaked Markdown fence in the phishing playbook is gone with its section; a broken list
    item in the new "Step 5" is recorded.
* **Discovery** keeps the unit ID of an article the site moved (`moved_from` in `articles.yaml`).
* **Evaluation.** Held-out question x07 (mail forwarding and inbox rules) lost its evidence in the phishing
  playbook; it now cites the audit-log search procedures in `ms-purview-exchange`. The other `ms-learn-ir`
  evidence is unchanged apart from its URLs. The evaluation has not been re-run on this snapshot.

### Changed (ranking v4, fifth iteration)
* **Ranking v4** is the default; v3 stays available (`--ranking v3`).
  * Fusion: the dense top's similarity is rescaled to [0, 1] before blending. The weights are BM25 0.3, coverage
    0.3 and similarity 0.4.
  * Compound tokens match by their parts ("AMBER+STRICT" in "TLP:AMBER+STRICT"). Comparison words ("say",
    "compare", "according") no longer count as content.
  * `coverage.level` also uses the best result's similarity, reported as `best_similarity`. The advice and the MCP
    instructions say to read the top passages before concluding that the sources do not cover a question.
* **Index.** Web navigation sections ("Related content", "Next steps", "See also", "Clean up resources" and the
  like) are left out of default retrieval (511 chunks). New source aliases: "the book", "NIST SP 800-61", "NIST
  800-61". Index rebuilds reuse each chunk's vector when its text and the model are unchanged.
* **Evaluation.**
  * A sealed held-out set y (30 questions across all fourteen packs) was written before any v4 experiment.
  * v4 was chosen on every other set, x included, and on a practitioner test
    (`docs/practitioner-test-2026-10-27.md`). It was frozen in `eval/results/frozen-config-v4.json`.
  * y was run once with each ranking:
    * v3: 12/26 evidence complete, MRR 0.342;
    * **v4: 14/26, MRR 0.289**.
  * v4 completes more questions but ranks first hits lower, and searching each source separately it completed 0
    of y's 5 cross-source questions, against v3's 2. The diagnosis is in `eval/README.md`.
  * Rejected: two cross-encoder re-rankers (no gain on the tuning pool, 1.8–10 s per search), query focus, and a
    "DAIR" alias.

### Added (Azure, Google Cloud, FIRST and SWGDE; ranking v3, fourth iteration)
* **Four packs**, all snapshot 2026-10-25 and all with 0 validation errors. The combined index holds 12,364
  chunks across fourteen packs.
  * `ms-azure-ir` (73 Microsoft Learn articles; 900 chunks): Azure incident response, ransomware protection,
    activity and resource logs, flow logs and packet capture, Key Vault, Storage and Firewall logging, Azure
    Backup security, disk snapshots and VHD export, chain of custody.
  * `gcp-security-ir` (64 Google Cloud pages; 985 chunks; CC BY 4.0): the data incident response process,
    compromised credentials, abuse notifications, ransomware and cryptomining, Security Command Center findings
    and threat detection, Cloud Audit Logs, VPC Flow Logs, Packet Mirroring, snapshots, service account keys,
    Cloud Storage protection.
  * `first-standards` (8 FIRST pages; 291 chunks): TLP 2.0, CSIRT Services Framework 2.1, PSIRT Services Framework
    1.1, multi-party vulnerability coordination 1.1, CVSS 4.0 and 3.1, EthicsfIRST.
  * `swgde-forensics` (15 SWGDE PDFs; 508 chunks): acquisition, examination, remote and cloud collection, macOS,
    mobile, archiving, data integrity, report writing, model SOP, glossary.
* **Routes.**
  * `gcp_docs_html` (DevSite): alerts, tab groups, page chrome, the "Last updated" date, and a per-page check of
    the content license sentence.
  * `first_org_html`: `h1` top-level sections and mixed-case anchors.
  * `pdf_collection_layout` (`route_layout.py`): one unit per PDF, from the page layout. It reads furniture and
    printed page labels, takes headings from the outline and from numbered bold lines, builds ruled and
    shaded-band tables and nested lists, joins paragraphs across pages, and keeps a front-matter section. Elements
    are cited as `…pdf#page=N` (citation scheme `pdf_collection`).
  * Discovery reads the DevSite navigation as a TOC, and takes `pinned` pages for sites without one.
* **Ranking v3** (default). It is hybrid:
  * BM25 is blended with the best passage's IDF-weighted coverage of the question's key words (light stemming,
    a small IR synonym table) and with local embedding similarity: bge-small-en-v1.5 in ONNX, MIT, pinned in
    `models.lock.json`, fetched only by `irpacks models fetch`.
  * Chunk text is indexed without link targets.
  * Named sources get per-pack candidates and round-robin slots in the top 5.
  * The dense top is taken within the search's filters.
  * Without the `dense` extra it falls back to a lexical blend and says so.

  New commands and options: `irpacks index --dense`, `--index-text`, `irpacks models fetch|verify`,
  `irpacks eval --ranking v2|v3`.
* **Agent signals and guidance.**
  * Every search response carries `coverage` (high, medium or low; the words no result matched; advice) and
    `dense`, and each result carries `match`.
  * The MCP server's instructions give an incident-time workflow covering all packs.
  * MCP `search` takes `per_source` for questions that compare sources.
* **Evaluation.**
  * A sealed held-out set x (23 questions across all fourteen packs) and an x-tuning set (13) were written before
    any v3 experiment.
  * v3 was chosen on every other set (`eval/diagnose_ranks.py`) and frozen (`eval/results/frozen-config-v3.json`).
  * x was run once with each ranking: v2 11/20 evidence complete, MRR 0.432; **v3 13/20, MRR 0.525**. Cross-source
    questions were complete for 1/4 in one search and 2/4 searching each source separately.
  * The coverage signal's calibration and limits are measured in `eval/README.md`.

### Changed (fourth iteration)
* **Rights.** FIRST and SWGDE are cleared for public distribution, by the maintainer's decision (2026-10-25), and
  their public candidates are now complete.
  * FIRST is treated as redistributable with attribution, including the CVSS attribution.
  * SWGDE is redistributable under its Redistribution Policy. Every document keeps its cover page with the
    disclaimer, and citations now state the document status ("published") next to version and date (recipe
    `document_status`).
  * The evaluation quotes of both are stored as text, with the same evidence. Their history records keep titles.
* `check-updates` counts `page_template_changed` pages instead of listing each one: FIRST pages change their script
  nonces on every request.

### Fixed (fourth iteration)
* PDF layout route: table detection ignores the white boxes Word draws behind runs of text. They had turned a cover
  page paragraph into a table (four SWGDE documents) and wrapped Linux table cells into one row per line. Sliver
  columns beside a rule are merged, so the shaded bands of one table form one table. A line already read into a
  table is never read into a second one; that had repeated rows in seven sections of the Linux technical notes.
  SWGDE now has 37 tables instead of 51; no word was lost.
* Web routes: table cells, list items and other blocks are joined with line breaks, so two cells no longer run
  together (all web packs).
* The public leak check no longer flags citation metadata (publisher, unit titles) and URL slugs.
* The generated README, QA report and conversion note of a PDF collection no longer describe it as unpaginated
  web articles.

### Added (CISA packs, third iteration)
* **Two CISA packs** from tagged PDFs the maintainer supplied. www.cisa.gov refuses this project's cloud
  environment, so the bytes were not fetched and their identity with the published files is not verified. Both are
  marked TLP:CLEAR, and both have 0 validation errors.
  * `cisa-ir-playbooks`: the Federal Government Cybersecurity Incident & Vulnerability Response Playbooks
    (November 2021). 44 pages, 11 units, 83 sections, 78 chunks.
  * `cisa-stopransomware`: the #StopRansomware Guide v3.0 (October 2023; CISA, MS-ISAC, NSA, FBI). 31 pages,
    10 units, 22 sections, 30 chunks.

  The combined index holds 9,682 chunks across ten packs.
* **Tagged-PDF route, outline mode** (`structure: outline` in the recipe), for Word-tagged documents whose tag
  hierarchy does not reflect the printed one.
  * The hierarchy comes from the PDF outline. Headings tagged as `P`, table captions, and full-width checklist rows
    that match an outline entry become units and sections.
  * Word list quirks are repaired: nested `L` directly under `L`, unbulleted continuation items, private-use
    bullet glyphs.
  * A narrow title row above a table becomes its caption.
  * New recipe options: `printed_labels.pattern`, `furniture_patterns` and `exclude_hidden_text` (text drawn
    under artwork). The NIST pack's output is unchanged.
  * The page map records outline entries matched to table rows (`role: table_row_heading`); `validate` checks
    page ranges only for heading bookmarks.
* **Evaluation.** The CISA held-out set (8 questions) was run once: 4/7 evidence complete, MRR 0.464. On the
  ten-pack index every earlier set scores as on the eight-pack index.
* `sources fetch` skips an artifact marked `download: false` (its URL is a landing page) and prints where to place
  the supplied file.

### Changed (third iteration)
* **Rights.** AWS documentation is treated as redistributable with attribution, by the maintainer's decision
  (2026-10-25), extending the Microsoft Learn decision. The AWS public candidate is now complete.

### Fixed (third iteration)
* The source lock ignored PDF observations (page and bookmark counts, metadata dates) for files with an upper-case
  `.PDF` extension.

### Added (sources at scale, second iteration)
* **Five new web packs**, all snapshot 2026-10-25 and all with 0 validation errors:
  * `ms-defender` (375 articles);
  * `ms-sentinel` (99);
  * `ms-purview-exchange` (156);
  * `ms-windows-auditing` (337);
  * `aws-security-ir` (262 AWS pages).

  `ms-learn-ir` grew from 16 to 130 articles; its first 16 articles keep their IDs. In total there are 1,359 web
  articles, and the combined index holds 9,574 chunks across eight packs.
* **Discovery** (`irpacks sources discover <pack>`). Article lists are generated from the published tables of
  contents by regex rules in each recipe, into `packs/<pack>/articles.yaml`. Each exclusion carries a reason, and
  existing unit IDs are kept. `check-updates` also reports TOC drift: articles added to or removed from the
  selected TOC sections.
* **Route `aws_docs_html`.** The web route now uses site profiles: body, alerts, page chrome and metadata per
  site. The Learn route also handles layouts found at scale: tab groups (linearized under `Tab:` labels), FAQ
  wrappers, `<details>`, block-holding `<span>` anchors, definition lists, and headings inside lists or without
  anchors (kept as bold paragraphs).
* **Scheduled change check**, weekly on Mondays at 12:34 UTC, enabled by the maintainer's decision. It runs as a
  Claude scheduled task now, and in the `check-updates` GitHub workflow (every pack) once Actions is available.
  It reports only. Without a local build, `refresh` compares with the tracked history record.
* **Evaluation.** The vendor held-out set (12 questions) was run once: 7/11 evidence complete, MRR 0.374. The
  earlier sets on the eight-pack index kept every question's evidence, but MRR fell (held-out 0.750 → 0.692).
* Downloads run 8 at a time (`sources fetch`, `refresh`, `check-updates`). `sources verify` reports a pack that
  was never fetched in one line, instead of as errors.

### Changed
* **Rights.** Microsoft Learn articles are treated as redistributable with attribution, by the maintainer's
  decision (2026-10-25). The Microsoft public candidates are now complete. AWS stays `pending_review` (changed in
  the third iteration).

### Fixed
* Ordered lists with a `start` attribute crashed the Markdown export.
* Source line wraps inside paragraphs survived as line breaks; HTML whitespace now collapses to spaces.
* Already-bold terms were wrapped in bold again (`****x****`).
* The Markdown link checks treated escaped brackets as links, and an escaped backslash hid the bracket after it.
* Parentheses in link URLs ended Markdown links early; they are now percent-encoded.
* Public export: text that is part of a stub title (for example a related-articles link naming an omitted page)
  was reported as a leak.

### Not done (second iteration)
* **CISA** (Federal IR and Vulnerability Response Playbooks, #StopRansomware Guide). www.cisa.gov denies every
  request from the cloud environment (Akamai "Access Denied"), so nothing was acquired. Built in the third
  iteration from PDFs the maintainer supplied.

### Added (first iteration)
* **MCP server** (`irpacks serve`, optional `mcp` extra pinned to `mcp==2.3.0`). It is read-only and runs over
  stdio, with five tools: `list_packs`, `search`, `expand_context`, `get_section` and `get_page`. Anticipated
  failures return readable tool errors. See `docs/mcp.md`.
* **Pack `ms-learn-ir` 0.1.0.** 16 Microsoft Learn articles captured on 2026-10-25: the incident response
  playbooks, the IR overview and planning guides, and Microsoft Entra response and security operations guidance.
  * 353 sections, 3,256 elements, 314 chunks; 0 errors and 0 warnings.
  * Public candidate: the 8 MIT-licensed Entra articles. The 8 security-docset articles are omitted pending a
    rights review, leaving title-and-URL stubs.
* **Route `learn_html_pages`** for Microsoft Learn (DocFX) article pages.
  * Sections keep the published anchors. Code keeps its language. Alerts become admonitions.
  * Images are recorded by alt text and URL only.
  * Each article's source commit and update time are recorded and cited.
* **`source-map.json`** and a section-anchor citation scheme for unpaginated sources. Web-pack validation checks
  that units are present, that unit metadata matches, and that every span URL and anchor resolves.
* **Change detection.**
  * `check-updates` compares web articles by their published version metadata and a hash of the body text. It
    reports `unchanged`, `content_changed`, `metadata_changed` or `page_template_changed`.
  * `irpacks diff` produces a semantic change report between two builds or history records: sections added,
    removed and retitled, and changed blocks with word-level diffs and a severity.
  * `irpacks refresh` builds a candidate from a new capture and reports the changes. With `--adopt`, it moves the
    pack to the new snapshot.
  * `irpacks snapshot` writes text-free history fingerprints under `history/`. These redact the titles and
    section IDs of units not cleared for redistribution.
* **Evaluation.** The Microsoft held-out set (`eval/questions-ms.spec.yaml`, 8 questions) was run once: 4/7
  evidence complete. The book and NIST splits were rerun on the three-pack index with nothing tuned; evidence was
  unchanged.
* Tests: 57 offline tests. New ones cover the web route, the semantic diff, the article update statuses, history
  redaction, a web pack build and the MCP server. All use synthetic fixture pages in Learn's markup
  (`tests/fixtures/learn/`), so no Microsoft text is in the repository.

### Fixed
* Web-pack chunks dropped each section's URL#anchor from their spans, so a citation listed only the article URL.
  Chunk spans now keep `url`, and validation requires every chunk to cite the anchors it covers.
* The code-fence balance check is CommonMark-aware: a fence inside an indented or longer fence no longer counts.
  A stray fence line in Microsoft's phishing playbook is recorded as a source defect and kept as published.

### Fixed (from the pilot setup dry run, `docs/pilot-dryrun-2026-10-25.md`)
* `irpacks sources fetch <pack>` now accepts the pack ID positionally, as the README and pilot guide document.
  `--pack` is still accepted.
* `irpacks sources verify` blocks only on build inputs (authoritative or structured candidate). A re-fetched
  reference capture whose bytes vary per request (the CSRC landing page) is reported as `changed`, and a registered
  identity that was never supplied as `not_supplied`. Each status line explains itself, and a summary line closes
  the output.
* The pilot guide gives the measured disk use and timings (clone to first search: about 2.6 min on 4 vCPU) and the
  expected `verify` output.

## [0.1.0.dev0] (unreleased, local only)

### Engine
* Pack contract (`pack.yaml`), recipes, rights rules, source lock (`sources.lock.json`) and JSON Schemas for all
  outputs.
* Route `asciidoctor_html_verified_against_pdf`: the same-build Asciidoctor HTML gives the structure, and every
  block is verified against the authoritative PDF. The verification uses a weighted-LIS anchor chain, per-chapter
  footnote labels, xref titles, and furniture removal. TOC and index are extracted from the PDF only.
* Route `tagged_pdf_structure`: the tagged-PDF structure tree. It keeps per-MCR pages, repairs table rows split
  across pages, reconstructs rowspans, and uses CSF native locators.
* Canonical `document.json` and `page-map.json`; Markdown book and chapter exports with page anchors.
* Section-bounded chunking with required context: lead-ins, warnings, notes, table headers.
* SQLite FTS5 search and token-budgeted context expansion.
* Ranking configuration v2, chosen on the tuning split only and then frozen:
  * corpus weights;
  * content-word proximity;
  * figure-intent boost;
  * result slots reserved for packs named by a source alias.
* Validation:
  * schemas, IDs and anchors;
  * locators and printed labels;
  * furniture leakage and verbatim code;
  * element coverage and citations;
  * whether elements lie inside their PDF-bookmark page ranges.
* Rights-driven public candidate export with byte-level leak checks. Omitted divisions keep their title and anchor
  as a navigation stub.
* Deterministic local and public archives (`irpacks package`).
* Opt-in update check (`irpacks check-updates`): HEAD request with a 1-byte range-GET fallback. It never modifies
  the lock.
* Book-to-framework mapping validator with provenance labels (`irpacks validate-mappings`), and a synthetic
  Countermap example.
* Offline pytest suite (41 tests) with regression fixtures:
  * edition change;
  * uncertain citations;
  * missing warnings;
  * invalid mappings;
  * corrupt hashes;
  * private-content export;
  * malicious source content.

### Packs
* `dynamic-ir` 0.1.0: Wright, *Dynamic Incident Response*, edition 20260917164224. Full conversion: 666 pages,
  9,868 elements, 736 chunks.
* `nist-sp800-61r3` 0.1.0: NIST SP 800-61 Rev. 3 (April 2025). 48 pages, 320 elements, 58 chunks.

### Fixed during final review
* Quotation attributions now carry the comma the PDF prints between name and title. 19 elements were affected;
  the issue was found by visual sampling of p. x.
* Public export: omitted divisions lost their anchors, which left dead links in the contents list.
