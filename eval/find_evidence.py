"""Helper for authoring evaluation questions: exact-phrase lookup in canonical documents.

Usage: uv run python eval/find_evidence.py "<phrase>" [--pack dynamic-ir] [--id ELEMENT_ID]
Independent of the search index (plain substring match on element text), so evidence is not selected by the
retriever being evaluated.
"""
import argparse, json, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phrase", nargs="?")
    ap.add_argument("--pack", action="append")
    ap.add_argument("--id", action="append")
    ap.add_argument("--chars", type=int, default=300)
    a = ap.parse_args()
    for pack in a.pack or ["dynamic-ir", "nist-sp800-61r3"]:
        doc = json.loads((ROOT / "build/local" / pack / "document.json").read_text())
        for e in doc["elements"]:
            text = e["text"] if e["type"] != "table" else "\n".join(
                " | ".join(c["text"] for c in r["cells"]) for r in e["attrs"]["rows"])
            hit = (a.id and e["id"] in a.id) or (a.phrase and a.phrase.lower() in text.lower())
            if not hit:
                continue
            pages = sorted({(s["page"], s["page_label"]) for s in e["source_spans"]})
            i = text.lower().find(a.phrase.lower()) if a.phrase else 0
            print(f"[{pack}] {e['id']} {e['type']} {e['mapping_status']} pages={pages}")
            print("    " + text[max(0, i - 80): i + a.chars].replace("\n", " / "))


if __name__ == "__main__":
    main()
