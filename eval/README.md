# Retrieval evaluation

This measures **retrieval of source-verified evidence**: does the local search return the passages that answer a
question, with valid citations and complete required context? No answer generation is involved. No answer
accuracy or abstention quality is measured or claimed.

## Question set

* `questions.spec.yaml` is the authoring spec: 25 questions, 15 **tuning** (`t01`–`t15`) and 10 **held-out**
  (`h01`–`h10`). The split was fixed when the questions were written, before any run.
  * The questions cover:
    * exact terms and paraphrases;
    * procedures;
    * tables with CSF row locators;
    * a figure question;
    * questions that need required context;
    * single-pack NIST questions;
    * cross-source questions, which need one book passage and one NIST passage.
  * Scenarios are synthetic.
* `questions.yaml` is **generated** by `uv run python eval/build_questions.py`. It attaches a verbatim quote of up
  to 200 characters and the page locators of every evidence element, taken from the built canonical documents.
  The build fails if any element or table row has disappeared, so evidence cannot drift silently. `irpacks eval`
  checks the quotes again before every run.
* **Evidence model.** A question has one or more evidence *groups*, and a group lists interchangeable
  *alternatives* (element IDs; table rows also need their native locator). A question is
  **evidence-complete@k** only if **every** group is satisfied within the top k results.
* **Verification.**
  * Evidence was chosen by reading the cited elements, using exact-phrase lookup with `eval/find_evidence.py`.
    It was not taken from search rankings.
  * The verification was done by an AI agent (Claude). Maintainer review is pending.
  * All evidence quotes are book or NIST text under the packs' rights. One evidence alternative, `ch06.f01`, is a
    figure whose image is pending rights review; the quote used for it is its printed caption.

### Microsoft Learn questions (`questions-ms.spec.yaml`)

When the `ms-learn-ir` pack was added, a **separate held-out set** of 8 questions (`m01`–`m08`, split
`ms-heldout`) was written. This happened after the ranking configuration had been frozen. The set has never been
used for tuning.
* The questions cover:
  * exact cmdlet and KQL questions (m01, m06);
  * paraphrases (m02–m04);
  * an attack-flow procedure (m05);
  * one cross-source question that needs a Microsoft step and a book passage (m07);
  * one unanswerable question (m08: the default primary refresh token lifetime).
* m08 was checked with a regex over all three packs for "PRT" near lifetime, valid, days or expire. Nothing matched.
* The set is resolved with `uv run python eval/build_questions.py --spec eval/questions-ms.spec.yaml`, which writes
  `questions-ms.yaml`. Evidence locators carry the article URL and section anchor instead of PDF pages.
* The spec sets `quotes: withhold_unredistributable`. Evidence from articles not cleared for redistribution (the
  security docset: m03, m05, m06) is therefore stored as the SHA-256 of its quote, not its text. `irpacks eval`
  checks the hash against the built pack before every run, so drift is still caught. The rationales describe
  where the evidence is without quoting it.
* For web evidence, `citation_valid` requires the chunk's citation to list the evidence element's URL#anchor.

### Vendor documentation questions (`questions-vendor.spec.yaml`)

A third held-out set of 12 questions (`v01`–`v12`, split `vendor-heldout`) was written when the `ms-defender`,
`ms-sentinel`, `ms-purview-exchange`, `ms-windows-auditing` and `aws-security-ir` packs were added. It was written
before any run, and run once with the frozen configuration.
* The questions cover:
  * Defender for Endpoint isolation, and the advanced hunting process table;
  * Sentinel hunting bookmarks;
  * Audit (Standard) retention, and mailbox-access forensics;
  * Windows events 4624 (logon type 10) and 4625 (status 0xC000006A);
  * GuardDuty credential remediation, CloudTrail `AssumedRole`, and the VPC flow log default format;
  * one cross-source question: the book plus Defender for Endpoint;
  * one unanswerable question: the Windows hibernation file.
* The unanswerable check is a regex for `hibernat` or `hiberfil` over all eight packs (text and table cells). It
  found no element.
* Evidence was chosen by exact-phrase lookup (`eval/find_evidence.py`), not from search rankings. The AWS quotes
  were first stored as hashes (`quotes: withhold_unredistributable`). After the AWS rights decision they were
  regenerated as text; the evidence elements are unchanged.

### CISA questions (`questions-cisa.spec.yaml`)

A fourth held-out set of 8 questions (`c01`–`c08`, split `cisa-heldout`) was written when the two CISA packs were
added. It was written before any run, and run once with the frozen configuration.
* The questions cover:
  * the federal deadline for reporting an incident to CISA;
  * the post-incident hotwash (lessons learned);
  * the checklist steps to declare an incident;
  * three #StopRansomware checklist and best-practice items: the first step for affected systems, what to do
    when a device cannot be disconnected, and keeping backups;
  * one cross-source question: #StopRansomware plus the book's containment triggers (ch12);
  * one unanswerable question: a CISA requirement for ransomware negotiation transcripts.
* The unanswerable check is a regex over all ten packs for `negotiat` within 80 characters of `transcript` (text
  and table cells). It found no element.
* Evidence was chosen by exact-phrase lookup, not from search rankings.

