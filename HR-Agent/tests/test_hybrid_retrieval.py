from datetime import date
from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError

from src.cache import NullCache, ResilientCache
from src.config import Settings
from src.contracts import PolicyChunk
from src.providers import DeterministicProvider
from src.repository import MemoryRepository
from src.retrieval import RetrievalService


def chunk(identifier, title="Kebijakan", content="Informasi pegawai"):
    return PolicyChunk(chunk_id=identifier, document_id="HR-LEAVE-001", title=title,
                       section="Hak", version="2.1", effective_date=date(2026, 1, 1),
                       content=content)


def service(repository=None, provider=None, cache=None, lifetime=0, version="test"):
    return RetrievalService(repository or MemoryRepository(), provider or DeterministicProvider(),
                            cache or NullCache(), lifetime,
                            settings=Settings(_env_file=None, policy_index_version=version))


def test_fusion_deduplication_and_metadata():
    shared = chunk("shared")
    repository = Mock()
    repository.policy_index_version = "test"
    repository.vector_search.return_value = [chunk("semantic"), shared, shared]
    repository.lexical_search.return_value = [chunk("lexical"), shared]
    results = service(repository).search("informasi", 3)
    assert results[0].chunk_id == "shared"
    assert results[0].score == pytest.approx(2 / 62 + 0.02 * 0.2)
    assert len({result.chunk_id for result in results}) == 3
    assert results[0].model_dump(exclude={"score"}) == shared.model_dump(exclude={"score"})
    repository.lexical_search.assert_called_once_with("informasi", 20)
    assert repository.vector_search.call_args.args[1] == 20


def test_keyword_boost_and_stable_ties():
    repository = Mock(policy_index_version="test")
    repository.vector_search.return_value = [chunk("first"), chunk("annual", "Cuti tahunan")]
    repository.lexical_search.return_value = []
    assert service(repository).search("cuti tahunan", 1)[0].chunk_id == "annual"
    repository.vector_search.return_value = [chunk("b"), chunk("a")]
    repository.lexical_search.return_value = [chunk("a"), chunk("b")]
    assert [result.chunk_id for result in service(repository).search("informasi", 2)] == ["a", "b"]


@pytest.mark.parametrize("query, count", [(" ", 3), ("x", 3), ("cuti", 0), ("cuti", 6), ("x" * 501, 3)])
def test_invalid_arguments(query, count):
    with pytest.raises(ValueError):
        service().search(query, count)


def test_empty_candidates_and_same_document_sections():
    assert service().search("cuti") == []
    repository = MemoryRepository()
    repository.chunks = [chunk("one", "Cuti"), chunk("two", "Cuti")]
    assert len(service(repository).search("cuti")) == 2


def test_cache_hit_and_version_isolation():
    cache = Mock()
    stored = {}
    cache.get.side_effect = stored.get
    cache.set.side_effect = lambda key, value, lifetime: stored.update({key: value})
    repository = MemoryRepository()
    repository.chunks = [chunk("annual", "Cuti tahunan")]
    provider = Mock(wraps=DeterministicProvider())
    provider.settings = None
    provider.embedding_dimensions = 1536
    retrieval = service(repository, provider, cache, 60)
    first = retrieval.search("Cuti  tahunan")
    assert retrieval.search("cuti tahunan") == first
    assert provider.embed.call_count == 1
    service(repository, provider, cache, 60, "next-version").search("cuti tahunan")
    assert provider.embed.call_count == 2
    assert len(stored) == 2


@pytest.mark.parametrize("payload", [{}, [None], [{"chunk_id": "broken"}]])
def test_invalid_cache_payload_is_a_miss(payload):
    cache = Mock()
    cache.get.return_value = payload
    provider = DeterministicProvider()
    provider.embed = Mock(wraps=provider.embed)
    assert service(provider=provider, cache=cache, lifetime=60).search("cuti") == []
    provider.embed.assert_called_once()


def test_disabled_cache_and_provider_failure():
    cache = Mock()
    service(cache=cache).search("cuti")
    cache.get.assert_not_called()
    cache.set.assert_not_called()
    provider = Mock(settings=None, embedding_dimensions=1536)
    provider.embed.side_effect = TimeoutError("provider unavailable")
    with pytest.raises(TimeoutError):
        service(provider=provider).search("cuti")


def test_redis_failure_is_safe_and_null_cache_is_disabled(caplog):
    cache = ResilientCache("redis://localhost:6379")
    cache.client = Mock()
    cache.client.get.side_effect = ConnectionError("secret-token")
    cache.client.set.side_effect = ConnectionError("secret-token")
    cache.client.ping.side_effect = ConnectionError("secret-token")
    assert cache.get("private-query") is None
    cache.set("private-query", [], 60)
    assert not cache.health()
    assert "secret-token" not in caplog.text
    assert "private-query" not in caplog.text
    repository = MemoryRepository()
    repository.chunks = [chunk("annual", "Cuti tahunan")]
    assert service(repository, cache=cache, lifetime=60).search("cuti tahunan")
    disabled = NullCache()
    assert disabled.get("anything") is None
    assert not disabled.health()
    disabled.set("anything", [], 60)
    disabled.close()


def test_cache_json_roundtrip_and_malformed_json():
    cache = ResilientCache("redis://localhost:6379")
    cache.client = Mock()
    cache.set("key", [{"text": "cuti"}], 60)
    arguments = cache.client.set.call_args
    cache.client.get.return_value = arguments.args[1]
    assert cache.get("key") == [{"text": "cuti"}]
    assert arguments.kwargs == {"ex": 60}
    cache.client.get.return_value = "not json"
    assert cache.get("key") is None


def test_database_failure_propagates():
    repository = Mock(policy_index_version="test")
    repository.vector_search.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        service(repository).search("cuti")


def test_deterministic_embeddings_and_memory_ranking():
    provider = DeterministicProvider()
    assert provider.embed(["Cuti tahunan"]) == provider.embed(["cuti tahunan"])
    repository = MemoryRepository()
    repository.chunks = [chunk("privacy", "Privasi"), chunk("annual", "Cuti tahunan")]
    assert repository.vector_search(provider.embed(["cuti tahunan"])[0], 1)[0].chunk_id == "annual"
    assert repository.lexical_search("jatah cuti tahunan", 1)[0].chunk_id == "annual"
