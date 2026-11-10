# Practitioner pilot guide (prepared, not started)

This guide is for a small pilot with **three to five external incident-response practitioners**. The material is
ready, but **nobody has been recruited or contacted**, and no feedback exists yet. Recruiting, sending invitations
and collecting responses are the maintainer's decisions and actions.

## Goals

The pilot measures what automated evaluation cannot:

| Measure | How |
|---|---|
| Installation friction | Time from clone to the first successful `irpacks search`; every blocking step written down |
| Retrieval omissions | Tasks where the participant knows the book covers something but search or context did not surface it |
| Citation mistakes | Any citation that points to the wrong page or section, checked against the PDF |
| Context loss | Answers that were wrong or unsafe because a warning, lead-in, note or table header was missing |
| Repeat use | Whether the participant would use the pack again, for what, and what would stop them |

## Setup for participants

1. Prerequisites:
   * a laptop with Python ≥ 3.11 and `uv`;
   * about 400 MB of disk. Measured in the dry run: sources 173 MB, environment 74 MB, builds, index and
     extraction cache 135 MB;
   * network access for the opt-in source download, or the PDF supplied by the participant. The book is free to
     download from the publisher.
2. Clone the repository, which the maintainer will share privately. Then run:

   ```bash
   uv sync
   uv run irpacks sources fetch dynamic-ir && uv run irpacks sources fetch nist-sp800-61r3
   uv run irpacks sources verify
   uv run irpacks build dynamic-ir && uv run irpacks build nist-sp800-61r3 && uv run irpacks index
   ```

   `sources verify` should end with `ok: 0 build-input problem(s)`. Packs you did not fetch (the Microsoft and AWS
   web packs) are listed once each as `not fetched`. Two kinds of non-`ok` line are expected and harmless:
   * `not_supplied` for `dynamic-ir/pdf/1e-20260914175315-user-inspected`, a registered identity for a reader's
     copy that the build does not need;
   * `changed` for `nist-sp800-61r3/site/csrc-final`, a landing page whose HTML embeds per-request tokens.

   A `MISMATCH` on a PDF or HTML input means the file is not the locked edition. Stop and contact the facilitator.

   Measured in the facilitator dry run (fresh clone, empty package cache, 4 vCPU, 2026-10-25; see
   `docs/pilot-dryrun-2026-10-25.md`):

   | Step | Time |
   |---|---|
   | `uv sync` | 5 s |
   | Source download | 16 s |
   | Both builds plus the index | 135 s (peak memory 1.45 GB) |
   | **Clone to first search** | **about 2.6 minutes** |
3. Optional: connect an AI agent of your choice. Point it at `build/local/<pack>/AGENT_GUIDE.md` and let it call
   `irpacks search` and `irpacks context`. **Do not upload the book or pack contents to an external service unless
   you have decided that is acceptable for your organization.**

## Synthetic case (same for every participant)

> **Synthetic.** Example Manufacturing Ltd. (fictional) receives a call from a supplier asking why an invoice is
> overdue. Finance says it was paid to the supplier's "new" bank account two days ago. The accounts-payable
> clerk's mailbox shows an inbox rule forwarding invoice email to an external address. Entra ID shows a new
> authenticator registered on the clerk's account the day before, from an unfamiliar location.

Tasks, about 60 minutes in total. Use the packs, and an agent if you like. For every answer, write down the
citations you relied on.

1. **Verify and triage.** What should you confirm first, and how do you decide whether this is an incident and how
   severe it is?
2. **Contain identity access.** List the containment steps for the clerk's account in order, including sessions,
   refresh tokens, MFA methods and mailbox rules. Note any warnings the packs attach to these steps.
3. **Scope.** Which other accounts, mailboxes and finance processes should you check, and what evidence would you
   collect?
4. **Payment.** What does the book say about tracing the fraudulent transaction and coordinating with finance?
5. **Framework view (optional).** Run `uv run irpacks countermap-example` if you have Countermap. Do the
   provenance labels make clear which links come from the book and which from an analyst?
6. **NIST alignment.** Which NIST SP 800-61r3 / CSF 2.0 functions and categories does your response touch?
7. **Free exploration.** Ask the packs three questions from your own recent work.

## Facilitator checklist

* Use the feedback template (`docs/feedback-template.md`). Record answers verbatim, with no prompting.
* For every reported citation error, open the PDF page and record the actual page.
* Record the participant's environment: OS, Python version, and agent/model if any.
* Do not share participants' own incident details in project artifacts.
* Summarize findings into the issue ledger only with the participant's permission.
