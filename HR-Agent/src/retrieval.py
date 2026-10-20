from __future__ import annotations

import hashlib
import json
import math

from pydantic import ValidationError

from .cache import ResilientCache
from .config import Settings, get_settings
from .contracts import PolicyChunk, SearchPolicyArgs
from .providers import ModelProvider
from .repository import Repository
from .search_text import query_terms


class RetrievalService:
    def __init__(self, repository: Repository, provider: ModelProvider, cache: ResilientCache,
                 cache_ttl_seconds: int, *, settings: Settings | None = None) -> None:
        self.repository = repository
        self.provider = provider
        self.cache = cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self.settings = settings or getattr(provider, "settings", None) or get_settings()
        repository_version = getattr(repository, "policy_index_version", None)
        if repository_version is not None and repository_version != self.settings.policy_index_version:
            raise ValueError("Repository and retrieval index versions must match")

    def search(self, query: str, top_k: int = 3) -> list[PolicyChunk]:
        # TODO(M2): Version-aware cache -> embed -> lexical/vector candidates -> RRF -> keyword rerank.
        
        arguments = SearchPolicyArgs(query=query.strip(), top_k=top_k)
        normalized_query = " ".join(query_terms(arguments.query))
        if not normalized_query:
            return []

        cache_key = self._build_cache_key(normalized_query, top_k)
        cached_results = self._read_cached_results(cache_key, top_k)
        # An empty list is a valid cache hit; only None means retrieval is needed.
        if cached_results is not None:
            return cached_results

        embedding = self._embed_query(normalized_query)
        semantic_candidates, lexical_candidates = self._fetch_candidates(
            normalized_query, embedding, top_k
        )
        chunks_by_identifier, fused_scores = self._fuse_candidates(
            semantic_candidates, lexical_candidates
        )
        results = self._rerank_candidates(
            normalized_query, chunks_by_identifier, fused_scores, top_k
        )
        self._cache_results(cache_key, results)
        return results

    def _build_cache_key(self, normalized_query: str, top_k: int) -> str:
        # Include every setting that changes retrieval results. Keep the ordered
        # identity and JSON encoding stable so existing cache entries remain usable.
        identity = [
            self.settings.policy_index_version,
            self.settings.embedding_model,
            type(self.provider).__module__,
            type(self.provider).__qualname__,
            getattr(self.provider, "embedding_dimensions", self.settings.embedding_dimensions),
            "fusion-keyword-v1",
            hashlib.sha256(normalized_query.encode()).hexdigest(),
            top_k,
        ]
        encoded_identity = json.dumps(identity).encode()
        identity_hash = hashlib.sha256(encoded_identity).hexdigest()
        return "hr:retrieval:" + identity_hash

    def _read_cached_results(self, cache_key: str, top_k: int) -> list[PolicyChunk] | None:
        if self.cache_ttl_seconds <= 0:
            return None

        cached = self.cache.get(cache_key)
        if not isinstance(cached, list) or len(cached) > top_k:
            return None

        # Cache data must meet the same chunk contract as database results.
        # Invalid scores or duplicate chunks make the whole entry a cache miss.
        try:
            chunks = [PolicyChunk.model_validate(chunk) for chunk in cached]
            if not all(math.isfinite(chunk.score) for chunk in chunks):
                return None
            unique_identifiers = {chunk.chunk_id for chunk in chunks}
            if len(unique_identifiers) != len(chunks):
                return None
            return chunks
        except (ValidationError, TypeError, ValueError):
            return None

    def _embed_query(self, normalized_query: str) -> list[float]:
        embeddings = self.provider.embed([normalized_query])
        if len(embeddings) != 1:
            raise ValueError("Expected one query embedding")
        return embeddings[0]

    def _fetch_candidates(
        self, normalized_query: str, embedding: list[float], top_k: int
    ) -> tuple[list[PolicyChunk], list[PolicyChunk]]:
        # Retrieve more candidates than we return so fusion and keyword ranking
        # can promote relevant chunks from either search method.
        candidate_limit = max(20, top_k * 4)
        semantic_candidates = self.repository.vector_search(embedding, candidate_limit)
        lexical_candidates = self.repository.lexical_search(normalized_query, candidate_limit)
        return semantic_candidates, lexical_candidates

    def _fuse_candidates(
        self, semantic_candidates: list[PolicyChunk], lexical_candidates: list[PolicyChunk]
    ) -> tuple[dict[str, PolicyChunk], dict[str, float]]:
        # Reciprocal-rank fusion compares positions rather than incompatible raw
        # search scores. A chunk contributes once per list, using its original rank.
        # Process lexical results last to preserve which metadata wins on duplicates.
        chunks_by_identifier: dict[str, PolicyChunk] = {}
        fused_scores: dict[str, float] = {}
        for candidates in (semantic_candidates, lexical_candidates):
            seen_identifiers: set[str] = set()
            for rank, chunk in enumerate(candidates, start=1):
                if chunk.chunk_id in seen_identifiers:
                    continue
                seen_identifiers.add(chunk.chunk_id)
                chunks_by_identifier[chunk.chunk_id] = chunk
                previous_score = fused_scores.get(chunk.chunk_id, 0.0)
                rank_contribution = 1 / (60 + rank)
                fused_scores[chunk.chunk_id] = previous_score + rank_contribution
        return chunks_by_identifier, fused_scores

    def _rerank_candidates(
        self, normalized_query: str, chunks_by_identifier: dict[str, PolicyChunk],
        fused_scores: dict[str, float], top_k: int
    ) -> list[PolicyChunk]:
        terms = set(query_terms(normalized_query))
        results: list[PolicyChunk] = []
        # Keyword coverage adds at most 0.02, with more weight on titles and
        # sections. Preserve the summation order and arithmetic for identical scores.
        for identifier, chunk in chunks_by_identifier.items():
            weighted_fields = ((chunk.title, 0.5), (chunk.section, 0.3), (chunk.content, 0.2))
            coverage = sum(
                self._weighted_keyword_coverage(text, weight, terms)
                for text, weight in weighted_fields
            )
            score = fused_scores[identifier] + 0.02 * coverage
            results.append(chunk.model_copy(update={"score": score}))
        # Chunk identifiers provide stable ordering when final scores tie.
        return sorted(results, key=lambda chunk: (-chunk.score, chunk.chunk_id))[:top_k]

    def _weighted_keyword_coverage(self, text: str, weight: float, terms: set[str]) -> float:
        field_terms = set(query_terms(text))
        matching_terms = terms.intersection(field_terms)
        return weight * len(matching_terms) / len(terms)

    def _cache_results(self, cache_key: str, results: list[PolicyChunk]) -> None:
        if self.cache_ttl_seconds > 0:
            serialized_results = [chunk.model_dump(mode="json") for chunk in results]
            self.cache.set(cache_key, serialized_results, self.cache_ttl_seconds)

