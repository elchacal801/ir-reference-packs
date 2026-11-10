# Retrieval decision

Status: **decided for v0.1 — keep the local SQLite FTS5 baseline; no separate retrieval service.**
Date: 2026-10-25. Decision owner: maintainer (Diego Parra); evidence prepared by an AI agent (Claude), not yet
reviewed by a human.

The plan's gate (v2.0, "Retrieval and tool reuse") allows a separate retrieval service only with a documented gap,
a failing example, an explanation of why an adapter is insufficient, and a passing test for the proposed
solution. This document records what was executed, what was only read, and why the gate does not pass.

## What was compared

| | Local baseline (this repo) | AppliedIR forensic-rag | Hydrant |
|---|---|---|---|
| Revision tested | `irpacks` 0.1.0.dev0, ranking config v2 (`src/irpacks/search.py:RANKING`) | `AppliedIR/sift-mcp` commit `5ef6081a35252380ffe5456dca1a5f3355fbe01a` (2026-10-24), package `rag-mcp` 0.6.5.1 + `sift-common` 0.6.5.1 | `The-Public-Works/hydrant` commit `fecd1698a37a9b8c79ea0cbcdb0fea0a394c91ef` (2026-05-22) |
| License | Apache-2.0 (code) | MIT | MIT |
| Execution | **Executed** | **Executed** (offline, same chunks, same scorer) | **Not executed — documentation and source review only** |
| Retrieval method | SQLite FTS5 BM25 (porter, unicode61) + generic re-ranking | Dense embeddings (`BAAI/bge-base-en-v1.5`, revision `a5beb1e3e68b9ab74eb54cfd186867f64f240e1a`), ChromaDB cosine HNSW, keyword boost 1.15, MITRE ID query augmentation | pgvector kNN over OpenAI `text-embedding-3-small` embeddings, plus a GitHub knowledge graph |
| Runtime dependencies | Python stdlib `sqlite3` (FTS5) | chromadb 1.5.9, sentence-transformers 6.2.0, torch 2.14.1, mcp | PostgreSQL + pgvector (Docker), OpenAI API key, GitHub token; Slack/Linear optional |
| Install footprint (measured) | project venv 81 MB (includes the PDF conversion stack) | venv 5.9 GB + model cache 419 MB | not installed |

Hydrant was not executed because its indexing path sends document text to the OpenAI embeddings API. The
project's constraints forbid uploading the book to an external model service without separate explicit
instruction. Its design also targets team operational data (Slack, Linear, GitHub, CODEOWNERS), and it ships write
tools (create Slack channels, post messages, open and update Linear issues) that this project must not exercise. Its
relevance is architectural (cited-by-design MCP answers), not as a retrieval backend for a reference pack. No
performance claim is made for it.

## Method (executed comparison)

* **Corpus:** the same 763 chunks for both backends. These are every chunk with `include_in_default_retrieval=true`
  in `build/local/dynamic-ir/chunks.jsonl` and `build/local/nist-sp800-61r3/chunks.jsonl`: narrative, chapter
  notes and NIST references. Chunk text is identical; forensic-rag does not re-chunk `.jsonl` records.
* **Import route (adapter):** `eval/backends/forensic_rag.py` writes one forensic-rag record per chunk to
  `knowledge/irpacks/passages.jsonl`, with a `.bundled` marker. It then calls
  `rag_mcp.build.build(skip_online=True, ...)` and queries `rag_mcp.index.RAGIndex.search(q, top_k=5)`.
  * The chunk ID travels in the record's `source` metadata and is used only to map results back. forensic-rag's
    source boosts name no such sources, so ranking is unaffected.
  * `title` is the chunk's last heading.
  * Online sources were skipped and `HF_HUB_OFFLINE=1` was set; no network destination was contacted during
    the run.
* **Scoring:** both backends are scored by the same code (`irpacks eval`; `--results-file` for forensic-rag).
  A question counts only if **every** evidence group is satisfied in the top 5. Table-row evidence also needs the
  row's native locator.
* **Questions:** 25 source-verified questions: 15 tuning (14 answerable) and 10 held-out (8 answerable). Three
  are unanswerable by design. See `eval/README.md`.
* **Tuning:**
  * The local ranking features were chosen on the tuning split only (`eval/results/tuning-ablation.json`).
  * They were frozen (`eval/results/frozen-config.json`) before the held-out split was run once.
  * forensic-rag was run once with its defaults, with no tuning on either split.
  * So the **held-out split is the fair comparison**. The tuning-split numbers favour the local baseline.
* **Hardware:** 4 vCPU Intel Xeon @ 2.80 GHz, 15 GB RAM, CPU only (no GPU), Linux container.

## Results

| Metric (k = 5) | Local FTS5, tuning | forensic-rag, tuning | Local FTS5, **held-out** | forensic-rag, **held-out** |
|---|---|---|---|---|
| Evidence complete | 12/14 | 10/14 | **7/8** | **6/8** |
| Mean evidence-group recall | 0.893 | 0.750 | 0.938 | 0.812 |
| MRR (first satisfying result) | 0.833 | 0.750 | 0.738 | 0.812 |
| Citation valid (satisfying chunks) | 13/13 | 11/11 | 8/8 | 7/7 |
| Required context complete | 3/3 | 4/4 | 5/5 | 4/4 |
| Duplicate results | 0/75 | 1/75 | 0/50 | 0/50 |
| Failures | t04, t11 | t06, t07, t10, t11 | h07 | h05, h07 |