### Sealed set x and x-tuning (`questions-x.spec.yaml`, `questions-x-tuning.spec.yaml`)

Written on 2026-10-25, when the Azure, Google Cloud, FIRST and SWGDE packs were added, and committed (dd641bf)
**before any ranking v3 experiment**.
* **x (sealed held-out, split `x-heldout`, 23 questions `x01`–`x23`).** These span all fourteen packs, with weight
  on the four new ones. They are phrased as a responder would ask during an incident, with few of the
  documents' own words:
  * Azure forensic disk copies, the activity log;
  * a leaked Google Cloud service account key, Data Access audit logs;
  * a GuardDuty credential finding, purging a phishing email, mail forwarding rules, a service-install event ID;
  * TLP:AMBER+STRICT, CVSS v4.0 Attack Requirements, CSIRT incident coordination;
  * live acquisition, remote collection documentation, cloud preservation requests;
  * federal KEV monitoring, memory before shutdown;
  * four cross-source questions: Azure and Google Cloud packet capture, Azure and Google Cloud deletion
    protection, the book and SWGDE on chain of custody, Google Cloud and Entra on revoking access;
  * three unanswerable questions: a CVSS 5.0 formula, a SWGDE Tesla procedure, a CISA directive on Ivanti
    appliances.

  `eval/diagnose_ranks.py` refuses this split, so it cannot be looked at while tuning.
* **x-tuning (13 questions `xt01`–`xt13`).** These are for the same packs and may be used to choose a ranking.
  No evidence element is shared with x.
* The unanswerable checks are searches over all fourteen packs, on text and table cells:
  * x21: `CVSS v5`, `CVSS 5.0` and `CVSS version 5` found no element.
  * x22: `Tesla` found one element, which names the TeslaCrypt ransomware family (an Azure Firewall article). The
    SWGDE vehicle infotainment document is not in the pack.
  * x23: `Ivanti` found no element. "Emergency Directive" appears only as a general term, naming no directive.
  * xt13 (x-tuning): `TLP 3`, `TLP version 3` and `TLP v3` found no element.
* Evidence was chosen by exact-phrase lookup, not from search rankings. FIRST and SWGDE quotes were first stored as
  hashes (`quotes: withhold_unredistributable`), pending their rights decision. After both were cleared
  (2026-10-25) they were regenerated as text. Each text hashes to the stored value, and the evidence elements are
  unchanged.

### Sealed set y (`questions-y.spec.yaml`)

Written on 2026-10-27 and committed (b231b05) **before any ranking v4 experiment and before the practitioner
test**.
* **30 questions `y01`–`y30`, split `y-heldout`, across all fourteen packs.** They are phrased as a responder or an
  agent would ask during an incident, often without the documents' own terms: "a PHP file that runs commands" for
  a web shell, "forged Kerberos tickets" for a krbtgt reset.
  * 21 questions need one source. They cover endpoint and email investigation, cloud logs and detections (AWS,
    Azure, Google Cloud), Windows events, identity, evidence handling (SWGDE), TLP, CVSS and disclosure (FIRST),
    eradication and NIST.
  * Five cross-source questions (y22–y26):
    * NIST and CISA on lessons learned;
    * AWS and Azure on isolating a VM;
    * AWS and Google Cloud on the logs that record an admin grant;
    * the book and CISA on persistence before eradication;
    * the book and CISA on law enforcement before eradication.
  * Four unanswerable questions (y27–y30): TLP:BLACK (TLP 2.0 has no such label), imaging a Chromebook, NIST SP
    800-61 Revision 4, and Okta System Log retention. Each was checked by a search over all fourteen packs; the
    checks are in the spec.
* No evidence element is used by any earlier set. Evidence was chosen by exact-phrase and regex lookup over the
  canonical documents, never from the search index or its results.
* `eval/diagnose_ranks.py` refuses this split.

### Unanswerable questions (3)

These are checked against the full text of both canonical documents, including tables, code and footnotes. The
regex checks were rerun on the final build (2026-10-25):

| ID | Question | Check | Result |
|---|---|---|---|
| t13 | SHA-256 of the LockBit 5.0 encryptor | `LockBit\s*5` | 0 elements |
| | | elements mentioning LockBit that contain a 64-hex string | 0 of the 17 LockBit elements |
| h08 | HIPAA per-violation penalty for willful neglect | `willful neglect` | 0 elements |
| | | `civil monetary penalt` | 0 elements |
| | | `HIPAA.{0,80}(penalt\|\$)` | 0 elements |
| h09 | Cobalt Strike team-server default port | `Cobalt Strike` | 1 element (Conti leaks narrative, `ch08.s04.s03.sb01.p02`; no technical detail) |
| | | `50050` | 0 elements |
| | | `team ?server` | 0 elements |

For these questions the harness records what retrieval returned. A generator would have to abstain. Abstention
is not evaluated.

## Metrics (`irpacks eval`)

| Metric | Definition |
|---|---|
| `evidence_complete_at_5` | Answerable questions with all evidence groups satisfied in the top 5 |
| `mean_group_recall` | Mean fraction of evidence groups satisfied |
| `mrr` | Mean reciprocal rank of the first satisfying result |
| `citation_valid` | Each satisfying chunk's citation covers the evidence element's PDF pages, and its printed labels match the page map. For web evidence: the citation lists the element's URL#anchor |
| `required_context_complete` | `irpacks context` returns all required context within 3,000 tokens, for satisfying chunks that declare required context or for questions marked `requires_context` |
| `duplicate_results` | Results that repeat elements already returned at a higher rank |

