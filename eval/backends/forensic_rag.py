"""Run the evaluation questions through sift-mcp forensic-rag (rag-mcp) over the SAME chunks the local index uses.

This script runs in forensic-rag's own virtual environment, not the project's: it imports rag_mcp, chromadb and
sentence-transformers, which are not project dependencies. It writes a results file that `irpacks eval
--results-file` scores with the project's own scorer, so both backends are scored identically.

    HF_HOME=/path/to/hf  /path/to/.venv-rag/bin/python eval/backends/forensic_rag.py \
        --work build/compare/forensic-rag --out eval/results/forensic-rag-raw.json

* Offline: online sources are skipped (build(skip_online=True)) and Hugging Face access is disabled
  (HF_HUB_OFFLINE=1) so the pinned, already-downloaded embedding model is used. No network destination is
  contacted.
* Corpus: every chunk with include_in_default_retrieval=true from build/local/<pack>/chunks.jsonl, one record per
  chunk, text unchanged. forensic-rag does not re-chunk .jsonl records. Each record's `source` metadata carries
  the chunk ID; it is used only to map results back (forensic-rag's source boosts list no such names, so ranking
  is unaffected). `title` is the chunk's last heading, as a document title would be.
* Book text is written only under the --work directory (keep it under build/, which is not tracked).
* forensic-rag's defaults are used unchanged (no tuning on either split).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")  # chromadb

ROOT = Path(__file__).resolve().parents[2]
MODEL = "BAAI/bge-base-en-v1.5"


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(ROOT / "build" / "compare" / "forensic-rag"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "results" / "forensic-rag-raw.json"))
    ap.add_argument("--packs", nargs="*", default=["dynamic-ir", "nist-sp800-61r3"])
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    import chromadb
    import sentence_transformers
    import torch
    import yaml
    from importlib.metadata import version
    from rag_mcp.build import build
    from rag_mcp.index import RAGIndex

    work = Path(args.work)
    kdir, ddir = work / "knowledge", work / "data"
    pdir = kdir / "irpacks"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / ".bundled").touch()
    n = 0
    with open(pdir / "passages.jsonl", "w", encoding="utf-8") as out:
        for pack in args.packs:
            for line in open(ROOT / "build" / "local" / pack / "chunks.jsonl", encoding="utf-8"):
                r = json.loads(line)
                if not r["include_in_default_retrieval"]:
                    continue
                out.write(json.dumps({"text": r["text"], "metadata": {
                    "source": r["chunk_id"], "title": r["heading_path"][-1] if r["heading_path"] else ""}},
                    ensure_ascii=False) + "\n")
                n += 1

    t0 = time.perf_counter()
    res = build(skip_online=True, data_dir=ddir, knowledge_dir=kdir, model_name=MODEL)
    build_s = time.perf_counter() - t0
    if res.status != "success":
        print(json.dumps({"status": res.status, "errors": res.errors}), file=sys.stderr)
        return 1

    idx = RAGIndex(ddir, model_name=MODEL)
    t0 = time.perf_counter()
    idx.load()
    load_s = time.perf_counter() - t0

    qs = yaml.safe_load((ROOT / "eval" / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    results, latencies = {}, []
    for q in qs:
        t0 = time.perf_counter()
        got = idx.search(q["question"], top_k=args.k)
        latencies.append(time.perf_counter() - t0)
        results[q["id"]] = [r["source"] for r in got["results"]]

    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    lat = sorted(latencies)
    details = {
        "tool": "sift-mcp forensic-rag (rag-mcp)", "rag_mcp_version": version("rag-mcp"),
        "embedding_model": MODEL, "model_revision": _model_revision(),
        "chromadb": chromadb.__version__, "sentence_transformers": sentence_transformers.__version__,
        "torch": torch.__version__, "device": "cpu" if not torch.cuda.is_available() else "cuda",
        "python": platform.python_version(), "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(), "top_k": args.k, "records_indexed": res.total_records, "records_written": n,
        "build_seconds": round(build_s, 1), "index_load_seconds": round(load_s, 1),
        "query_latency_ms_median": round(1000 * lat[len(lat) // 2], 1),
        "query_latency_ms_max": round(1000 * lat[-1], 1),
        "peak_rss_mb": round(rss_mb), "index_disk_mb": round(dir_size(ddir) / 2**20, 1),
        "settings": "forensic-rag defaults: cosine HNSW, keyword boost 1.15, MITRE query augmentation, no tuning",
        "max_seq_length_tokens": idx.model.max_seq_length,
        "offline": {k: os.environ.get(k) for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"backend": "forensic-rag", "details": details, "results": results},
                                         indent=2) + "\n", encoding="utf-8")
    print(json.dumps(details, indent=2))
    return 0


def _model_revision() -> str | None:
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    ref = hub / ("models--" + MODEL.replace("/", "--")) / "refs" / "main"
    return ref.read_text().strip() if ref.exists() else None


if __name__ == "__main__":
    sys.exit(main())
