"""Ingestion subsystem: embedding, sentiment analysis, chunking, hybrid search, and pipeline."""

from app.ingest.chunking import (
    ChunkDraft,
    ChunkSyncStats,
    build_product_chunks,
    chunk_description,
    chunk_meta,
    chunk_reviews,
    chunk_specs,
    compute_content_hash,
    sync_product_chunks,
)
from app.ingest.embedding import (
    Embedder,
    LocalEmbedder,
    NullEmbedder,
    ProviderEmbedder,
    get_embedder,
)
from app.ingest.pipeline import (
    IngestResult,
    IngestStats,
    IngestTimings,
    ingest_source,
    resolve_source_scope,
)
from app.ingest.search import SearchHit, hybrid_search
from app.ingest.sentiment import (
    AspectSentiment,
    ReviewSentimentItem,
    SentimentBatchResponse,
    SentimentSummary,
    analyze_product_reviews,
    rating_to_sentiment,
    recompute_product_sentiment_counts,
)

__all__ = [
    "AspectSentiment",
    "ChunkDraft",
    "ChunkSyncStats",
    "Embedder",
    "IngestResult",
    "IngestStats",
    "IngestTimings",
    "LocalEmbedder",
    "NullEmbedder",
    "ProviderEmbedder",
    "ReviewSentimentItem",
    "SearchHit",
    "SentimentBatchResponse",
    "SentimentSummary",
    "analyze_product_reviews",
    "build_product_chunks",
    "chunk_description",
    "chunk_meta",
    "chunk_reviews",
    "chunk_specs",
    "compute_content_hash",
    "get_embedder",
    "hybrid_search",
    "ingest_source",
    "rating_to_sentiment",
    "recompute_product_sentiment_counts",
    "resolve_source_scope",
    "sync_product_chunks",
]