## Protocol (tuning vs held-out)

1. **Baseline (no re-ranking):** tuning 10/14. Failures: t04, t10, t11, t15.
2. **Ablation on tuning only.** `eval/ablate_tuning.py` tries generic features one at a time and in combination;
   the results are in `results/tuning-ablation.json`.
   * Stopword removal **lowered** recall (9/14), so it was rejected.
   * Proximity is stable from 0.5 to 2.0.
   * No question-, chapter- or pack-specific rule was used.
3. **Freeze.** `results/frozen-config.json` records the ranking configuration, the `search.py` hash, the questions
   hash and the chunk hashes.
4. **Held-out, run once:** `results/local-fts5-heldout.json`.
5. **Final-build verification.** After later content fixes (attribution punctuation), both splits were rerun with
   the frozen configuration and nothing tuned: `results/local-fts5-final-build-*.json`. Results were identical.
6. **Public candidate index**, all splits: `results/public-index-all.json`.
7. **Three-pack index.** After `ms-learn-ir` was added to the combined index, both splits were rerun with the
   frozen configuration and nothing tuned: `results/local-fts5-3pack-*.json`. Evidence and failures were
   identical; MRR moved slightly (0.833 → 0.845 tuning, 0.738 → 0.750 held-out).
8. **Microsoft held-out set, run once:** `results/local-fts5-ms-heldout.json`.
9. **Eight-pack index.** After the Defender, Sentinel, Purview/Exchange, Windows auditing and AWS packs were added
   (9,574 chunks), the earlier sets were rerun with nothing tuned: `results/local-fts5-8pack-*.json` and
   `results/local-fts5-ms-heldout-8pack.json`. Evidence and failures were identical, but MRR fell (see below).
10. **Vendor held-out set, run once:** `results/local-fts5-vendor-heldout.json`. A final-build rerun after a
    Markdown-only fix (no ranking or chunk-text change) gave identical results.
11. **Ten-pack index.** After the two CISA packs were added (9,682 chunks), every earlier set was rerun with nothing
    tuned: `results/local-fts5-10pack-*.json`, `results/local-fts5-ms-heldout-10pack.json` and
    `results/local-fts5-vendor-heldout-10pack.json`. Every metric was identical to the eight-pack index.
12. **CISA held-out set, run once:** `results/local-fts5-cisa-heldout.json`. A rerun on the final build gave
    identical results.

13. **Fourteen-pack index, v2.** After the Azure, Google Cloud, FIRST and SWGDE packs were added (12,365 chunks),
    every earlier set and x-tuning were rerun with v2 on the Markdown index, with nothing tuned:
    `results/local-fts5-v2-14pack-*.json`.
    * Held-out MRR fell from 0.692 to 0.660.
    * v10 ("default VPC flow log record format") now misses. The question names no provider, and the Google Cloud
      VPC Flow Logs pages fill the top 5.
    * v04 is now complete at rank 5.
14. **Sealed set x written**, with x-tuning, before any v3 experiment (commit dd641bf).
15. **Ranking v3 chosen on the tuning pool**: every set except x, scored with `eval/diagnose_ranks.py`. The earlier
    held-out sets (held-out, Microsoft, vendor, CISA) were each run once before; from here on they are tuning
    data, and their v3 results below are not held-out measures.
    * Frozen in commit 39f1dae.
    * Amended in d2d144a, before x was run: `dense_within_filters`, a fix for filtered and per-source searches.
      On the tuning pool it moved one rank below the top 5 and nothing else.
    * Record: `results/frozen-config-v3.json` (ranking, code, model, question and chunk hashes) and
      `results/diagnose-v3-tuning-pool.json`.
16. **Disclosure.** Before the freeze, the question text of x22 (unanswerable) was run once through the CLI as a
    smoke test of the new signals output. No ranking decision was taken from it, and the signals never change the
    order. Apart from that, no search results for x were looked at before its run. Its evidence was chosen by
    exact-phrase lookup.
17. **Sealed set x, run once** with v2 (Markdown index, `results/local-fts5-v2-14pack-x-heldout.json`) and once
    with v3 (`results/local-hybrid-v3-14pack-x-heldout.json`).
18. **Diagnosis after that run**, with nothing tuned: the failures' ranks to depth 50, the coverage signal per
    question, and per-source scoring of the cross-source questions (`results/hybrid-v3-x-heldout-diagnosis.json`).
    After the run, only text changed: the calibration comment in `search.py` (`git diff d2d144a --
    src/irpacks/search.py`) and the MCP server instructions, which now warn that coverage cannot detect a
    non-existent version. The ranking did not change.

