# Countermap integration example (optional, synthetic)

This example shows how an agent can combine two independent local tools:

* **Countermap** answers framework questions: ATT&CK technique resolution and the D3FEND countermeasures that
  D3FEND maps to a technique, each with Countermap's own provenance.
* **The `dynamic-ir` reference pack** supplies citable book passages: PDF page plus printed page, a stable element
  ID and a chunk ID.

The scenario is **synthetic**: an account takeover that ends in payment redirection at a fictional manufacturer.

The reference packs do not depend on Countermap, and Countermap does not depend on them. This project never
modifies Countermap and never re-ingests ATT&CK, D3FEND, ATLAS or F3. Countermap is called only through its
read-only CLI (`info`, `lookup`, `counter`, `render`).

## Files

| File | What it is |
|---|---|
| `scenario.yaml` | Synthetic scenario in Countermap's scenario-spec format: 8 steps, ATT&CK Enterprise IDs |
| `mappings/dynamic-ir-attack.yaml` | Book element → ATT&CK ID associations **stored in this repository**, each labelled `source_explicit`, `analyst_mapping` or `derived` (schema: `schemas/framework-mappings.schema.json`) |
| `run_example.py` | Builds the per-step report; writes `build/examples/countermap/report.{md,json}` (not tracked, because it quotes book text) |

## Run

```bash
# 1. Build the pack and the combined index (see the root README)
uv run irpacks build dynamic-ir && uv run irpacks build nist-sp800-61r3 && uv run irpacks index

# 2. Countermap in its own environment, database built from its own lock file (see Countermap's docs)
python -m venv ~/.venv-countermap && ~/.venv-countermap/bin/pip install countermap==0.2.2
~/.venv-countermap/bin/countermap --db ~/cm.db build      # downloads ATT&CK/D3FEND/ATLAS/F3 per its lock

# 3. Validate the mapping file against that snapshot, then run the example
uv run irpacks validate-mappings examples/countermap/mappings/dynamic-ir-attack.yaml \
    --countermap-db ~/cm.db --countermap-bin ~/.venv-countermap/bin/countermap
uv run irpacks countermap-example --countermap-db ~/cm.db --countermap-bin ~/.venv-countermap/bin/countermap
```

Without `--countermap-db`, the example runs in **pack-only mode**. The report then states that no Countermap
lookups were performed, and nothing is substituted for them. `validate-mappings` without a database checks
structure, element IDs and evidence quotes, and reports `framework_ids_checked: NOT checked`.

## Provenance labels (book mappings)

| Label | Meaning | Count |
|---|---|---|
| `source_explicit` | The book names the ID at that element. The validator requires the quoted `evidence` to occur in the element text, and the element to contain the ID (`T1234.001` or an `attack.mitre.org/techniques/T1234/001` URL). | 5 |
| `analyst_mapping` | An analyst's judgment, with `rationale`, `analyst` and `review_status` (all `unreviewed`; prepared by an AI agent). **Not stated by the book, and not a Countermap, MITRE ATT&CK or D3FEND relationship.** | 10 |
| `derived` | Parent technique of a non-derived sub-technique entry on the same element (`parent_of_subtechnique`). | 4 |

The report keeps three things apart in every step:
1. Countermap's official data, with Countermap's `provenance` field;
2. the book mappings from this repository, with their labels;
3. keyword-retrieval hits, which are *not* mappings.

### Validation rules

The validator rejects:
* IDs that do not resolve in the pinned snapshot;
* revoked IDs that Countermap redirects. For example, `T1656` resolves to `T1684.001` in ATT&CK 20.0; the file
  must name `T1684.001` explicitly;
* names that differ from the snapshot;
* unknown elements;
* missing evidence or rationale;
* bad derivations;
* a snapshot version mismatch.

## Tested snapshot (2026-10-25)

* **Countermap:** 0.2.2 from PyPI, a read-only external tool. License per package metadata:
  `Apache-2.0 AND BSD-3-Clause`.
  * Source lock: `countermap-sources.lock.json` from `elchacal801/countermap` commit
    `06e5ee7c50e79c10f7683cc6b07b06bd026ad0c5`.
  * Database built locally and not degraded: ATT&CK Enterprise/Mobile/ICS 20.0, D3FEND ontology 1.6.0, D3FEND
    mappings 1.6.0, ATLAS 2026.09, F3 1.2.
* **Results:**
  * The mapping file validated against this snapshot: 19 entries, 18 distinct IDs, 0 errors.
  * `countermap render` accepted the scenario: all 8 step IDs resolved.
  * Countermap reports **no D3FEND mapping** for steps s7 (T1684.001 Impersonation) and s8 (T1657 Financial
    Theft). That gap is real in the loaded data and is shown in the report as Countermap returns it.
* **Licensing:** framework data is subject to MITRE's ATT&CK terms of use and to the D3FEND, ATLAS and F3
  licenses, as listed by `countermap info`. This repository stores only framework IDs and names in the mapping
  file, not framework content.

## Limitations

* The analyst mappings cover only the techniques in this scenario plus five IDs the book prints. They are not a
  complete ATT&CK mapping of the book.
* Keyword retrieval for a step description is lexical. It surfaced the right Business Email Compromise sections
  for s7 and s8, but for s1 its top hits are only loosely related (tabletop scenarios, verification examples).
* No mapping has been reviewed by a human yet.
