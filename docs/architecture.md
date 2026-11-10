# Architecture

```
packs/<pack>/pack.yaml + recipe.yaml + rights.yaml + review/*.yaml
            │
sources.lock.json ──► input/<pack>/... (hash-verified; untracked)
            │
            ▼
   route (per recipe)                         canonical model                exports (all from the canonical model)
 ┌──────────────────────────────┐        ┌───────────────────────┐     ┌──────────────────────────────────────────┐
 │ asciidoctor_html_verified_   │        │ document.json         │     │ book.md, chapters/*.md (page anchors)    │
 │   against_pdf (book)         │──────► │  elements, IDs, spans │ ──► │ chunks.jsonl (retrieval corpus)          │
 │ tagged_pdf_structure (NIST,  │        │ page-map.json (PDF)   │     │ search.sqlite (FTS5 over chunks only)    │
 │   CISA: outline mode)        │        │                       │     │                                          │
 │ learn_html_pages, aws_docs_  │        │ source-map.json (web, │     │ assets/ (figures, formulas, tables)      │
 │   html, gcp_docs_html,       │        │   PDF collection)     │     │ qa/, qa-report.md, AGENT_GUIDE.md, ...   │
 │   first_org_html (web)       │        │ annotations.jsonl     │     │                                          │
 │ pdf_collection_layout (SWGDE)│        │                       │     │                                          │
 └──────────────────────────────┘        └───────────────────────┘     └──────────────────────────────────────────┘
                                                    │
                                         rights.apply_rights ──► public.export_public ──► dist/public/packs/<pack>/
                                                    │
                                         diffdoc (semantic diff, history/ fingerprints) ◄── refresh / check-updates
                                                                                   ▲
 published tables of contents ──► discover.py ──► packs/<pack>/articles.yaml ──────┘ (TOC drift)

 build/local/index.sqlite (combined index: FTS5 + optional chunk vectors, dense.py)
     ├─► irpacks search / context / section (CLI)
     └─► irpacks serve (read-only MCP server, stdio)
```

## Source identity

* `sources.lock.json` pins every artifact. Each entry records:
  * `source_id`, `sha256` and size;
  * the URL and where it was resolved from;
  * HTTP validators and retrieval time;
  * observations: pages, bookmarks and PDF metadata for PDFs; for web articles, the source commit, update time,
    `ms.date`, title, document ID and a SHA-256 of the article body text.

  A hash proves byte identity only. It says nothing about correctness or rights.
* `sources.resolve()` refuses a file whose hash differs from the lock. A different file, such as a reader's own
  copy of a book, is registered under a **new** `source_id` and never substituted. The user-inspected copy of the
  book (`dynamic-ir/pdf/1e-20260914175315-user-inspected`, sha256 `a0b161…`) is registered this way. It is marked
  `not_available_locally` because it was never supplied; the build uses the publisher's PDF.
* The authoritative input is the publisher's PDF.
  * The book's structured candidate is the HTML from the same build. It is verified, not trusted: the build
    stamps are 20260917164224 for the PDF and 20260917164218 for the HTML.
  * The visible differences are listed in `qa/issues.jsonl` and `docs/pilot-report.md`. Where they differ, the
    PDF text wins.

## Routes

### Book: `asciidoctor_html_verified_against_pdf`

1. **PDF inventory** (`pdfsource.py`) extracts characters, images (raw hashes) and link annotations for every page,
   and caches them by source hash.
   * Running headers and footers are detected as repeated band patterns plus a folio rule, and are excluded from
     body text.
   * TOC and index pages are PDF-only regions.
2. **Structured candidate** (`adochtml.py`): the Asciidoctor HTML is parsed into typed blocks. These include
   sections, paragraphs, lists, listings with callouts, tables with spans, admonitions, sidebars, quotes,
   footnotes, figures (data-URI bytes) and math. Footnote and xref markers are kept as variable parts.
3. **Alignment** (`align.py`):
   * Every block is turned into a case-folded alphanumeric key.
   * Each key is placed in the PDF body stream by a weighted longest-increasing-subsequence anchor chain. Ties go
     to the earliest occurrence, so a repeated table header cannot pull a block forward.
   * Gaps are then filled between placed neighbours.
   * Variable parts are read back from the PDF: per-chapter footnote labels, and xrefs printed as titles.
   * Visible punctuation is compared separately and classified. Callout glyphs, typography and attribution
     style, for example, are recorded in `extraction.visible_differences`.