19. **Final-build verification.** After the run, FIRST and SWGDE were cleared for distribution. That changed rights
    metadata only. A layout-route fix also changed the text of some SWGDE chunks: four cover pages, and
    de-duplicated Linux tables. The index was rebuilt (12,364 chunks) and every set rerun with the frozen
    rankings, with nothing tuned: `results/final-build-*.json`.
    * Set x gave the same results: v3 13/20, MRR 0.525; v2 11/20, MRR 0.432. Every evidence rank was unchanged.
    * Every other set gave the same results, except x-tuning: xt12's first evidence moved from rank 4 to 3 (MRR
      0.399 → 0.406).
    * Some top-5 lists swapped two adjacent non-evidence results with near-equal scores.

    Set x has now been used, and it joins the tuning pool.
20. **Sealed set y written**, before any v4 experiment (commit b231b05).
21. **Practitioner test** (`docs/practitioner-test-2026-10-27.md`): 33 incident-time questions through the MCP
    tools, with v3. The questions were written after y was sealed, and none targets a y question.
    * **Disclosure.** One comparison question (Defender for Endpoint and AWS on isolating a host) touches the topic
      of y23 (AWS and Azure on isolating a VM). Its AWS results did not include y23's evidence. Nothing was tuned on
      it.
22. **Ranking v4 chosen** on the tuning pool, which now includes x, and on the practitioner questions.
    * Frozen in commit 0f27fee. Record: `results/frozen-config-v4.json` (ranking, index changes, code, model,
      question and chunk hashes) and `results/diagnose-v4-tuning-pool.json`.
    * Rejected on the tuning pool:
      * two cross-encoder re-rankers (MiniLM-L6, bge-reranker-base): no gain, and 1.8–10 s per search;
      * query focus: fewer first hits in the top 3;
      * a "DAIR" source alias: the framework's name is the subject of some questions.
    * On the tuning pool (79 answerable, `diagnose_ranks.py`):
      * evidence complete: 55 (v3) → 56;
      * first hits at rank 1: 39 → 39;
      * MRR: 0.587 → 0.582;
      * multi-source questions complete in one search: 2/11 → 3/11; per source, 7/11 under both.
23. **Sealed set y, run once** with each ranking:
    * v3, on a copy of the index as it was before v4's index changes (no navigation rule, no new aliases):
      `results/hybrid-v3-14pack-y-heldout.json`;
    * v4: `results/hybrid-v4-14pack-y-heldout.json`.

    The questions file matched the hash in the freeze record.
24. **Diagnosis after that run**, with nothing tuned: `results/hybrid-v3-y-heldout-diagnosis.json` and
    `results/hybrid-v4-y-heldout-diagnosis.json`. They give:
    * the evidence ranks to depth 50;
    * the coverage level and best similarity of each question;
    * per-source scoring of the cross-source questions.

    Every tuning-pool set was also rerun with v4 for the table below (`results/hybrid-v4-14pack-*.json`).

Sets x and y have now been used. Any future ranking change must be judged on a **new** held-out set.

## Results (k = 5)