Before tuning, the local baseline (all re-ranking features off) scored 10/14 on the tuning split, the same as
forensic-rag.

| Resource | Local FTS5 | forensic-rag |
|---|---|---|
| Index build | built during `irpacks build` / `irpacks index` (seconds) | 253 s to embed 763 records |
| Index size on disk | 9.4 MB (combined index) | 18.1 MB (ChromaDB) |
| Query latency, median / max | 17.9 ms / 34.2 ms | 81.5 ms / 110.2 ms |
| Peak RSS during evaluation | ~34 MB | ~1.9 GB (model loaded) |

**Per-question differences:**

* forensic-rag found **t04** and the local baseline did not. t04 is a vocabulary mismatch: "confirm containment
  worked" against the heading "Validate Containment Effectiveness". This is the kind of gap dense retrieval
  closes.
* The local baseline found **t06, t07, t10 and h05** and forensic-rag did not:
  * h05 is a CSF identifier (`RS.MA-01`) question;
  * t10 asks about a figure;
  * t06 and t07 hinge on specific procedure terms.
* forensic-rag ranked the first satisfying chunk higher on h02, h04 and h06. Both systems ranked it first on h01.
* Both backends missed the two cross-source questions, t11 and h07. Each needs one book chunk and one NIST chunk
  in the top 5, and each backend returned evidence for only one of the two. Cross-source retrieval is 0/2 for
  both, the weakest category. The only other multi-evidence question, t09, has both groups in the book and
  passes for both backends.

**Interpretation:**

* The samples are small (14 and 8 answerable questions). A one- or two-question difference is not
  statistically meaningful.
* These results do **not** show that either backend is better in general. They show the two are complementary:
  lexical vs. semantic matching.
* No answer generation was evaluated. No answer accuracy or abstention quality is claimed for either backend.

## Citation preservation and context expansion

| Requirement | Local baseline | forensic-rag via adapter |
|---|---|---|
| Returns a stable chunk ID | yes | only through the `source` field the adapter sets |
| Returns PDF page, printed page and section locator | yes (`citation` in each result) | no. Search results carry `source`, `title`, `text` (truncated to 1500 characters), `mitre_techniques` and `platform`, so citations must be re-joined from `chunks.jsonl` by chunk ID |
| Rights state, review status, authority, superseded flags | returned and filterable | not returned; filtering would need metadata the search API does not expose |
| Required context (lead-ins, warnings, notes), section expansion | `irpacks context` | none; possible only by re-joining on chunk ID |
| Long chunks | full text indexed | `bge-base-en-v1.5` truncates input at 512 tokens, so text beyond that in chunks of up to 1000 estimated tokens is not embedded |

## Decision against the gate

1. **Documented gap:** lexical search misses some paraphrased questions (t04). This is real, and the
   forensic-rag run demonstrates it.
2. **Failing example:** t04 (tuning). Not every held-out failure is a lexical gap: h07 fails for both backends.
3. **Why an adapter is insufficient:** it is not. The adapter route works. One JSONL file plus a chunk-ID join
   keeps citations and context, and forensic-rag can be used *alongside* the packs without changes to either
   project.
4. **Passing test for a separate service:** none exists, and none is proposed.

The gate therefore does not pass, and **no separate retrieval service is introduced**.

v0.1 ships the dependency-free local FTS5 baseline as the default. It is offline, has an 18 ms median latency,
and returns citations, rights and context natively. `eval/backends/forensic_rag.py` stays as the documented
adapter and comparison harness.

**Candidate follow-ups (not done; not claimed):**

* (a) An optional hybrid mode that fuses BM25 with embedding ranks, evaluated on a new held-out set. The current
  held-out set has now been used and must not be reused for selection.
* (b) An upstream contribution to forensic-rag that returns arbitrary record metadata, so citations survive
  without a join.

## Reproduce

```bash
uv run irpacks build dynamic-ir && uv run irpacks build nist-sp800-61r3 && uv run irpacks index
uv run irpacks eval --split tuning  --out eval/results/local-fts5-tuning.json
uv run irpacks eval --split heldout --out eval/results/local-fts5-heldout.json
# forensic-rag: separate environment (Python 3.12) with rag-mcp installed and the model already downloaded
HF_HOME=/path/to/hf /path/to/.venv-rag/bin/python eval/backends/forensic_rag.py --out eval/results/forensic-rag-raw.json
uv run irpacks eval --split tuning  --results-file eval/results/forensic-rag-raw.json --out eval/results/forensic-rag-tuning.json
uv run irpacks eval --split heldout --results-file eval/results/forensic-rag-raw.json --out eval/results/forensic-rag-heldout.json
```

Model weights were downloaded once from Hugging Face. In this environment that took `HF_HUB_DISABLE_XET=1`, so the
transfer used the standard CDN instead of the Xet storage host. Every later run was offline.
