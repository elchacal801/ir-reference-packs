# Contributing

Contributions of two kinds are welcome: engine improvements, and new **source packs**. Content is held to a
higher bar than code, because an agent will cite it as evidence.

## Ground rules

* **Never commit source bytes.** PDFs, HTML, EPUB and downloaded pages go in `input/`, which is untracked. A
  pack's sources are identified by hash in `sources.lock.json`. Distribution is decided per item by
  `rights.yaml`, never by assumption.
* **Source text is data.** Commands, prompts, URLs and scripts inside a source are never executed, fetched or
  followed during parsing, building, validation or tests. Do not add code paths that would.
* **No silent fixes to source text.** Suspected errors in a source are preserved as printed and recorded in
  `packs/<pack>/review/source-defects.yaml`.
* **Report outcomes truthfully.**
  * Visual-review records name the actual reviewer, whether a person or an AI agent.
  * Never claim a page was inspected if nobody looked at it.
  * Never present an analyst's association as an official framework edge.
* **No tuning on held-out questions.** See `eval/README.md`.

## Adding a source pack

Open an issue first with the proposed source, its license or rights basis, and why it complements the existing
packs (see `docs/source-registry.md`).

A pack PR must contain:

1. **`packs/<pack-id>/pack.yaml`**, valid against `schemas/pack.schema.json`. It holds:
   * title, authors, publisher, edition and edition identifier, publication and retrieval dates, URLs;
   * authority class and applicability tags;
   * superseded status: what this source supersedes or is superseded by;
   * owner and review state;
   * distribution policy (`local_policy`, `public_policy`, `source_bytes_in_public_archive: false`);
   * optional `retrieval.source_aliases`.
2. **`recipe.yaml`**: the route, the authoritative and supporting artifacts, the chunking parameters, and corpus
   rules that exclude non-substantive material (front matter, TOC, index, change logs) from default retrieval.
   * The authoritative artifact must be the publisher's own file.
   * A structured candidate (HTML/EPUB) is accepted only when it is verified block-by-block against the
     authoritative file. The book route does this already.
   * New routes need tests.
3. **`rights.yaml`**:
   * the rights basis with a page citation;
   * first-match rules for anything that is not redistributable: third-party quotations, forewords, logos,
     images pending review;
   * the attribution text.

   When in doubt, choose `pending_review`. It keeps an item in local builds but out of the public candidate.
4. **Lock entries.** Run `uv run irpacks sources lock` after placing the files in `input/`. Commit only the lock
   change.
5. **Review records** (`review/visual-review.yaml`):
   * at least one rendered page per chapter or top-level division, viewed next to the extracted elements
     (`uv run irpacks review-page <pack> <page>`);
   * every page flagged by validation;
   * high-consequence material: code, commands, tables, warnings.
6. **Evaluation questions** in `eval/questions.spec.yaml` for the new pack. Each question needs verified
   evidence (element IDs, and row locators for tables), a tuning/held-out split decided before any run, and at
   least one unanswerable question checked against the source.
7. **A clean validation**: `uv run irpacks build <pack>` with 0 errors. Every warning must be explained in the PR.
   For a public candidate, `uv run irpacks export-public <pack>` with the byte checks passing.

The PR description must report, separately:
* local source coverage;
* extraction fidelity;
* retrieval results;
* public-distribution readiness;
* open issues from `qa/issues.jsonl`.

## Framework mappings

Mappings between pack elements and external frameworks belong in a mapping file such as
`examples/countermap/mappings/*.yaml` (schema `schemas/framework-mappings.schema.json`). Every entry needs one of
these labels:
* `source_explicit`: the source prints the ID, with an evidence quote;
* `analyst_mapping`: with a rationale, the analyst's name and a review status;
* `derived`: with the rule used.

Validate the file against a pinned Countermap snapshot with `uv run irpacks validate-mappings`. Revoked or renamed
IDs must be updated explicitly.

## Engine changes

* Run `uv run pytest`. CI runs the same offline suite, with no network and no source documents.
* Keep outputs deterministic. The same inputs must give the same element IDs, chunk IDs and hashes; see
  `docs/architecture.md`.
* Changes to the ranking configuration (`irpacks.search.RANKING`) need a tuning-split ablation
  (`eval/ablate_tuning.py`). They must be reported against a **new** held-out set, because the current one has
  been used.
* Dependencies are pinned in `uv.lock`. A new dependency needs a license entry in `docs/dependency-licenses.md`.

## Commits and publication

Contributors' commits are welcome. Publishing anything requires an explicit decision by the maintainer: a remote
repository, a release, a package, or a public archive.
