"""Download the BEIR retrieval fixtures used by ``retrieval.py`` into ``benchmarks/data/``.

Large parquet files are intentionally not committed; run this once per machine.
Usage:  python benchmarks/fetch_data.py --corpus scifact,nfcorpus
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq

DATA = Path(__file__).resolve().parent / "data"

SOURCES = {
    "scifact": ("BeIR/scifact", "BeIR/scifact-qrels"),
    "nfcorpus": ("BeIR/nfcorpus", "BeIR/nfcorpus-qrels"),
}


def fetch(name: str) -> None:
    from datasets import load_dataset

    corpus_id, qrels_id = SOURCES[name]
    DATA.mkdir(parents=True, exist_ok=True)
    corpus = load_dataset(corpus_id, "corpus", split="corpus")
    queries = load_dataset(corpus_id, "queries", split="queries")
    pq.write_table(corpus.data.table, DATA / f"{name}_corpus.parquet")
    pq.write_table(queries.data.table, DATA / f"{name}_queries.parquet")
    qrels = load_dataset(qrels_id, split="test")
    lines = ["query-id\tcorpus-id\tscore"]
    lines += [f"{r['query-id']}\t{r['corpus-id']}\t{r['score']}" for r in qrels]
    (DATA / f"{name}_qrels_test.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{name}: {len(corpus)} docs, {len(queries)} queries, {len(qrels)} judgments")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="scifact,nfcorpus")
    args = ap.parse_args()
    for name in [c.strip() for c in args.corpus.split(",") if c.strip()]:
        if name not in SOURCES:
            raise SystemExit(f"unknown corpus {name!r}; choose from {list(SOURCES)}")
        fetch(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
