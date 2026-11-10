# Dependency licenses

The project's own code is Apache-2.0. The tables below list the locked Python dependencies (`uv.lock`) installed in
the project environment on 2026-10-25, with the license declared in each package's metadata. This is a summary,
not legal advice. The authoritative terms are those shipped with each package.

## Runtime

| Package | Version | Declared license | Used for |
|---|---|---|---|
| pdfplumber | 0.11.10 | MIT | PDF character, word and image extraction |
| pdfminer.six | 20260107 | MIT | PDF parsing, including the structure-tree walker for tagged PDFs |
| pypdfium2 | 5.14.0 | BSD-3-Clause, Apache-2.0 (bundles PDFium and its dependencies' licenses) | Rendering, bookmarks, page labels, image pixels |
| lxml | 6.1.3 | BSD-3-Clause | HTML parsing (`huge_tree`; no network access) |
| jsonschema | 4.26.0 | MIT | Schema validation |
| jsonschema-specifications | 2025.9.1 | MIT | (jsonschema dependency) |
| referencing | 0.37.0 | MIT | (jsonschema dependency) |
| rpds-py | 2026.9.1 | MIT | (jsonschema dependency) |
| attrs | 26.1.0 | MIT | (jsonschema dependency) |
| PyYAML | 6.0.3 | MIT | Pack contracts, recipes, review records |
| Pillow | 13.0.0 | MIT-CMU (HPND) | Image checks and crops |
| charset-normalizer | 3.5.2 | MIT | (pdfminer.six dependency) |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | (pdfminer.six dependency, for encrypted PDFs) |
| cffi | 2.1.1 | MIT-0 | (cryptography dependency) |
| pycparser | 3.12 | BSD-3-Clause | (cffi dependency) |

Python standard library: `sqlite3`, which must be built with FTS5, plus `tarfile`, `gzip` and `hashlib`.

## Optional extra `dense` (local embedding search, rankings v3 and v4)

| Package | Version | Declared license | Used for |
|---|---|---|---|
| onnxruntime | 1.31.0 | MIT | Runs the embedding model on CPU |
| tokenizers | 0.23.3 | Apache-2.0 | The model's tokenizer, loaded from a local file (`Tokenizer.from_file`) |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Vectors and similarity |
| flatbuffers, protobuf, packaging | 25.12.19, 7.36.2, 26.3 | Apache-2.0; BSD-3-Clause; Apache-2.0 OR BSD-2-Clause | (onnxruntime dependencies) |
| huggingface-hub | 2.3.0 | Apache-2.0 | (tokenizers dependency; never called: the model files are fetched only by `irpacks models fetch`, with curl, and checked against `models.lock.json`) |

The model itself, BAAI/bge-small-en-v1.5 in Xenova's quantized ONNX conversion (revision `ea104dac`), is
MIT-licensed. It is not a Python package and is not redistributed by this project.

The optional extra `mcp` installs the MCP Python SDK (`mcp` 2.3.0, MIT) and its dependencies.

## Development and test only

| Package | Version | Declared license |
|---|---|---|
| pytest | 9.1.1 | MIT |
| pluggy | 1.7.0 | MIT |
| iniconfig | 2.3.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| Pygments | 2.21.0 | BSD-2-Clause |

## Build backend

* hatchling 1.28.0 (MIT), pinned in `pyproject.toml`.

## Optional external tools (not dependencies, separate environments)

| Tool | Version tested | License | Notes |
|---|---|---|---|
| Countermap | 0.2.2 (PyPI) | Apache-2.0 AND BSD-3-Clause (package metadata) | Called through its CLI. Its framework data is under MITRE ATT&CK terms of use and the D3FEND, ATLAS and F3 licenses (see `countermap info`). |
| AppliedIR sift-mcp forensic-rag (rag-mcp) | 0.6.5.1 at commit 5ef6081a | MIT | Used only for the comparison in `eval/backends/forensic_rag.py`. It pulls chromadb, sentence-transformers and torch (about 5.9 GB). |
| BAAI/bge-base-en-v1.5 | revision a5beb1e3 | MIT (model card) | Embedding model used by forensic-rag in the comparison |
| Hydrant | commit fecd1698 | MIT | Reviewed, not installed or run |

## Content licenses

Content licenses are separate from code licenses. See `NOTICE`, each pack's `rights.yaml` and the built
`ATTRIBUTION.md`.
