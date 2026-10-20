"""Opt-in Step 4 checkpoint, independent of the unfinished API and agent."""
import os
from uuid import uuid4

import pytest

from src.cache import ResilientCache
from src.config import get_settings
from src.providers import OpenAIProvider
from src.repository import PostgresRepository
from src.retrieval import RetrievalService


@pytest.mark.live
def test_seeded_retrieval_with_redis_and_cache_outage():
    if os.getenv('RUN_LIVE_TESTS') != '1':
        pytest.skip('RUN_LIVE_TESTS=1 is required')
    settings = get_settings()
    provider = OpenAIProvider(settings=settings)
    available_cache = ResilientCache(settings.redis_url)
    unavailable_cache = ResilientCache('redis://127.0.0.1:1/0')
    try:
        assert available_cache.health()
        key = 'hr:retrieval:checkpoint:' + uuid4().hex
        available_cache.set(key, {'checkpoint': True}, 30)
        assert available_cache.get(key) == {'checkpoint': True}
        assert not unavailable_cache.health()
        with PostgresRepository(settings.database_url, settings.embedding_dimensions,
                                policy_index_version=settings.policy_index_version) as repository:
            for cache in (available_cache, unavailable_cache):
                retrieval = RetrievalService(repository, provider, cache, 60, settings=settings)
                results = retrieval.search('jatah cuti tahunan', 3)
                assert 'HR-LEAVE-001' in [chunk.document_id for chunk in results]
                assert all(chunk.title and chunk.section and chunk.version and chunk.effective_date
                           and chunk.content for chunk in results)
    finally:
        provider.client.close()
        available_cache.close()
        unavailable_cache.close()