| Run | Evidence complete | Group recall | MRR | Citations valid | Required context | Duplicates | Failures |
|---|---|---|---|---|---|---|---|
| Local FTS5, tuning (selected on) | 12/14 | 0.893 | 0.833 | 13/13 | 3/3 | 0/75 | t04, t11 |
| Local FTS5, **held-out** | 7/8 | 0.938 | 0.738 | 8/8 | 5/5 | 0/50 | h07 |
| forensic-rag, tuning (defaults) | 10/14 | 0.750 | 0.750 | 11/11 | 4/4 | 1/75 | t06, t07, t10, t11 |
| forensic-rag, **held-out** (defaults) | 6/8 | 0.812 | 0.812 | 7/7 | 4/4 | 0/50 | h05, h07 |
| Public candidate index, all | 19/22 | 0.909 | 0.806 | 21/21 | 8/8 | 0/125 | t04, t11, h07 |
| Three-pack index, tuning | 12/14 | 0.893 | 0.845 | 13/13 | 3/3 | 0/75 | t04, t11 |
| Three-pack index, held-out | 7/8 | 0.938 | 0.750 | 8/8 | 5/5 | 0/50 | h07 |
| Three-pack index, **Microsoft held-out** | 4/7 | 0.571 | 0.500 | 4/4 | 1/1 | 0/40 | m03, m04, m05 |
| Eight-pack index, tuning | 12/14 | 0.893 | 0.839 | 13/13 | 3/3 | 0/75 | t04, t11 |
| Eight-pack index, held-out | 7/8 | 0.938 | 0.692 | 8/8 | 5/5 | 0/50 | h07 |
| Eight-pack index, Microsoft held-out | 4/7 | 0.571 | 0.369 | 4/4 | 1/1 | 0/40 | m03, m04, m05 |
| Eight-pack index, **vendor held-out** | 7/11 | 0.636 | 0.374 | 7/7 | 1/1 | 0/60 | v02, v04, v09, v11 |
| Ten-pack index, tuning | 12/14 | 0.893 | 0.839 | 13/13 | 3/3 | 0/75 | t04, t11 |
| Ten-pack index, held-out | 7/8 | 0.938 | 0.692 | 8/8 | 5/5 | 0/50 | h07 |
| Ten-pack index, Microsoft held-out | 4/7 | 0.571 | 0.369 | 4/4 | 1/1 | 0/40 | m03, m04, m05 |
| Ten-pack index, vendor held-out | 7/11 | 0.636 | 0.374 | 7/7 | 1/1 | 0/60 | v02, v04, v09, v11 |
| Ten-pack index, **CISA held-out** | 4/7 | 0.571 | 0.464 | 4/4 | 0/0 | 0/40 | c04, c06, c07 |
| Fourteen-pack index, v2: tuning / held-out | 12/14 / 7/8 | 0.893 / 0.938 | 0.839 / 0.660 | 13/13 / 8/8 | 3/3 / 5/5 | 0/75 / 0/50 | t04, t11 / h07 |
| Fourteen-pack index, v2: Microsoft / vendor / CISA | 4/7 / 7/11 / 4/7 | 0.571 / 0.636 / 0.571 | 0.369 / 0.370 / 0.464 | 4/4 / 7/7 / 4/4 | 1/1 / 1/1 / 0/0 | 0 duplicates | m03–m05 / v02, v09, v10, v11 / c04, c06, c07 |
| Fourteen-pack index, v2: x-tuning | 5/12 | 0.417 | 0.333 | 5/5 | 0/0 | 0/65 | xt01–xt04, xt06, xt10, xt12 |
| **v3** (tuning pool; chosen on these): tuning / held-out | 12/14 / 7/8 | 0.893 / 0.938 | 0.818 / 0.838 | 13/13 / 8/8 | 3/3 / 5/5 | 0/75 / 0/50 | t04, t11 / h07 |
| **v3** (tuning pool): Microsoft / vendor / CISA | 5/7 / 8/11 / 4/7 | 0.786 / 0.727 / 0.571 | 0.548 / 0.503 / 0.500 | 6/6 / 8/8 / 4/4 | 1/1 / 1/1 / 0/0 | 0 duplicates | m04, m07 / v02, v10, v11 / c04, c06, c07 |
| **v3** (tuning pool): x-tuning | 6/12 | 0.542 | 0.399 | 7/7 | 0/0 | 0/65 | xt01–xt04, xt06, xt12 |
| Sealed set x, **v2**, run once | 11/20 | 0.600 | 0.432 | 13/13 | 0/0 | 0/115 | x03, x04, x08, x14, x16–x20 |
| Sealed set x, **v3**, run once | **13/20** | **0.675** | **0.525** | 14/14 | 1/1 | 0/115 | x03, x04, x06, x14, x18–x20 |
| **v4** (tuning pool; chosen on these): tuning / held-out | 13/14 / 7/8 | 0.964 / 0.938 | 0.806 / 0.775 | 14/14 / 8/8 | 4/4 / 5/5 | 0/75 / 0/50 | t11 / h07 |
| **v4** (tuning pool): Microsoft / vendor / CISA | 5/7 / 7/11 / 4/7 | 0.786 / 0.636 / 0.571 | 0.576 / 0.462 / 0.571 | 6/6 / 7/7 / 4/4 | 1/1 / 1/1 / 0/0 | 0 duplicates | m04, m07 / v02, v04, v10, v11 / c04, c06, c07 |
| **v4** (tuning pool): x-tuning / x | 7/12 / 13/20 | 0.583 / 0.650 | 0.419 / 0.517 | 7/7 / 13/13 | 0/0 / 1/1 | 0/65 / 0/115 | xt01–xt04, xt06 / x03, x04, x06, x14, x18–x20 |
| Sealed set y, **v3**, run once | 12/26 | 0.538 | **0.342** | 16/16 | 2/2 | 0/150 | y02–y05, y09, y11, y13, y16, y21–y26 |
| Sealed set y, **v4**, run once | **14/26** | **0.596** | 0.289 | 17/17 | 1/1 | 0/150 | y03, y04, y06, y09, y11, y13, y16, y22–y26 |

**Failure notes:**
* **t04** is a vocabulary mismatch: "confirm containment worked" against "Validate Containment Effectiveness".
  Dense retrieval found it.
* **t11 and h07** are cross-source. Both backends return one source's evidence but not the other's.
* **h05** needs the CSF row `RS.MA-01`. The lexical index finds it; forensic-rag did not.
* **m03:** book passages on cloud and token attacks (ch18, ch13) and the token theft playbook's introduction
  outrank the playbook's investigation-triggers list. The evidence calls the detection "Anomalous token"; the
  question says "stolen and replayed session token".
* **m04:** all five results come from the privileged-accounts monitoring article. They include its own "Emergency
  access accounts" section at rank 3, but none come from the emergency-access article that holds the evidence.
* **m05:** the right article, but the wrong sections. The app consent playbook's prevention and remediation
  sections rank above its attack-techniques list.
* These were diagnosed, not tuned. The cross-source question m07 was complete, with the book passage at rank 1 and
  the Microsoft step at rank 5.
* **v02:** "process creation events" matches the Windows event 4688 reference and other advanced hunting tables
  (logon, image load) ahead of the process events table.
* **v04, v09:** the right article (the Purview auditing overview; the CloudTrail `userIdentity` reference), but
  other sections of it rank above the evidence section.
* **v11:** cross-source again. A Defender isolation chunk is at rank 5, but not the one holding the evidence
  element; the book's containment chapter (ch12) is not in the top 5.
* **v12** (unanswerable): retrieval returned book memory-forensics and ransomware passages; a generator would have
  to abstain.
