# Pilot setup dry run (facilitator, 2026-10-25)

**What this is.** The facilitator ran the participant setup in `docs/pilot-guide.md` exactly as written. The run
was done by an AI agent (Claude) on behalf of the maintainer.

**What this is not.** It is **not** participant feedback. No practitioner was recruited or contacted, and the
pilot itself has not started.

## Environment

* A fresh `git clone` of `main` at merge commit `6f120cc`, into an empty directory.
* An empty `uv` package cache, so every dependency was downloaded.
* Linux container: 4 vCPU Intel Xeon @ 2.80 GHz, 15 GB RAM, Python 3.13 (managed by `uv`).
* Network access to PyPI, the publisher's site and nist.gov.

## Steps and timings

| Step (as written in the guide) | Result | Time |
|---|---|---|
| `git clone` | ok | 1 s |
| `uv sync` | ok: 23 packages, 74 MB environment | 5 s |
| `uv run irpacks sources fetch dynamic-ir` | **failed: `unrecognized arguments: dynamic-ir`** (finding 1) | — |
| `… sources fetch --pack dynamic-ir` / `--pack nist-sp800-61r3` (the workaround from `--help`) | ok: 18 artifacts, 173 MB | 16 s |
| `uv run irpacks sources verify` | **exit 1**: `MISMATCH` on the CSRC landing page and `absent` for the never-supplied reader copy (finding 2) | 1 s |
| `uv run irpacks build dynamic-ir && … build nist-sp800-61r3 && … index` | ok: both validations passed, 794 chunks indexed | 135 s |
| First `irpacks search` | ok: correct section and page citations | < 1 s |

* **Peak memory:** 1,451 MB for the book build, 344 MB for NIST.
* **Disk:** about 385 MB in total.
* **Clone to first successful search:** about 2.6 minutes of command time. Reading the guide is not included.

**Reproducibility across environments.** The fresh clone's `document.json`, `page-map.json`, `chunks.jsonl` and
`book.md` were byte-identical to those in the maintainer's build environment, for both packs.

## Findings and fixes

1. **The documented `sources fetch <pack>` form did not work.** The CLI only accepted `--pack <pack>`, while the
   README, the pilot guide and every other pack command use a positional pack ID.
   * Fixed: `irpacks sources fetch <pack>` is now the form shown in the help. `--pack` is still accepted.
   * Test: `tests/test_cli_sources.py`.
2. **`sources verify` failed on a harmless difference.** The NIST CSRC landing page is a reference capture, not a
   build input. Its HTML differs on every request because of Cloudflare e-mail obfuscation tokens and a challenge
   script, so its hash can never be stable. The reader-copy identity, which was never supplied, was reported as
   `absent` without explanation.
   * Fixed: `verify` now separates build inputs (`authoritative`, `structured_candidate`) from reference captures:
     * a differing build input is a blocking `MISMATCH` (exit 1);
     * a differing reference capture is an informational `changed`;
     * a registered but never-supplied identity is `not_supplied`.
   * Each line explains itself, and a summary line ends the output.
   * The build was never affected: `resolve()` hash-checks only the inputs it reads.
   * Test: `tests/test_cli_sources.py`.
3. **Guide accuracy.** The disk estimate was corrected (about 400 MB, measured). The guide now shows the measured
   timings and the expected `verify` output.

## Synthetic-case smoke check (material check, not a result)

Each case task was entered as one keyword query to confirm the pilot material reaches relevant passages.

| Task | Top results (book sections unless noted) |
|---|---|
| 1 Verify and triage | Verify and Triage step-by-step (p. 188), Initial Risk Assessment (pp. 170–171) |
| 2 Contain identity access | Contain Step 2, Implement Identity and Access Containment (p. 270); Entra ID Session and Token Revocation (pp. 342–344); Refresh Token and Persistent Credential Revocation (pp. 236–237). `irpacks context` on the top hit: `required_context_complete = true` |
| 3 Scope | BEC Investigation: Identity and Access (pp. 311–313), Email System (pp. 309–311) |
| 4 Payment | BEC Investigation: Financial Transaction Investigation (pp. 313–314) |
| 6 NIST alignment | NIST SP 800-61r3 §3, CSF 2.0 Community Profile (pp. 10–11); book Ch 20, The Six CSF Functions |

Not exercised:
* step 3 of the setup (connecting an AI agent; optional);
* task 5 (Countermap; optional, and it needs a separate installation).

## Ready for the pilot?

The setup works end to end once findings 1 and 2 are fixed. Those fixes must be on the branch participants clone.

Still the maintainer's to do:
* choose the participants and send the guide;
* decide how the repository is shared, since it is private;
* collect the forms in `docs/feedback-template.md`.
