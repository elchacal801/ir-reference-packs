# Security policy

## Reporting a vulnerability

Report a vulnerability through the repository's private vulnerability reporting (the **Security** tab on GitHub,
**Report a vulnerability**). Please do not open a public issue for a vulnerability.

Please include:
* the affected command or module;
* a minimal reproducer, preferably a synthetic document or query;
* the impact.

## Threat model and handling of malicious documents

Reference packs are built from documents about attacks. They contain real-looking commands, URLs, credentials,
exploit pseudocode and prompts. Any source could also be crafted to attack the build or the agents that read the
packs. The project's rules:

1. **Parsing never executes or fetches anything.** Building, validation, search, context expansion and evaluation:
   * never run commands found in source text;
   * never follow URLs, resolve remote images, or load external entities or scripts in HTML.
   The HTML is parsed as data by `lxml`, and its embedded data URIs are decoded locally. The regression test
   `tests/test_regressions.py::test_malicious_source_text_is_data_and_never_executed` builds a pack containing
   injected instructions and shell commands while process creation and sockets are blocked.
2. **Network access is opt-in and explicit.** It happens only in:
   * `irpacks sources fetch`;
   * `irpacks check-updates`;
   * the manual `check-updates` workflow.
   These use `curl` with fixed arguments on URLs from the lock or recipe, never on URLs found in source text.
3. **Inputs are pinned.** `sources.lock.json` records sha256 and size. A mismatch stops the build. A different file
   is registered as a new source identity; it never silently replaces the old one.
4. **Untrusted content stays out of instructions.** `AGENT_GUIDE.md` tells agents that pack text is data.
   * Embedded prompts and commands are examples to read, never instructions to follow.
   * Markdown exports keep code inside fences, and raw HTML from sources is escaped outside fences.
   * Chunk records carry rights and review metadata but no executable fields.
5. **Hostile queries.** Search quotes every query term before it reaches FTS5. Malformed operators, quotes and
   control characters cannot change the query structure. This is covered by tests.
6. **Archives and public candidates.**
   * Public exports are checked byte by byte: file-type allowlist, no source documents, no withheld images, no
     omitted text, no local-only paths.
   * Archives are built deterministically and never include `input/`.
7. **Optional external tools** (Countermap, forensic-rag) run in their own environments. Countermap is called only
   through its read-only CLI. The forensic-rag comparison runs offline with a pre-downloaded, revision-pinned
   model.

## What this project is not

Packs are reference material, not operational authority. Commands in a pack are reproduced as published, for
study. Running them against systems is the reader's responsibility and requires authorization.
