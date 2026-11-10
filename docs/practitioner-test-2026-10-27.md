# Practitioner test, 2026-10-27

An AI agent (Claude), acting as an incident responder, put 33 incident-time questions to the packs. It asked them
through the MCP server's own `search` tool, as a connected assistant would, and read the top five results of each.
This is an informal usability test with one tester's judgement, not a measurement:
* the questions and verdicts are not part of any evaluation set;
* none of them targets a sealed held-out question.

One comparison query (Defender for Endpoint and AWS on isolating a host) touches the topic of one sealed question.
Its AWS results did not include that question's evidence. Nothing was tuned on it.

Ranking: v3, with dense retrieval on, on the fourteen-pack index. The raw result lists are not kept in the
repository; the questions are listed below. A rerun with ranking v4 is at the end.

## Verdicts

*Good*: the top results answer the question with a citable passage. *Partial*: the answer is there but ranked low,
or only part of it. *Miss*: nothing in the top five answers it.

| # | Scenario | Question (abridged) | Verdict | Notes |
|---|---|---|---|---|
| 1 | BEC | find and remove an inbox rule hiding the bank's replies (CFO, O365, "popped") | Good | Book BEC investigation, Defender Outlook-rules remediation, Purview mailbox-rule audit. Coverage said `low` (slang words): a false alarm |
| 2 | BEC | which emails did the attacker actually read | Partial | Book BEC identity steps and Microsoft's phishing "did the user read" step; the mailbox-access audit reference is not in the top 5 |
| 3 | BEC | AiTM phishing stole a session cookie, MFA bypassed | Miss | The Defender XDR "session cookie theft" playbook is not in the packs (coverage gap); the Entra token theft playbook is not in the top 5 |
| 4 | BEC | did the attacker grant a malicious OAuth app mailbox access | Good | Defender for Cloud Apps OAuth alert investigation |
| 5 | AWS | access key pushed to a public GitHub repo an hour ago | Miss | GitHub hunting-query pages outrank "Remediating potentially compromised AWS credentials" (not in the top 100). Coverage `low`: correct |
| 6 | AWS | GuardDuty InstanceCredentialExfiltration.OutsideAWS, what next | Good | The finding's own entry |
| 7 | Ransomware | spreading across file servers, first hour | Partial | Book's immediate containment triggers at 5; the #StopRansomware response checklist at 23 |
| 8 | Ransomware | how to report to CISA and what they want | Good | #StopRansomware reporting; CISA "notify within 1 hour" |
| 9 | Ransomware | common initial access vectors | Good | #StopRansomware initial access sections |
| 10 | Google Cloud | investigate access with compromised user credentials | Good | Google's compromised-credentials guide |
| 11 | Linux | which logs show SSH logins and sudo | Miss | SWGDE's Linux log-file table at 13 |
| 12 | Linux | where attackers add cron and systemd persistence | Good | Book's Linux persistence removal; SWGDE scheduled tasks |
| 13 | Windows | PsExec lateral movement: which event IDs | Partial | Defender for Identity lateral-movement alerts; the service-install event (4697) is not in the top 100 |
| 14 | Windows | what event 4648 means | Good | The event reference |
| 15 | Azure | who deleted a resource group yesterday | Miss | Tutorial "Clean up resources" sections; the activity log at 6 |
| 16 | Vulnerability | compute a CVSS 4.0 environmental score | Partial | CVSS 4.0 documents, but nomenclature sections first |
| 17 | Vulnerability | PSIRT intake of a researcher's report | Good | PSIRT Services Framework 7.2 |
| 18 | Legal | what a chain-of-custody form records for a litigation laptop | Partial | Azure chain-of-custody article; SWGDE myths |
| 19 | SOC | MFA fatigue (push bombing) detection and response | Miss | Coverage `low`: correct |
| 20 | SOC | Sentinel: turn hunting results into an incident | Good | Link query results to an incident |
| 21 | Program | who is on the IR team, which roles | Good | Google, NIST 2.2, book |
| 22 | Comparison | Defender for Endpoint vs AWS on isolating a host (per source) | Partial | Defender: an unrelated Defender for Storage page first, isolation second. AWS: EC2 remediation third |
| 23 | Judgement | contain now or keep watching | Good | Book: assess containment urgency and strategy |
| 24 | Judgement | scope a ransomware incident before recovery | Good | Book ch. 17 scope |
| 25 | Communications | talking to executives during a major incident | Partial | Book debrief; CISA communications |
| 26 | Standards | TLP:AMBER vs TLP:AMBER+STRICT | Good | TLP 2.0 definitions. Coverage `low`: a false alarm ("AMBER+STRICT" against "TLP:AMBER+STRICT") |
| 27 | Standards | CSIRT services according to FIRST | Good | CSIRT Services Framework |
| 28 | Comparison | NIST and the book on measuring IR performance (per source) | Miss | "the book" was not a source name; "say" and "book" polluted each source's search |
| 29 | Comparison | phishing steps, Microsoft vs CISA (per source) | Partial | Microsoft: link lists ("Extra incident response playbooks") ahead of the playbook steps |
| 30 | Out of scope | CrowdStrike RTR command to kill a process | Not covered | Coverage `low`: correct |
| 31 | Out of scope | Splunk SPL for failed logons | Not covered | Coverage `low`: correct |
| 32 | Out of scope | reset MFA for an Okta super admin | Not covered | Coverage `high`: wrong (Defender's Okta posture assessments, not reset steps) |
| 33 | Regulation | GDPR breach notification deadline | Good | Book: seventy-two hours. Coverage `high`: correct |

In scope (30 questions): 16 good, 8 partial, 6 miss. Out of scope (3): the signal warned on 2.

Latency: 0.2-0.4 s per search once warm; the first search after start-up took 11 s (loading the embedding model and
vectors).

## Compared with the vendors' own documentation search

The same questions went to the official Microsoft Learn and AWS documentation MCP servers:
* AiTM (#3): Microsoft's server returned the session-cookie-theft alert playbook and "Protecting tokens", with
  investigation steps.
* Leaked AWS key (#5): AWS's server returned "What to do if you inadvertently expose an AWS access key" and the
  Trusted Advisor exposed-keys check.

Both did better than the packs on their own products. They search all of the vendor's documentation, live.

## What this says

* **Strengths.**
  * Judgement and process questions: the book's step-by-step guidance on containment timing and scoping.
  * Government and community guidance: CISA, #StopRansomware, FIRST, SWGDE.
  * Exact references: event IDs, GuardDuty findings, CVSS metrics.
  * Questions that need more than one source.

  No vendor tool covers these, and every answer comes with an exact citation.
* **Weaknesses.**
  * Vendor-specific how-to questions, where the vendor's own live search is broader and ranks better.
  * Conversational phrasing that leads with incidental words ("GitHub repo", "delete a resource group").
  * Navigation link lists and tutorial clean-up sections that outrank the steps.
* **Signal.** The packs did not answer 9 questions well: 6 misses and 3 out of scope.
  * Coverage said `low` on 5 of them (#5, #19, #28 on the NIST side, #30, #31).
  * It said `high` on 2 of them (#15, #32).
  * It said `low` on 2 good answers (#1, #26).

The changes made in response, and their measured effect, are in `eval/README.md` (ranking v4).

## Rerun with ranking v4

The same 33 questions were rerun through the MCP `search` tool with the frozen v4, on the same index and with the
same options. These questions helped choose v4, so this rerun shows what changed for them. It is not a held-out
measure: v4's held-out result is on the sealed set y (`eval/README.md`).

| # | v3 | v4 | What changed |
|---|---|---|---|
| 1 | Good | Good | Coverage `medium` instead of a false `low` |
| 2 | Partial | **Good** | The mailbox-access audit reference ("Use MailItemsAccessed to investigate compromised accounts") is now at rank 4 |
| 3 | Miss | **Partial** | Entra ID Protection's token theft detections (with the note that they are no longer remediated automatically) and Defender's "Possible use of a stolen session cookie" alert. These cover detection, not response steps; the session-cookie-theft playbook is still not in the packs. Coverage `low` |
| 5 | Miss | **Partial** | "Remediating potentially compromised AWS credentials" at rank 4, below Google Cloud's compromised API keys and two GitHub pages |
| 7 | Partial | Partial | Azure's "What to do during an attack" at 3; the book's containment triggers drop out of the top 5 |
| 11 | Miss | **Good** | SWGDE's Linux log-file table at rank 2. Coverage `low`: a false alarm |
| 15 | Miss | Miss | Azure Backup and Key Vault soft-delete pages fill the top 5. Coverage `low` instead of a wrong `high` |
| 19 | Miss | Miss | Generic detection pages. Coverage `medium` instead of a correct `low` |
| 22 | Partial | Partial | The unrelated Defender for Storage page is gone. Defender's automatic isolation is at 2 (the manual isolate action is not in the top 5); AWS's EC2 remediation, whose step 2 isolates the instance, is at 2 |
| 23 | Good | Good | Coverage `low`: a false alarm |
| 25 | Partial | **Good** | The book's emergency communication plan first and "Communicating with Decision Makers" at 4 |
| 26 | Good | Good | Coverage still `low` (false alarm), now from similarity rather than the AMBER+STRICT token |
| 28 | Miss | **Partial** | "The book" is now a source name. Book: "What Compliance Requires" (IR metrics baselines) at 2. NIST: "performance measures" listed among a policy's elements at 3 |
| 29 | Partial | Partial | Microsoft: the link lists are gone, and phishing investigation steps rank 2–4. CISA: its general IR process (the playbook has no phishing-specific steps) |
| 32 | Not covered | Not covered | Coverage `low` instead of a wrong `high` |
| 33 | Good | Good | Coverage `low`: a false alarm (it was a correct `high`) |

The other questions kept their verdict. Some coverage levels also moved:
* #3 and #11 went from `medium` to `low`;
* #14, #16, #20 and #25 went from `high` to `medium`, and #18 from `low` to `medium`;
* on the per-source questions, the AWS side of #22 and the NIST side of #28 went from `low` to `medium`.

v4 says `high` for 9 questions, where v3 said it for 16.

In scope (30 questions):
* v3: 16 good, 8 partial, 6 miss;
* v4: **19 good, 9 partial, 2 miss**.

The freeze commit's message gives 19 good and 3 misses. That came from a quicker first reading of the same output.
The question-by-question reading in the table above finds 2 misses.

The coverage signal:
* **Questions the packs answer badly or not at all** (2 misses, 3 out of scope): `low` on 4 of 5, against 3 of 5
  under v3. Nothing was `high`; v3 had said `high` for #15 and #32.
* **Good answers flagged `low`:** 4 of 19 (#11, #23, #26, #33), against 2 of 16 under v3. v4 warns more often. A
  `low` therefore means "read the top passages before concluding", as the advice now says, not "not covered".

Latency once warm: median 0.36 s per question (range 0.17–0.62 s).