* **c04, c06:** generic ransomware questions that name no source. Sections of the book's ransomware chapter
  (ch17: Scope, Ransomware-Specific Metrics, Attack Trends) and Microsoft's DART ransomware playbook fill the top 5,
  ahead of the #StopRansomware checklist item and best practice that hold the evidence.
* **c07:** cross-source. The question names both sources, but the two reserved alias slots are shared by every
  named pack, and both go to the book's ch17 chunks. No #StopRansomware chunk and not the ch12 table reaches the
  top 5. A per-pack reservation would be a ranking change, judged only on a new held-out set.
* **c01** is complete at rank 4: the Appendix B checklist step, after the key-terms table, the introduction and
  Appendix E.
* **c08** (unanswerable): retrieval returned book ransomware passages (ch17, ch12); a generator would have to
  abstain.
* **Index growth.** No question lost its evidence when the index grew from three to eight packs, but first hits
  moved down: held-out MRR 0.750 → 0.692, Microsoft set 0.500 → 0.369. The packs are about ten times larger in
  total, and generic IR vocabulary now matches many vendor sections. Restricting a query to packs (`--filter-pack`,
  or the MCP `packs` filter) or naming the source (aliases such as "Defender XDR", "GuardDuty") reserves results for
  it. Any ranking change needs a new held-out set. Adding the two CISA packs (108 chunks) changed no earlier
  result.

**Ranking v3 and the sealed set x** (diagnosed after the single run; nothing tuned):
* **What v3 changed on x.** Three questions were gained: x08 (event ID for a new service, now rank 1), x16 (memory
  before shutdown, rank 1) and x17 (packet capture in Azure and Google Cloud, both sides in the top 5). One was
  lost: x06.
  * The gain is two questions out of 20, a small sample.
  * On the tuning pool, v3 gained 3 of 59 (39 → 42), with 31 first hits instead of 26.
  * v3 also lost m07 on the tuning pool (cross-source: the book passage stays at rank 1, the Microsoft step drops
    out of the top 5).
* **x06** (purge a phishing email from every mailbox): two Purview articles cover this.
  * "Search for and delete email messages" ranks 1, and its "Step 3: Delete the message" section ranks 5. That
    section describes `New-ComplianceSearchAction -Purge` and soft versus hard delete.
  * The evidence article, "Find and delete email messages in eDiscovery", ranks 4 by its introduction. Its
    hard-delete section, with the `-PurgeType HardDelete` command, ranks 15. v2 had it in the top 5.
  * The evidence was chosen before the run and lists only the eDiscovery article's elements. It is not widened
    after the run, so x06 counts as a miss.
* **x03** (leaked service account key): Google Cloud's "don't submit keys to source code repositories" and
  "create a key" sections rank 2–4. The response steps (block the token, replace the key) are not in the top 50.
  The question's urgency ("what do we do right now") shares no words with "respond to compromised service
  accounts".
* **x04** (are data reads logged by default?): four of the five results are Google Cloud Data Access audit log
  sections. The evidence list item, from the audit log overview, ranks 35.
* **x14** (keep the cloud provider from losing the data): the evidence speaks of "preservation requests"; the
  question of not losing data. Not in the top 50: a vocabulary gap that the embeddings did not bridge.
* **Cross-source (x17–x20).** One combined search completed 1 of 4 (x17); one search per source completed 2 of 4
  (x17, x19).
  * x19: the book's chain-of-custody item ranks 2 and SWGDE's ranks 10 in a combined search. Each source's own
    search finds its own item.
  * x18: per source, Cloud Storage soft delete is found. On the Azure side, Azure Backup security articles fill
    the top 5, ahead of the ransomware-preparation article's immutable backups and immutable blobs.
  * x20: per source, Entra's revoke-access overview is found. On the Google Cloud side, neither the
    service-account blocking step nor the revoking-user-access page reaches the top 5.
* **Unanswerable (x21–x23).** Retrieval returned:
  * x21: CVSS 3.1 equations and CVSS 4.0 passages;
  * x22: SWGDE Linux, model SOP and mobile device passages;
  * x23: the CISA vulnerability response playbook first.

  A generator would have to abstain. The coverage signal (below) flagged two of the three.

**Ranking v4 and the sealed set y** (diagnosed after the single run; nothing tuned):
* **Result.** v4 completed 14 of 26 answerable questions, and v3 completed 12.
  * v4 gained y02 (CloudTrail history without a trail: rank 8 → 2), y05 (the event for a cleared security log: not
    in the top 50 → 3) and y21 (message trace retention: 7 → 3).
  * It lost y06 (3 → 9).
  * Two questions out of 26 is not a statistically meaningful difference.
