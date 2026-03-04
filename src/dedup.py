"""Embedding-based article deduplication using sentence-transformers."""

import gc
import logging
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"


def deduplicate_articles(
    articles: list[dict], threshold: float = 0.80
) -> list[dict]:
    """Deduplicate articles using embedding-based cosine similarity.

    Generates embeddings for each article, clusters near-duplicates using
    single-linkage clustering (union-find), and returns one representative
    per cluster with signal_strength and duplicate_titles attached.

    Args:
        articles: List of article dicts (must have 'title' and either
                  'full_text' or 'snippet').
        threshold: Cosine similarity threshold for merging (0.0-1.0).

    Returns:
        List of representative articles with `signal_strength` and
        `duplicate_titles` fields added.
    """
    if len(articles) <= 1:
        for a in articles:
            a["signal_strength"] = 1
            a["duplicate_titles"] = []
        return articles

    start_time = time.time()

    # Build text for each article
    texts = []
    for a in articles:
        full_text = a.get("full_text") or ""
        snippet = a.get("snippet", "")
        # Use title + first 500 chars of full_text if available, else snippet
        if full_text:
            texts.append(a["title"] + " " + full_text[:500])
        else:
            texts.append(a["title"] + " " + snippet)

    # Generate embeddings
    embeddings = _generate_embeddings(texts)
    if embeddings is None:
        # Model load failed — skip dedup, return all articles
        logger.warning("Dedup skipped: embedding generation failed")
        for a in articles:
            a["signal_strength"] = 1
            a["duplicate_titles"] = []
        return articles

    # Compute pairwise cosine similarity and cluster
    clusters = _cluster_articles(embeddings, threshold)

    # Pick representatives and build output
    representatives = []
    for cluster_indices in clusters:
        # Pick the article with the longest text as representative
        best_idx = max(cluster_indices, key=lambda i: len(texts[i]))
        rep = articles[best_idx]
        rep["signal_strength"] = len(cluster_indices)
        rep["duplicate_titles"] = [
            articles[i]["title"] for i in cluster_indices if i != best_idx
        ]
        representatives.append(rep)

    elapsed = round(time.time() - start_time, 1)
    largest_cluster = max(len(c) for c in clusters)
    removed = len(articles) - len(representatives)
    logger.info(
        "Dedup complete in %.1fs: %d in → %d unique clusters, "
        "%d removed, largest cluster=%d",
        elapsed,
        len(articles),
        len(representatives),
        removed,
        largest_cluster,
    )

    return representatives


def get_dedup_stats(
    original_count: int, deduped_articles: list[dict]
) -> dict[str, Any]:
    """Compute dedup stats for the run log."""
    cluster_sizes = [a.get("signal_strength", 1) for a in deduped_articles]
    return {
        "articles_in": original_count,
        "unique_clusters": len(deduped_articles),
        "articles_removed": original_count - len(deduped_articles),
        "largest_cluster": max(cluster_sizes) if cluster_sizes else 0,
    }


def _generate_embeddings(texts: list[str]) -> np.ndarray | None:
    """Generate embeddings using sentence-transformers, then free the model.

    Returns numpy array of shape (n, dim) or None if model load fails.
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        logger.error("sentence-transformers not installed — skipping dedup")
        return None

    try:
        logger.info("Loading embedding model %s", _MODEL_NAME)
        model = SentenceTransformer(_MODEL_NAME)
        embeddings = model.encode(texts, show_progress_bar=False)

        # Free model memory (critical for 1GB RAM constraint)
        del model
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

        return np.array(embeddings)
    except Exception as e:
        logger.error("Embedding generation failed: %s", e)
        return None


def _cluster_articles(
    embeddings: np.ndarray, threshold: float
) -> list[list[int]]:
    """Cluster articles using cosine similarity + union-find.

    Args:
        embeddings: Array of shape (n, dim).
        threshold: Similarity threshold for merging.

    Returns:
        List of clusters, each a list of article indices.
    """
    n = len(embeddings)

    # Normalize for cosine similarity
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1  # avoid division by zero
    normalized = embeddings / norms

    # Compute pairwise cosine similarity matrix
    similarity = np.dot(normalized, normalized.T)

    # Union-Find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # Merge articles above threshold
    for i in range(n):
        for j in range(i + 1, n):
            if similarity[i, j] >= threshold:
                union(i, j)

    # Collect clusters
    clusters_map: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters_map.setdefault(root, []).append(i)

    return list(clusters_map.values())