4. **Element status.** Each element gets a `mapping_status`:
   * `verified`
   * `verified_with_differences`
   * `pdf_text_fallback`, where the PDF text replaces the HTML
   * `pdf_only`
   * `verified_image`
   * `visually_verified`
   * `unverified`

   Each element also gets typed `source_spans`: PDF page, printed label, and a bbox in
   `pdf_points_top_left_origin`.
5. **Images** are matched to PDF images by pixel dimensions and checked by 32×32 grayscale similarity. All 204
   scored 1.0; a deliberately mismatched control pair scored 0.22.
6. **Gaps.** Uncovered PDF text is classified:
   * list and callout markers;
   * repeated table headers;
   * TOC and index;
   * paired with an element;
   * unclassified.

   A page without unclassified gaps is `text_covered` or `text_with_explained_gaps`.

### NIST and CISA: `tagged_pdf_structure`

A pdfminer walker reads the structure tree. It keeps the `/Pg` of each marked-content reference and orders
children by `/K`. On top of that it:
* repairs rows of tables split across pages, using column x-positions;
* reconstructs rowspans;
* drops repeated headers;
* splits merged list items;
* recovers orphan marked content;
* reads printed page numbers from the footers (the PDF has no `/PageLabels`). A recipe can give the footer
  pattern (`printed_labels.pattern`), for example `Page | 12`.

