# -*- coding: utf-8 -*-
"""Local-first retrieval primitives shared by Wiki and Memory."""

from .embedding import (
    LocalOnnxEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    create_embedding_provider,
    get_default_local_embedding_provider,
    resolve_bundled_embedding_model_dir,
)
from .local_index import IndexedDocument, RetrievalHit, SQLiteRetrievalIndex
from .orchestrator import (
    LegacyRetrievalAdapter,
    NativeIndexAdapter,
    RetrievalCandidate,
    RetrievalOrchestrator,
)
from .runtime import HybridRetrievalRuntime
from .vector_index import EmbeddingProvider, SQLiteVectorIndex

__all__ = [
    "HybridRetrievalRuntime", "IndexedDocument", "LegacyRetrievalAdapter",
    "LocalOnnxEmbeddingProvider", "OpenAICompatibleEmbeddingProvider",
    "NativeIndexAdapter",
    "RetrievalCandidate", "RetrievalHit", "RetrievalOrchestrator",
    "EmbeddingProvider", "SQLiteRetrievalIndex", "SQLiteVectorIndex",
    "get_default_local_embedding_provider",
    "create_embedding_provider",
    "resolve_bundled_embedding_model_dir",
]
