from argo.memory.retrieval import BM25, chunk_text, rrf, tokenize, recency_score, cosine


def test_tokenize_drops_stopwords_and_short_tokens():
    assert tokenize("The quick brown fox and the dog") == ["quick", "brown", "fox", "dog"]


def test_chunking_is_heading_aware_and_overlapping():
    text = "# A\n" + ("alpha " * 300) + "\n# B\n" + ("beta " * 300)
    chunks = chunk_text(text, max_chars=500, overlap=50)
    assert len(chunks) >= 2
    assert any("alpha" in c.text for c in chunks)
    assert any("beta" in c.text for c in chunks)


def test_bm25_prefers_relevant_document():
    docs = [{"text": "cat sat on mat"}, {"text": "agent memory consolidation via dreaming"},
            {"text": "deploy pipeline migrations"}]
    ranked = BM25(docs).search("memory consolidation", k=3)
    assert ranked[0][0] == 1


def test_rrf_fuses_rankings():
    fused = rrf([[(1, 0.9), (2, 0.8), (3, 0.7)], [(3, 0.9), (1, 0.8), (2, 0.7)]], k=60)
    order = [i for i, _ in fused]
    assert order[0] == 1  # rank 1 in both vs 3/2 etc


def test_recency_and_cosine():
    import time
    now = time.time()
    assert recency_score(now, now) > recency_score(now - 60 * 86400, now)
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0


def test_rrf_weights_and_k_are_honoured():
    from argo.memory.retrieval import rrf
    a = [(0, 1.0), (1, 0.9), (2, 0.8)]
    b = [(2, 1.0), (1, 0.9), (0, 0.8)]
    lexical_only = rrf([a, b], k=60, weights=[1.0, 0.0])
    assert [i for i, _ in lexical_only] == [0, 1, 2]
    dense_only = rrf([a, b], k=60, weights=[0.0, 1.0])
    assert [i for i, _ in dense_only] == [2, 1, 0]
    # a smaller k makes the top rank dominate more strongly
    sharp = rrf([a, b], k=1, weights=[1.0, 1.0])
    flat = rrf([a, b], k=100, weights=[1.0, 1.0])
    assert sharp[0][1] > flat[0][1]