For NIST, CSF elements become native locators (`csf2:RS.MA-01`) on table rows. The default hierarchy follows the
tags (`H1`–`H6`, as NIST's are).

**Outline mode** (`structure: outline`, the CISA packs). Documents exported from Word often have a tag hierarchy
that does not match the printed one: headings tagged as `P`, chapter titles held in table captions, checklist
phases as full-width table rows. In outline mode:
* The PDF outline (bookmarks) is the hierarchy. The walker descends into the containers (`Part`, `Sect`, `Div`)
  and turns a block into a unit or section heading when its text matches the next outline entry at the right
  level.
* A table caption that matches an entry is lifted out of the table as a heading. A paragraph that is tagged before
  the caption but printed below it is moved after it.
* A full-width table row that matches an entry splits the table into one section per row group. The page map
  marks those bookmarks `role: table_row_heading`, and `validate` checks page ranges only for heading bookmarks.
* A narrow title row above a table (fewer cells than the widest row) becomes the caption, not the header.
* Word list quirks are repaired: an `L` nested directly in an `L` attaches to the preceding item; an unbulleted
  `LI` continues the previous item; private-use bullet glyphs (U+F0B7 and others) are stripped.
* `furniture_patterns` moves repeated markings (`TLP:CLEAR`) that sit in the body area to furniture.
  `exclude_hidden_text` drops text in a recipe-given page region that is drawn under artwork: invisible on the
  page, but present in the text layer and the tags. Each exclusion is logged as an issue and recorded in
  `review/source-defects.yaml`.
* An outline entry that matches no block is logged (`outline_entry_without_heading`); none remain in the CISA
  packs.

### Web articles: `learn_html_pages`, `aws_docs_html`, `gcp_docs_html` and `first_org_html`

All four routes are `route_learn.py` with a site *profile*. A profile says where the article body and title are,
how alerts are marked up, which page chrome to skip, and which `<meta>` tags carry version metadata.

| | Microsoft Learn (`learn_html_pages`) | AWS documentation (`aws_docs_html`) |
|---|---|---|
| Body and title | the second DocFX `div.content`; the `h1` of the first | `div#main-col-body`; its `h1.topictitle` |
| Alerts | `div.NOTE/TIP/IMPORTANT/WARNING/CAUTION` (the printed label is dropped) | `div.awsdocs-note`, kind from its title (Note, Important, Warning, …) |
| Chrome skipped | scripts, buttons, the tab bar | `awsdocs-*`/`awsui-*` elements, the page header, the "Topics" list |
| Version metadata | `git_commit_id`, `updated_at`, `ms.date`, `document_id`, `source_path`, `original_content_git_url` | none published: title and body hash only |

| | Google Cloud documentation (`gcp_docs_html`) | FIRST standards (`first_org_html`) |
|---|---|---|
| Body and title | `div.devsite-article-body`; `h1.devsite-page-title` without its bookmark widget, else the body's first `h1` | `div#c1`; its first `h1` |
| Alerts | `aside.note/caution/warning/special/key-point/…`; the leading "Note:" label is dropped | none |
| Chrome skipped | survey, rating, tooltip, bookmark and free-trial elements, interactive table filters (`form`, `devsite-select`), the label heading of each DevSite tab | none |
| Version metadata | "Last updated" date from the page footer; the footer's content license sentence, checked against the recipe (`page_license_unexpected` otherwise) | none published: title and body hash only |
| Layout | DevSite tab groups (`devsite-selector`, `div.ds-selector-tabs`) are linearized like DocFX tabs; `devsite-code`, `devsite-expandable` and `devsite-filter` are walked into | `h1` also marks top-level sections (`h1_sections`); anchors keep capitals |

Table cells, list items and other block elements are separated by line breaks when their text is joined, so two
cells never run together.

Each captured page is one unit (a `chapter` element with ID `<unit_id>`), parsed with lxml as data: scripts are
never run, links are not followed, and images are not downloaded.
* **Sections.** Every h2–h6 with a published `id` becomes a section `{unit}.{anchor}`, and sections nest by heading
  level. If the same anchor appears twice (the same heading in two tabs), the element anchor gets a suffix so it
  stays unique; the citation keeps the published anchor. A heading without an `id`, or one inside a list item or
  table cell, cannot be cited as a section. It is kept in place as a bold paragraph.
* **Layouts opened.** Containers that hold headings are walked into: FAQ `article`/`section` wrappers, DocFX tab
  groups, and `span` anchors that wrap blocks. Tab groups are linearized: each tab's content follows a
  `Tab: <label>` line, and the tab bar is dropped. `<details>` becomes a bold summary line followed by its
  content.
* **Blocks.**
  * Paragraphs, with the source's own line wraps collapsed to spaces (only `<br>` breaks a line).
  * Ordered and unordered lists, including checklists and a `start` number.
  * Definition lists (`dl`): one item per term, with its `dd` blocks as children.
  * Code from `pre`, with its language where the class names one.
  * Tables, and admonitions.
  * Relative links are made absolute. Parentheses and spaces in URLs are percent-encoded so a Markdown link
    cannot end early.
* **Images** are not captured. Each figure keeps its alt text and the absolute URL of the published image, and its
  rights state is `metadata_only`.
* **Spans.** Every element gets a `section_anchor` span: `url` (the article URL plus `#anchor`), `anchor`, `unit`
  and `mapping_status` `native_structure`. Chunk spans keep the `url`, and a chunk's citation lists every URL#anchor
  it covers.
* **Citation.** Article, section, the update time and short commit where published (else the capture date), and
  URL#anchor.
* **Checks.** Each article's headings are counted. Empty headings, headings in skipped chrome, and alert titles are
  not counted. If the count differs from the sections built plus the headings kept as bold paragraphs, the build
  raises a `section_count_mismatch` issue. Unknown markup is kept as text and recorded as an `unmapped_html` issue,
  never dropped silently.

### PDF collections: `pdf_collection_layout` (`route_layout.py`)

For a set of PDFs without a usable structure tree (the SWGDE documents are untagged, or tagged without the
printed hierarchy). Each recipe artifact is one hash-pinned PDF, and each PDF becomes one unit, as an article does
in a web pack. The conversion reads pdfplumber's character layer; nothing in the file is executed.
* **Furniture.** Lines whose text (digits ignored) repeats at about the same height on at least half of the pages
  are running headers and footers. The printed page label is read from them (`printed_labels.pattern`, e.g.
  `Page (\d+) of \d+`). Images repeated on most pages (logos) are furniture too.
* **Headings.** The PDF outline gives the section titles and levels. An outline entry matches up to five wrapped
  lines. Entries that point into the cover or front matter, bookmark-only entries and entries that point at a
  paragraph are dropped, and the remaining levels are renormalized. Below the outline's depth, and in documents
  without an outline, a bold line that starts with a section number ("7.2 Live acquisition") is a heading at the
  number's depth.
* **Blocks.**
  * Ruled tables come from pdfplumber's line detection. White filled boxes are ignored: Word draws them behind
    runs of text, and they are not rules or bands.
  * Tables drawn as shaded row bands, without rules, are assembled from aligned cell runs. Bands whose column
    starts agree within 8 pt form one table; a sliver column beside a rule is merged into its neighbour. A line
    read into one table is never read into another.
  * Bulleted, numbered and lettered items nest by marker indent, and hanging-indent lines join their item.
  * Paragraphs end at a gap wider than the line pitch, a change of left margin or of bold weight, or a heading.
    After a loose gap, a line still continues an unfinished sentence when it starts in lower case. A paragraph
    continues across a page break when its last line has no closing punctuation and the next page's line has the
    same margin and size.
* **Front matter.** Text before the first section (cover, disclaimer, redistribution policy) becomes the unit's
  `front-matter` section. A corpus rule (`within_pattern: '\.front-matter$'`) keeps it out of default retrieval.
* **Locators.** Each element carries its PDF page, printed page label and bbox, plus a URL locator: the
  document URL with the PDF open parameter of its section's first page (`…pdf#page=4`). Citations (scheme
  `pdf_collection`) name the document, its number, version, status (recipe `document_status`, e.g. "published")
  and date, the section, the printed and physical
  page, and that URL.
