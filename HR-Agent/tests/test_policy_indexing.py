from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI, BadRequestError, InternalServerError

from src.config import Settings
from src.providers import OpenAIProvider, validate_embeddings
from src.setup_lab import ROOT, chunk_policy, index_policies


POLICY = """---
document_id: TEST-001
title: Test policy
version: 2.1
effective_date: 2026-01-01
---
## First
First content.
## Second
Second content.
"""


def test_dataset_and_stable_chunks(tmp_path):
    chunks = [c for p in sorted((ROOT / 'data/policies').glob('*.md')) for c in chunk_policy(p)]
    assert len(chunks) == 15
    assert len({c['document_id'] for c in chunks}) == 7
    assert len({c['chunk_id'] for c in chunks}) == 15
    assert all(all(c[k] for k in ('title', 'section', 'version', 'effective_date')) for c in chunks)
    path = tmp_path / 'policy.md'
    path.write_bytes(POLICY.encode())
    first = chunk_policy(path)
    assert first[0]['version'] == '2.1'
    path.write_bytes(POLICY.replace('\n', '\r\n').encode())
    assert first == chunk_policy(path)
    path.write_text(POLICY.replace('First content.', 'Changed content.'), encoding='utf-8')
    changed = chunk_policy(path)
    assert first[0]['chunk_id'] != changed[0]['chunk_id']
    assert first[1]['chunk_id'] == changed[1]['chunk_id']


@pytest.mark.parametrize('text', [
    POLICY.replace('document_id: TEST-001', 'document_id:'),
    POLICY.replace('version: 2.1', 'version: [2, 1]'),
    POLICY.replace('2026-01-01', '2026-02-30'),
    POLICY.replace('## Second', '## First'),
    POLICY.replace('First content.', ''),
    POLICY.split('## First')[0],
    POLICY.replace('---\n', '', 1),
    POLICY.replace('title: Test policy', 'title: [broken'),
])
def test_invalid_policy(tmp_path, text):
    path = tmp_path / 'policy.md'
    path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        chunk_policy(path)


def test_duplicate_documents_and_empty_directory(tmp_path):
    with pytest.raises(ValueError, match='No policies'):
        index_policies(policy_dir=tmp_path, provider=Mock())
    for name in ('a.md', 'b.md'):
        (tmp_path / name).write_text(POLICY, encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate document'):
        index_policies(policy_dir=tmp_path, provider=Mock())


def settings(**kwargs):
    return Settings(_env_file=None, openai_api_key='test-key',
                    embedding_model='text-embedding-3-small', embedding_dimensions=64, **kwargs)


def test_batching_and_response_order():
    client = Mock()
    def create(**kwargs):
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[float(t)] * 64)
                                    for i, t in reversed(list(enumerate(kwargs['input'])))])
    client.embeddings.create.side_effect = create
    provider = OpenAIProvider(settings=settings(), client=client)
    assert provider.embed([]) == []
    vectors = provider.embed([str(i) for i in range(65)])
    assert [v[0] for v in vectors] == list(range(65))
    calls = client.embeddings.create.call_args_list
    assert [len(c.kwargs['input']) for c in calls] == [32, 32, 1]
    assert all(c.kwargs['model'] == 'text-embedding-3-small' and
               c.kwargs['dimensions'] == 64 for c in calls)


@pytest.mark.parametrize('indices', [[0, 0], [1], [], [0, 2]])
def test_invalid_response_indices(indices):
    client = Mock()
    client.embeddings.create.return_value = SimpleNamespace(data=[
        SimpleNamespace(index=i, embedding=[0.] * 64) for i in indices])
    with pytest.raises(ValueError, match='indices'):
        OpenAIProvider(settings=settings(), client=client).embed(['one', 'two'])


@pytest.mark.parametrize('vectors', [[], [[0.]], [[float('nan')] * 64],
    [[float('inf')] * 64], [[True] * 64], [['1'] * 64], [[1e39] * 64]])
def test_invalid_vectors(vectors):
    with pytest.raises(ValueError):
        validate_embeddings(vectors, 1, 64)


def test_client_configuration(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr('src.providers.OpenAI', constructor)
    OpenAIProvider(settings=settings(openai_base_url='https://example.test/v1',
                                     model_timeout_seconds=12))
    constructor.assert_called_once_with(api_key='test-key', base_url='https://example.test/v1',
                                        timeout=12, max_retries=2)


def test_provider_rejects_invalid_dimension():
    client = Mock()
    client.embeddings.create.return_value = SimpleNamespace(data=[
        SimpleNamespace(index=0, embedding=[0.])])
    with pytest.raises(ValueError, match='dimension'):
        OpenAIProvider(settings=settings(), client=client).embed(['text'])


def test_legacy_model_omits_dimensions_and_blank_input_is_rejected():
    config = settings()
    config.embedding_model = 'text-embedding-ada-002'
    client = Mock()
    client.embeddings.create.return_value = SimpleNamespace(data=[
        SimpleNamespace(index=0, embedding=[0.] * 64)])
    provider = OpenAIProvider(settings=config, client=client)
    provider.embed(['text'])
    assert 'dimensions' not in client.embeddings.create.call_args.kwargs
    with pytest.raises(ValueError, match='blank'):
        provider.embed([' '])


def test_later_batch_failure_does_not_return_partial_vectors():
    client = Mock()
    client.embeddings.create.side_effect = [SimpleNamespace(data=[
        SimpleNamespace(index=i, embedding=[0.] * 64) for i in range(32)]),
        RuntimeError('provider failure')]
    with pytest.raises(RuntimeError, match='provider failure'):
        OpenAIProvider(settings=settings(), client=client).embed(['text'] * 33)


@pytest.mark.parametrize('status,attempts,error', [(500, 3, InternalServerError),
                                                  (400, 1, BadRequestError)])
def test_sdk_retry_limit(monkeypatch, status, attempts, error):
    monkeypatch.setattr('openai._base_client.time.sleep', lambda _: None)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'message': 'test failure'}})
    with OpenAI(api_key='test-key', max_retries=2,
                http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        with pytest.raises(error):
            OpenAIProvider(settings=settings(), client=client).embed(['test'])
    assert len(requests) == attempts