* **v4 put first hits lower.** MRR fell from 0.342 to 0.289, and first hits at rank 1 from 5 to 3. Of the questions
  both rankings found, v4 ranked the evidence lower in five: y01 (1 → 5), y18 (1 → 2), y12 (2 → 4), y17 (2 → 5) and
  y06 (3 → 9).
  * The pattern: v4 weights the question's words more (BM25 0.3 and coverage 0.3, against v3's 0.1 and 0.2) and
    similarity less (0.4 against 0.7). Passages that repeat the question's general words then outrank the specific
    answer.
  * y01 (which part of a Defender investigation package shows persistence): the book's sections on removing
    persistence take ranks 1–3, and the package contents table drops to 5.
  * y06 (DCSync and forged Kerberos tickets): Kerberos event references (4768, 4771) and Defender's Golden Ticket
    alerts fill the top 5, ahead of the book's krbtgt reset.
  * y17 (a rootkit: is reimaging enough?): Google Cloud's kernel-rootkit pages take four places above CISA's
    checklist step.
  * The gains are the opposite case: the question names a product, and the answering section now leads that
    product's pages.
  * On the tuning pool this trade-off was small (first hits at rank 1: 39 under both; MRR 0.587 → 0.582). On y it is
    larger.
* **Cross-source (y22–y26).** No cross-source question was complete in one combined search under either ranking.
  Searching each source separately, as `per_source` does, v3 completed 2 of 5 (y23, y26) and v4 none. On the tuning
  pool both had completed 7 of 11.
  * y23 (AWS and Azure on isolating a VM): Azure's evidence ranks 1 in Azure's own search under both rankings. AWS's
    "supported containment actions" ranks 4 under v3 and 6 under v4.
  * y26 (the book and CISA on law enforcement before eradication): CISA's step ranks 3 under both. The book's
    passage ranks 5 under v3 and 11 under v4.
  * y22: NIST's lessons-learned passage ranks 6 (v3) and 7 (v4) within NIST. CISA's ranks 1.
  * y25: the book's persistence checklist ranks 15 (v3) and 13 (v4) within the book. CISA's ranks 1.
  * y24: neither CloudTrail's management events nor Google Cloud's Admin Activity audit logs reach the top 30 of
    their own source. The results describe specific grants (the Service Account User role, Kubernetes admin access)
    rather than the log types.
* **Missed under both rankings:**
  * y03 (stop service account key creation across the organization): key create, rotate and delete sections rank
    1–5. The organization policy constraint ranks 8.
  * y04 (how long deleted Azure Backup data can be recovered): the right article ranks 1 by its introduction. Its
    soft-delete retention passage ranks 10 under v4 and is not in v3's top 50.
  * y09 (CVSS 4.0 `E:X`): the vector-string sections rank first. The Exploit Maturity table that defines `X` is not
    in the top 50, and `E:X` is not matched as a term.
  * y11 (keep a seized phone from being wiped remotely): "wiped" draws email purge and remote-worker passages.
    SWGDE's airplane mode and RF shielding steps are not in the top 50 (v3: 36).
  * y13 (show that a disk image has not changed): "disk image" draws cloud disk snapshot pages. SWGDE's hash
    verification passages are not in the top 50.
  * y16 (how a user clears their own Entra risk): administrator remediation sections rank first. Self-remediation
    ranks 12 (v3: 21).
* **Unanswerable (y27–y30).** Retrieval returned:
  * y27: the TLP 2.0 definitions, which have no TLP:BLACK;
  * y28: SWGDE imaging procedures for other devices;
  * y29: the book's passages on NIST SP 800-61 Revision 3;
  * y30: retention pages of other products (Entra, Purview audit, Defender).

  A generator would have to abstain, and for y29 and y30 notice that the passage is about another version or
  product. v4's signal said `low` for y27, y29 and y30.
* **Choice of default.** v4 stays the default.
  * It completed more questions on y and on the tuning pool, and its `low` warning flags unanswerable questions
    more often (below).
  * Its cost is lower first hits on y and weaker per-source results on y.
  * Both rankings remain available (`--ranking v3`), and every response reports the configuration it used.

### Coverage signal: what it predicts

`search` reports, per result, the share of the question's key words its best passage contains. Words naming a
targeted source (an alias) are not counted.

Per response it also reports a level:
* **v3** takes the level from that share alone: `high` ≥ 0.75, `medium` ≥ 0.5, `low` below.
* **v4** also uses the best result's embedding similarity to the question. The level is `low` below 0.73, and
  `high` from 0.78 when the best passage also has most of the key words (≥ 0.75). Anything else is `medium`.

The thresholds were set on the tuning pool, and v4's also on the practitioner questions. The signal measures
closeness, not correctness. The numbers below are the responses' own `coverage` on the fourteen-pack index. The
first four rows are v3's.

| Set | Answerable, evidence in top 5 | Answerable, evidence missed | Unanswerable |
|---|---|---|---|
| Tuning pool (59 + 7): median best coverage | 0.80 | 0.74 | 0.49 |
| Tuning pool: levels | high 22, medium 18, low 2 | high 8, medium 8, low 1 | high 1, medium 2, low 4 |
| Sealed set x (20 + 3): median best coverage | 0.70 | 0.57 | 0.40 |
| Sealed set x: levels | high 5, medium 6, low 2 | high 2, medium 4, low 1 | medium 1, low 2 |
| Tuning pool with x, v4, thresholds set on it (56 + 23 + 10): median best coverage / best similarity | 0.79 / 0.807 | 0.73 / 0.796 | 0.45 / 0.704 |
| Tuning pool with x, v4: levels | high 22, medium 31, low 3 | high 9, medium 12, low 2 | medium 3, low 7 |
| Sealed set y, v3 (12 + 14 + 4): median best coverage | 0.52 | 0.60 | 0.67 |
| Sealed set y, v3: levels | high 1, medium 6, low 5 | high 2, medium 8, low 4 | high 1, medium 2, low 1 |
| Sealed set y, v4 (14 + 12 + 4): median best coverage / best similarity | 0.47 / 0.767 | 0.56 / 0.785 | 0.56 / 0.719 |
| Sealed set y, v4: levels | medium 11, low 3 | medium 9, low 3 | medium 1, low 3 |

