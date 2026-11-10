# Stage 2 pilot report — `dynamic-ir`

Date: 2026-10-25. Source: `dynamic-ir/pdf/1e-20260917164224`
(SHA-256 `f882f3fdfe4d2de2b8b295747b1579f7592c59cd46d5a3dc211ba8b777925164`), structured candidate
`dynamic-ir/html/1e-20260917164218`. Command: `uv run irpacks build dynamic-ir --pilot`.

## Scope

Recipe `pilot.keep` (element IDs assigned on the full document tree, so pilot and full builds share IDs):

| Kept unit | Physical pages (from bookmarks) | Element types exercised |
|---|---|---|
| `ch06` (all of Chapter 6) | 86–93 (+ part page 85) | raster diagrams (Figures 22–29), captions, xrefs |
| `ch12.s03` Timing and Coordination | 250–253 | tables (Table 17), admonitions, footnotes |
| `ch12.s04` Technical Implementation | 253–265 | code listings + callout lists, tables, figures, sidebar, admonitions; DNS Sinkholes (p. 254 / printed 230) |
| `ch12.s10` Contain: Step-by-Step | 293–299 | nested numbered procedures crossing pages |
| `ch13.s04` Eradication Challenges | 384–389 | code, figures, sidebar, footnotes |
| `ch13.s06` Eradicate: Step-by-Step | 396–404 | cross-page procedures, chapter-end notes |
| `ch16.s02` Prompting Foundations | 476–480 | prompt listings (embedded instructions as data), table |
| `ch16.s03` Foundational Use Cases | 480–497 | screenshots of AI tools, quote blocks of AI output, code |

Chapter-opening pages, part pages and the last two pages of each kept chapter (chapter-end notes) were added:
75 physical pages in total (`qa/pilot-scope.json`). Page ranges were resolved from the PDF's bookmarks and page
labels; no page offset was assumed.

## Results (final pilot run)

| Measure | Value |
|---|---|
| Alignment units | 831, all `exact` (literal parts located in order in the PDF body text) |
| Inline variables | 36 cross-references rendered identically; 5 footnote labels taken from the PDF |
| Punctuation-level differences | only theme classes: caption separator (43), callout glyph style (14), heading number style (4) |
| Leaf elements | 723, mapping status `verified` |
| Figures | 27, each matched to a PDF image object with identical pixel dimensions; 32×32 grayscale thumbnail similarity 1.0 for all (control pair of different figures: 0.22) |
| External links | 12, all matched to PDF URI annotations on the same pages |
| Uncovered PDF text | repeated table headers (41 key chars), list/callout markers (87), text of out-of-scope sections on boundary pages (classified `outside_pilot_scope`) |
| Pages | 48 `text_covered`, 27 `text_with_explained_gaps`, 0 unresolved |
| Chunks | 72; tokens min 51 / mean 418 / max 981; 0 oversize |
| Validation | passed, 0 errors, 0 warnings |
| Elapsed (uncached) | 16.9 s total (route 14.3 s incl. opening the 69 MB PDF, extracting 75 pages and parsing the 96 MB HTML) |
| Peak memory | 433 MB RSS |
| Manual text repairs | none |

## Visual inspection

Pages rendered from the PDF and viewed next to the extracted element list (and Markdown where noted):
2, 86, 88, 251, 254, 260, 295, 299, 386, 482, 487 — details in `packs/dynamic-ir/review/visual-review.yaml`.
All matched. Recorded observations:

- Code line breaks come from the HTML `<pre>`; the PDF wraps long lines visually (p. 386). Text identity is
  verified; visual wrap points are not reproduced.
- Diagram labels are raster image text and are not transcribed; chunk text says so.
- The PDF uppercases headings, table headers and sidebar titles via its theme; the Markdown keeps source casing.
- A cross-page list item (p. 295→296) is joined correctly.

## Systematic problems found and fixed before scaling up

1. A permissive fuzzy match of a short attribution line jumped ~12 pages ahead (seen in a full-book trial run),
   displacing later blocks. Replaced greedy placement with a weighted longest-increasing-subsequence anchor chain
   over exact occurrences; fuzzy matching only between placed neighbours.
2. Cross-file xrefs in the HTML (`href="_prepare_activity"`) were treated as external links. Now resolved as
   internal anchors.
3. Format-specific inline text: the HTML renders section xrefs as "Chapter 7"/"Section 15.2.5" and numbers
   footnotes globally; the PDF prints target titles and per-chapter labels. Inline xrefs and footnote references are
   now variable parts whose wording is taken from the PDF and checked against the target title or label.
4. Adjacent references (`[8][9]`, `Listing 78.[13]`) were resolved as one region; now parsed as runs.
5. A repeated table header on a continuation page was chosen as the anchor; ties now prefer the earliest occurrence.
6. Running footers of short chapters repeated < 3 times; a folio rule (line starts/ends with the page label) added.
7. Section anchors were duplicated on containers and headings; nested anchors (figures inside lists) were not
   emitted; headings of split sections were not attributed to any chunk. All fixed.
8. Validator false positives (example paths such as `C:\Users\...` inside source text; link and footnote
   normalization) corrected so checks test the intended property.

## Differences between the HTML candidate and the PDF (found in the full-book trial, recorded as issues)

- PDF p. 3: edition stamp `20260917164224` vs HTML `20260917164218` → PDF text used.
- PDF p. 77: the PDF reads "Special Publication 800-61 Revision 2." where the HTML adds ", Computer Security
  Incident Handling Guide" → PDF text used.
- Figure 180 caption: the PDF prints "[104]" with no note; the HTML has a footnote (SmartTECS, LockBit database
  dump). The note text is excluded from the edition and kept in `annotations.jsonl`.

## Decision

The pilot passed with no unresolved pages, critical issues or manual repairs, so the full conversion proceeded with
the same pipeline and settings.
