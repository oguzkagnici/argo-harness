"""BEIR SciFact retrieval benchmark: BM25 vs dense vs RRF vs RRF+rerank.

Protocol (Thakur et al., BEIR 2021): binary relevance from qrels; report
Recall@10, Recall@100 and nDCG@10. To bound embedding cost we evaluate a fixed
random sample of queries over their labeled positives plus random distractor
documents from the full SciFact corpus.

Usage:
  python benchmarks/retrieval.py --queries 100 --docs 1000
  python benchmarks/retrieval.py --queries 100 --docs 1000 --ablate

``--ablate`` runs one-factor-at-a-time ablations around the production recipe (BM25+dense, RRF k=60,
rerank top-20): RRF k, hybrid weighting, rerank depth, and a query instruction for the embedder.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from argo.config import load_config  # noqa: E402
from argo.llm.client import LLMClient  # noqa: E402
from argo.memory.retrieval import BM25, dense_search, rrf  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
RESULTS = Path(__file__).resolve().parent / "results"


def load_data():
    corpus = {row["_id"]: row for row in pq.read_table(DATA / "scifact_corpus.parquet").to_pylist()}
    queries = {row["_id"]: row for row in pq.read_table(DATA / "scifact_queries.parquet").to_pylist()}
    qrels: dict[str, set[str]] = {}
    lines = (DATA / "scifact_qrels_test.tsv").read_text(encoding="utf-8").splitlines()
    for line in lines[1:]:
        qid, docid, score = line.split("\t")
        if int(score) > 0:
            qrels.setdefault(qid, set()).add(docid)
    return corpus, queries, qrels


def build_sample(corpus, queries, qrels, n_queries: int, n_docs: int, seed: int):
    rng = random.Random(seed)
    usable = sorted(q for q, rel in qrels.items() if q in queries and rel & corpus.keys())
    chosen = rng.sample(usable, min(n_queries, len(usable)))
    positives = set().union(*(qrels[q] & corpus.keys() for q in chosen))
    pool = [d for d in corpus if d not in positives]
    distractors = rng.sample(pool, min(max(0, n_docs - len(positives)), len(pool)))
    doc_ids = sorted(positives | set(distractors))
    docs = [{"id": d, "text": (corpus[d]["title"] + "\n" + corpus[d]["text"]).strip()} for d in doc_ids]
    qs = [{"id": q, "text": (queries[q]["title"] + "\n" + queries[q]["text"]).strip()} for q in chosen]
    return docs, qs, qrels


def dcg(rels: list[int]) -> float:
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def score_order(order: list[str], rel: set[str]) -> tuple[float, float, float]:
    top10 = order[:10]
    rec10 = len(set(top10) & rel) / len(rel) if rel else 0.0
    rec100 = len(set(order[:100]) & rel) / len(rel) if rel else 0.0
    gains = [1 if d in rel else 0 for d in top10]
    ideal = [1] * min(len(rel), 10)
    ndcg = dcg(gains) / dcg(ideal) if ideal else 0.0
    return rec10, rec100, ndcg


async def embed_with_cache(llm: LLMClient, texts: list[str], model: str, cache_dir: Path) -> np.ndarray:
    key = hashlib.sha1((model + "\n" + "\n".join(texts)).encode("utf-8")).hexdigest()[:16]
    path = cache_dir / f"emb_{key}.npy"
    if path.exists():
        return np.load(path)
    vecs = await llm.embed(texts, model=model)
    arr = np.asarray(vecs, dtype=np.float32)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.save(path, arr)
    return arr


def weighted_rrf(rankings: list[list[tuple[int, float]]], weights: list[float], k: int) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, (idx, _score) in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + weight / (k + rank + 1)
    return sorted(scores.items(), key=lambda item: -item[1])


def cosine_rank(qvec: np.ndarray, doc_mat: np.ndarray, k: int) -> list[tuple[int, float]]:
    denom = (np.linalg.norm(doc_mat, axis=1) * (np.linalg.norm(qvec) or 1.0)) + 1e-9
    scores = (doc_mat @ qvec) / denom
    idx = np.argsort(-scores)[:k]
    return [(int(i), float(scores[i])) for i in idx]


async def run_ablations(args, llm, docs, qs, model, doc_mat, q_mat) -> dict[str, dict]:
    bm25 = BM25(docs, text_key="text")
    bm_cache: dict[str, list[tuple[int, float]]] = {}
    dn_cache: dict[str, list[tuple[int, float]]] = {}
    for qi, q in enumerate(qs):
        bm_cache[q["id"]] = bm25.search(q["text"], k=100)
        dn_cache[q["id"]] = cosine_rank(q_mat[qi], doc_mat, k=100)

    def ids(ranking):
        return [docs[i]["id"] for i, _ in ranking]

    configs: dict[str, dict] = {}
    # RRF k sweep (no rerank) — tests how sharply rank position is discounted
    for kk in (10, 60, 100):
        configs[f"rrf_k{kk}"] = {"k": kk, "weights": (1.0, 1.0), "rerank": 0}
    # weighted fusion: BM25-heavy and dense-heavy
    configs["rrf_bm25_heavy"] = {"k": 60, "weights": (0.75, 0.25), "rerank": 0}
    configs["rrf_dense_heavy"] = {"k": 60, "weights": (0.25, 0.75), "rerank": 0}

    rankings: dict[str, dict[str, list[str]]] = {name: {} for name in configs}
    for qi, q in enumerate(qs):
        bm, dn = bm_cache[q["id"]], dn_cache[q["id"]]
        for name, cfg in configs.items():
            fused = weighted_rrf([bm[:50], dn[:50]], list(cfg["weights"]), cfg["k"])[:100]
            rankings[name][q["id"]] = ids(fused)

    # rerank depth sweep on the production fusion (k=60, equal weights)
    for depth in (10, 50):
        name = f"rerank_top{depth}"
        rankings[name] = {}
        for qi, q in enumerate(qs):
            bm, dn = bm_cache[q["id"]], dn_cache[q["id"]]
            fused = weighted_rrf([bm[:50], dn[:50]], [1.0, 1.0], 60)[:100]
            cand = [docs[i]["text"] for i, _ in fused[:depth]]
            reranked = await llm.rerank(q["text"], cand, top_n=depth)
            order = [docs[fused[i][0]]["id"] for i, _ in reranked]
            order += [d for d in ids(fused) if d not in set(order)]
            rankings[name][q["id"]] = order

    # query instruction for the embedder (re-embed queries only)
    prefix = "Represent this sentence for searching relevant passages: "
    t0 = time.monotonic()
    q_inst = await embed_with_cache(llm, [prefix + q["text"] for q in qs], model, DATA / "cache")
    print(f"instruction query embeddings: {time.monotonic() - t0:.1f}s", flush=True)
    rankings["query_instruction"] = {}
    for qi, q in enumerate(qs):
        dn = cosine_rank(q_inst[qi], doc_mat, k=100)
        fused = weighted_rrf([bm_cache[q["id"]][:50], dn[:50]], [1.0, 1.0], 60)[:100]
        rankings["query_instruction"][q["id"]] = ids(fused)
    return rankings


def summarize(rankings: dict[str, dict[str, list[str]]], qrels, n_queries: int) -> dict[str, dict]:
    metrics = {m: {"recall@10": 0.0, "recall@100": 0.0, "ndcg@10": 0.0} for m in rankings}
    for method, per_q in rankings.items():
        for qid, order in per_q.items():
            r10, r100, ndcg = score_order(order, qrels[qid])
            metrics[method]["recall@10"] += r10
            metrics[method]["recall@100"] += r100
            metrics[method]["ndcg@10"] += ndcg
        for metric in metrics[method]:
            metrics[method][metric] = round(metrics[method][metric] / n_queries, 4)
    return metrics


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config()
    corpus, queries, qrels = load_data()
    docs, qs, qrels = build_sample(corpus, queries, qrels, args.queries, args.docs, args.seed)
    print(f"SciFact sample: {len(docs)} docs, {len(qs)} queries, "
          f"{sum(len(qrels[q['id']]) for q in qs)} positive judgments", flush=True)

    model = cfg.models.embedding_model or "qwen3-embedding-8b"
    async with LLMClient(cfg) as llm:
        t0 = time.monotonic()
        doc_mat = await embed_with_cache(llm, [d["text"] for d in docs], model, DATA / "cache")
        q_mat = await embed_with_cache(llm, [q["text"] for q in qs], model, DATA / "cache")
        print(f"embeddings: {doc_mat.shape} in {time.monotonic() - t0:.1f}s", flush=True)

        bm25 = BM25(docs, text_key="text")
        rankings: dict[str, dict[str, list[str]]] = {m: {} for m in ("bm25", "dense", "rrf", "rrf+rerank")}
        t1 = time.monotonic()
        for qi, q in enumerate(qs):
            bm = bm25.search(q["text"], k=100)
            dn = cosine_rank(q_mat[qi], doc_mat, k=100)
            fused = rrf([bm[:50], dn[:50]], k=60)[:100]
            bm_ids = [docs[i]["id"] for i, _ in bm]
            dn_ids = [docs[i]["id"] for i, _ in dn]
            fused_ids = [docs[i]["id"] for i, _ in fused]
            rankings["bm25"][q["id"]] = bm_ids
            rankings["dense"][q["id"]] = dn_ids
            rankings["rrf"][q["id"]] = fused_ids
            cand = [docs[i]["text"] for i, _ in fused[:20]]
            reranked = await llm.rerank(q["text"], cand, top_n=20)
            order = [docs[fused[i][0]]["id"] for i, _ in reranked]
            order += [d for d in fused_ids if d not in set(order)]  # keep full-depth ranking
            rankings["rrf+rerank"][q["id"]] = order
        print(f"ranking+rerank: {time.monotonic() - t1:.1f}s", flush=True)
        if args.ablate:
            ablation = await run_ablations(args, llm, docs, qs, model, doc_mat, q_mat)
            ablation["rrf+rerank20"] = rankings["rrf+rerank"]
            metrics = summarize(ablation, qrels, len(qs))
            summary = {"benchmark": "BEIR-SciFact-ablation", "docs": len(docs), "queries": len(qs),
                       "embedding_model": model, "seed": args.seed, "metrics": metrics,
                       "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            RESULTS.mkdir(parents=True, exist_ok=True)
            out = RESULTS / f"retrieval_ablation_{time.strftime('%Y%m%d-%H%M%S')}.json"
            out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(f"\n{'config':<22} {'R@10':>7} {'R@100':>7} {'nDCG@10':>8}")
            for name, m in sorted(metrics.items(), key=lambda kv: -kv[1]["ndcg@10"]):
                print(f"{name:<22} {m['recall@10']:>7.4f} {m['recall@100']:>7.4f} {m['ndcg@10']:>8.4f}")
            print(f"\nSaved: {out.relative_to(ROOT)}")
            return 0


    metrics = {m: {"recall@10": 0.0, "recall@100": 0.0, "ndcg@10": 0.0} for m in rankings}
    for method, per_q in rankings.items():
        for qid, order in per_q.items():
            r10, r100, ndcg = score_order(order, qrels[qid])
            metrics[method]["recall@10"] += r10
            metrics[method]["recall@100"] += r100
            metrics[method]["ndcg@10"] += ndcg
    for method in metrics:
        for metric in metrics[method]:
            metrics[method][metric] = round(metrics[method][metric] / len(qs), 4)

    summary = {"benchmark": "BEIR-SciFact", "docs": len(docs), "queries": len(qs),
               "embedding_model": model, "seed": args.seed, "metrics": metrics,
               "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS / f"retrieval_scifact_{stamp}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\n{'method':<12} {'R@10':>7} {'R@100':>7} {'nDCG@10':>8}")
    for method, m in metrics.items():
        print(f"{method:<12} {m['recall@10']:>7.4f} {m['recall@100']:>7.4f} {m['ndcg@10']:>8.4f}")
    print(f"\nSaved: {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--docs", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--ablate", action="store_true", help="run one-factor-at-a-time retrieval ablations")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
