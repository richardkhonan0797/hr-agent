"""Build and own API dependencies; injected dependencies remain caller-owned."""
from contextlib import ExitStack
from dataclasses import dataclass, field

from .agent import AgentService
from .cache import ResilientCache
from .config import Settings
from .providers import DeterministicProvider, OpenAIProvider
from .repository import PostgresRepository, Repository
from .retrieval import RetrievalService
from .tools import ToolExecutor


def model_is_ready(settings: Settings) -> bool:
    if settings.model_mode == "fake":
        return True
    if settings.model_mode != "live":
        return False
    return all(value and value.strip() for value in (
        settings.openai_api_key, settings.agent_model, settings.embedding_model,
    ))


@dataclass
class Runtime:
    repository: Repository
    cache: ResilientCache
    agent: AgentService | None
    resources: ExitStack = field(default_factory=ExitStack)

    def close(self) -> None:
        self.resources.close()


def assemble_runtime(settings: Settings, repository: Repository | None = None,
                     cache: ResilientCache | None = None,
                     agent: AgentService | None = None) -> Runtime:
    resources = ExitStack()
    try:
        if repository is None:
            repository = PostgresRepository(
                settings.database_url, settings.embedding_dimensions,
                policy_index_version=settings.policy_index_version,
            )
            resources.callback(repository.close)
        if cache is None:
            cache = ResilientCache(settings.redis_url)
            resources.callback(cache.close)
        if agent is None and model_is_ready(settings):
            if settings.model_mode == "fake":
                provider = DeterministicProvider(settings.embedding_dimensions)
            else:
                provider = OpenAIProvider(settings=settings)
                resources.callback(provider.client.close)
            retrieval = RetrievalService(repository, provider, cache,
                                         settings.retrieval_cache_ttl_seconds, settings=settings)
            tools = ToolExecutor(repository, retrieval, settings)
            agent = AgentService(repository, provider, tools, settings)
        # Missing model configuration must not prevent liveness/readiness endpoints
        # from starting. Chat returns unavailable until configuration is corrected.
        return Runtime(repository, cache, agent, resources)
    except Exception:
        resources.close()
        raise