* **Checks.** If less than 98% of a document's body characters outside tables reach an element, the build raises
  `coverage:text_not_in_elements` (major); none does in the SWGDE pack. An outline entry that matches no line is
  logged (`outline_entry_without_heading`).

### Discovery (`discover.py`)

A web pack's article list (`packs/<pack>/articles.yaml`) is generated from the published tables of contents by
the recipe's `discovery` rules. Each rule names a TOC, regular expressions over the TOC path ("A > B > C"), a URL
prefix (default: the TOC's directory) and a unit-ID prefix. Microsoft Learn and AWS publish TOC files; for Google
Cloud the TOC is read from the DevSite navigation of the docset's pages. FIRST publishes no TOC for its
standards, so its recipe lists the pages as `pinned` discovery entries. The SWGDE recipe lists its PDFs directly
as artifacts, each with its document number, version, date and landing page.

`irpacks sources discover` captures the selected pages and keeps those that parse as articles. Excluded entries
are recorded with a reason: landing pages, external or other-docset links, articles already in another pack,
download failures, and redirects to an article that is already listed. Unit IDs already assigned are kept for
their URL. `sources.load_recipe` merges the generated list into the recipe, so the build, the lock and the update
check see one artifact per article.

`source-map.json` replaces the page map. It holds one entry per article: URL, source ID, hash, page metadata and
section anchors. Validation checks that every unit is present, that the unit metadata matches the source map, and
that every span URL and anchor resolves to a section that exists.

## Change detection (`updates.py`, `diffdoc.py`, `refresh.py`)

* **Update check.** For PDFs, `check-updates` compares HTTP validators. For web articles it compares the page's
  own version metadata (where published) and a hash of the article body text, never raw bytes. It checks
  articles 8 at a time, and reports `unchanged`, `content_changed`, `metadata_changed` or
  `page_template_changed`. For packs with discovery rules it also compares the published TOCs with the pinned list
  (`discover.toc_drift`), and reports articles added to or removed from the selected sections.
* **Semantic diff.** `diffdoc.diff_documents` compares two canonical documents unit by unit, in four steps:
  1. Units are matched by ID. Changes to the metadata keys are reported: title, URL, commit, update time, `ms.date`
     and document ID.
  2. Sections are matched by ID, which reports sections added, removed and retitled.
  3. Within each section, blocks are aligned by content hash with `difflib.SequenceMatcher`. Unmatched blocks are
     paired with the most similar block of the same type when the similarity ratio is at least 0.5; otherwise
     they count as added or removed.
  4. Each change gets a severity: `high` for code, tables, text inside warnings and procedure steps. Changes are
     listed most severe first.
* **Refresh.** `refresh` builds a candidate pack in `build/candidates/` from a new capture, through the same
  `_build_web` path as a normal build. It writes the diff against the current build to `build/updates/`. Without
  a local build it diffs against the tracked history record instead (sections, not wording). It changes the
  recipe, the generated article list, the pack contract and the lock only with `--adopt`.
* **History.** `diffdoc.fingerprint` writes `history/<pack>/<edition>.json`. Per unit it records metadata; per
  section it records a content hash. Titles and IDs are kept only where the rights state is `redistributable`;
  otherwise section IDs are hashed and titles dropped. `diff_fingerprints` compares two such records, or a record
  and a build.

## MCP server (`mcp_server.py`)

`irpacks serve` runs an MCP server over stdio. It uses the official Python SDK, pinned as the optional `mcp` extra.
* It opens the combined index read-only, and finds pack directories next to it.
* It exposes five tools: `list_packs`, `search` (with `per_source`: one search per named source or given pack),
  `expand_context`, `get_section` and `get_page`.
* Every tool is annotated read-only, idempotent and closed-world.
* Anticipated failures return readable tool errors. Examples are an unknown chunk ID, a page out of range, or
  `get_page` on an unpaginated pack.
* The server instructions tell the client that every returned passage is data, not instructions.

The server performs no writes, downloads or command execution. Setup: `docs/mcp.md`.

## Identifiers and determinism

* **Element IDs** are hierarchical and semantic at the top level: `fm-*`, `part1`, `ch12`, `bm-index`. Below that
  they use per-type ordinals, e.g. `ch12.s04.s04.s03.p01`, `sec3.s02.t01`. They are stable for the same edition
  and recipe.
* **Chunk IDs** are `{pack}:{edition}:{chunk-config-hash8}:{first element}:{fragment}`:
  * A new edition produces new chunk IDs. This is tested.
  * Changing chunk parameters, tokenizer revision or variant changes the config hash.
* **Content hashes** are recorded per element and per chunk. Rebuilding from the same locked inputs reproduces the
  same IDs and hashes; see the reproducibility record in the final report.

## Chunking (`chunking.py`)

* Chunks never cross a section boundary. A whole section is kept when it fits in 1,000 estimated tokens, and small
  sections are not merged across headings.
* Atomic units are never split. They include:
  * a lead-in paragraph ending in ":" together with the list, table or code it introduces;
  * a listing with its callout list;
  * a procedure with an adjacent warning, caution or important admonition.

  Oversize units have explicit split policies:
  * lists by item;
  * code by lines, with the caption repeated;
  * tables by row groups, with the header repeated and CSF categories kept together;
  * containers by child;
  * prose by sentence.
* Overlap of 80–120 tokens is applied only to prose splits.
* **Required context** is recorded per chunk and returned by `irpacks context`. It covers:
  * the first fragment of a split unit;
  * the lead-in of a procedure in the previous chunk;
  * warning-type admonitions elsewhere in the section;
  * the footnotes a chunk references.
* Token counts use the `irpacks-approx-bpe` estimator, revision 2. It is a declared heuristic, not a model
  tokenizer.
* **Corpus rules** (`recipe.yaml`) classify chunks: narrative, notes, references, front matter, navigation, index,
  back matter. Only some are included in default retrieval.
* A chunk is excluded automatically if it contains any element whose locator is not citable.

## Search and context

* `search.py` builds an FTS5 index (`porter unicode61`) over `heading_path` and `text`, with column weights 2:1.
  The text is indexed without link targets and bare URLs (`index_text: plain`; v2 indexed the stored Markdown).
  `irpacks index --dense` also stores one embedding per chunk (`chunk_vectors`, `dense.py`). The index's `meta`
  table records the text mode and the embedding model.
* Queries are tokenized and quoted, so FTS5 syntax in a query cannot change its structure.
* **Ranking `RANKING` v4** (the default, frozen; `eval/results/frozen-config-v4.json`) is v3 (below) with these
  changes:
  * **Fusion.** The dense top's cosine similarities are rescaled to [0, 1] (minimum to maximum) before blending.
    Raw cosine values sit in a narrow band, so under v3 the best embedding match barely outscored the 100th. The
    weights are 0.3 × BM25 + 0.3 × coverage + 0.4 × rescaled similarity.
  * **Tokens.** Compound tokens match by their parts ("AMBER+STRICT" is found in "TLP:AMBER+STRICT"). Comparison
    words ("say", "compare", "according") are not counted as content.
  * **Levels.** The response's `coverage` level also uses the best result's raw similarity (`SIMILARITY_LOW`,
    `SIMILARITY_HIGH`). It is reported as `best_similarity`.
  * **Index side.** For web routes, sections titled as navigation ("Related content", "Next steps", "See also",
    "Clean up resources" and the like; `packbuild.NAVIGATION_TITLES`) are classified as `navigation` and left out of
    default retrieval: 511 chunks of link lists or tutorial clean-up. New source aliases: "the book", "NIST SP
    800-61", "NIST 800-61".

  Its measured effect, including where it does worse than v3, is in `eval/README.md`. Two cross-encoder
  re-rankers were tried and rejected (no gain on the tuning pool, 1.8–10 s per search).
* **Ranking `RANKING_V3`** (frozen; `eval/results/frozen-config-v3.json`; `--ranking v3`):
  1. **Candidates:**
     * the BM25 top 100;
     * plus the top 20 of each pack the query names by a source alias;
     * plus the 100 chunks most similar to the query by embedding, taken among the chunks the search's filters
       allow.
  2. **BM25 features from v2:**
     * corpus weights (notes and references × 0.5);
     * adjacency of content-word pairs as phrases, × (1 + share matched);
     * figure intent, × 1.5.
  3. **Fused score** = 0.1 × BM25 (normalised to the best candidate) + 0.2 × coverage + 0.7 × cosine similarity.
     * *Coverage* is the IDF-weighted share of the question's content words found in the chunk's best passage
       (a paragraph or list item plus the heading path). Matching uses a light stemmer and a small table of IR
       synonyms, so "confirm containment worked" matches "validate containment effectiveness".
     * Chunks outside the dense top get the lowest similarity in it.
  4. **Named sources.** Slots in the top 5 go round-robin to every named pack (two for a single pack). A named
     source's words are dropped from the query unless they are its subject ("What does Event Threat Detection
     do?").
  5. **Fallback.** Without the `dense` extra, the model files or chunk vectors, the blend is 0.7 BM25 + 0.3
     coverage. The response's `dense` field says why.
* **Embeddings** (`dense.py`): BAAI bge-small-en-v1.5, quantized ONNX (384 dimensions, CLS pooling, normalised,
  the bge query instruction), run by onnxruntime on CPU. Its files are fetched only by `irpacks models fetch`,
  checked against `models.lock.json`, and never downloaded implicitly. Queries are encoded locally.
* **Vector reuse.** `chunk_vectors` stores, with each vector, the sha256 of the text it embeds. A rebuild reuses
  a vector when that hash and the model label are unchanged, so a metadata-only rebuild takes seconds instead of
  about 12 minutes.
* **Signals** (they never change the order): per result `match` (coverage, matched and missing words); per response
  `coverage` (level, best coverage, best similarity under v4, `terms_in_no_result`, advice). Their calibration and
  limits are in `eval/README.md`.
* `RANKING_V3` and `RANKING_V2` are kept, and `--ranking v3` or `--ranking v2` reproduces the earlier baselines.
  Every response and evaluation run reports the configuration it used.
* `context.py` expands a seed chunk with its required context, its neighbours and the rest of its section, within
  a token budget. It reports `required_context_complete` and lists anything it omitted.

## Rights and public candidates

* `rights.py` applies first-match rules from `rights.yaml`, with inheritance and overrides. Every element is
  marked `redistributable`, `metadata_only`, `user_supplied_local` or `pending_review`.
* `public.py` builds the public candidate:
  * Non-redistributable items are omitted. Each keeps its ID, locators and, for divisions, title and anchor, but
    loses its content.
  * Images under review are withheld.
  * Markdown and chunks are regenerated, and the index is rebuilt.
  * Byte checks then run: file-type allowlist, no source documents, no withheld image bytes, no omitted text in
    any file or in the index, no local-only artifacts.
* `package.py` writes deterministic archives. Before writing the public archive, it checks the members again.

## Validation (`validate.py`)

Errors fail the build:
* schemas;
* duplicate IDs, anchors or chunk IDs;
* tree integrity;
* page accounting;
* locators and printed labels against the page map;
* elements outside their PDF-bookmark range;
* running-furniture leakage;
* Markdown links and anchors;
* code fences and verbatim code;
* assets;
* element coverage in Markdown;
* every text leaf in a chunk;
* required-context links;
* oversize chunks;
* chunk citations against the page map;
* absolute paths;
* internal markers.

For web packs, the page checks are replaced by the source-map checks: units present, unit metadata, and span URLs
and anchors. Every chunk must also cite the URL#anchor of each section it covers.

Warnings are reported in `qa-report.md`:
* unverified leaves;
* unmatched bookmarks;
* tables without a header row.

## External tools (optional, independent)

* **Countermap.** Called only through its read-only CLI, from `examples/countermap`. Book mappings live in this
  repository, each labelled with its provenance.
* **forensic-rag.** Used only for the comparison harness in `eval/backends/forensic_rag.py`, run in its own
  environment. See `docs/retrieval-decision.md`.