**On y:**
* v4's `low` went to 3 of the 4 unanswerable questions (v3: 1) and to 6 of the 26 answerable ones (v3: 9). Three
  of those six were complete.
* v4 gave `high` to no question. v3 gave it to two misses, and to y29, which asks about a NIST revision that does
  not exist.
* **Neither signal predicts whether the evidence is in the top 5.** Under both rankings, the missed questions had a
  higher median coverage than the complete ones, and under v4 a higher median similarity. Similarity separates the
  unanswerable questions (median 0.719) from the answerable ones (0.767 and 0.785). It does not separate right
  passages from wrong ones.

**On the tuning pool and x:**

* **`low` is a useful warning, not a verdict.** Under v3 it went to 6 of the 10 unanswerable questions, and to 6 of
  the 79 answerable ones. Under v4 (whose thresholds were chosen on these questions) it went to 7 and 5.
  * On x under v3, two of the three answerable `low` questions had their evidence at rank 1. In x09,
    "TLP:AMBER+STRICT" is one token in the passage, so the question's "AMBER+STRICT" was not counted as matched;
    v4 matches compound tokens by their parts. x12 is a short question about live acquisition.
* **Non-existent versions are not detected by the words.** Under v3, xt13 (TLP 3.0, tuning) scored `high` and x21
  (CVSS 5.0) `medium`: "3.0" and "5.0" occur in the packs as other versions or as score values. Under v4, xt13 and
  y29 (NIST SP 800-61 Revision 4) are `low` and x21 is still `medium`. When a question names a version, check that
  the passage is about that version.
* **`high` does not guarantee the evidence.** Under v3, 8 tuning questions and 2 on x (x04, x18) were `high` and
  missed it; under v4, 9 across the pool.
* The levels help most as a reason to keep searching: `low` and `terms_in_no_result` name the part of the
  question that nothing matched. `medium` and `high` are not evidence that the answer is in the results.

Small samples: a difference of one or two questions is not statistically meaningful. See
`docs/retrieval-decision.md` for the comparison method and what it does and does not show.

## Reproduce

```bash
uv run python eval/build_questions.py         # regenerate questions.yaml from the built packs
uv run irpacks eval --split tuning  --out eval/results/local-fts5-tuning.json
uv run irpacks eval --split heldout --out eval/results/local-fts5-heldout.json
uv run python eval/ablate_tuning.py           # tuning split only
uv run python eval/build_questions.py --spec eval/questions-ms.spec.yaml
uv run irpacks eval --split ms-heldout --questions eval/questions-ms.yaml --out eval/results/local-fts5-ms-heldout.json
uv run python eval/build_questions.py --spec eval/questions-vendor.spec.yaml
uv run irpacks eval --split vendor-heldout --questions eval/questions-vendor.yaml --out eval/results/local-fts5-vendor-heldout.json
uv run python eval/build_questions.py --spec eval/questions-cisa.spec.yaml
uv run irpacks eval --split cisa-heldout --questions eval/questions-cisa.yaml --out eval/results/local-fts5-cisa-heldout.json

# ranking v3 (needs `uv sync --extra dense`, `irpacks models fetch`, `irpacks index --dense`)
uv run python eval/diagnose_ranks.py --per-source          # tuning pool only; refuses the sealed split
uv run irpacks eval --split x-tuning --questions eval/questions-x-tuning.yaml --ranking v3 --out eval/results/local-hybrid-v3-14pack-xtuning.json
uv run irpacks index --index-text markdown --out build/local/index-v2.sqlite   # v2 baseline index
uv run irpacks eval --split x-heldout --questions eval/questions-x.yaml --index build/local/index-v2.sqlite --ranking v2 --out eval/results/local-fts5-v2-14pack-x-heldout.json
uv run irpacks eval --split x-heldout --questions eval/questions-x.yaml --ranking v3 --out eval/results/local-hybrid-v3-14pack-x-heldout.json

# ranking v4 (the default). index-v3.sqlite is the index as built at commit b231b05, before v4's index changes:
#   git checkout b231b05 && uv run irpacks index --dense --out build/local/index-v3.sqlite && git checkout -
uv run python eval/build_questions.py --spec eval/questions-y.spec.yaml
uv run irpacks eval --split y-heldout --questions eval/questions-y.yaml --index build/local/index-v3.sqlite --ranking v3 --out eval/results/hybrid-v3-14pack-y-heldout.json
uv run irpacks eval --split y-heldout --questions eval/questions-y.yaml --ranking v4 --out eval/results/hybrid-v4-14pack-y-heldout.json
# forensic-rag: see docs/retrieval-decision.md (separate environment; eval/backends/forensic_rag.py)
```
