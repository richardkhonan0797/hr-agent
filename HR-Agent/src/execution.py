"""Per-run deadline shared by synchronous dependencies, without background writes."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
import time
from threading import Thread
from typing import TypeVar

from openai import APIConnectionError, APIStatusError


class BudgetExceeded(TimeoutError):
    pass


@dataclass
class RunBudget:
    seconds: float
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    finalizing: bool = False

    def __post_init__(self) -> None:
        self.started = self.clock()

    def remaining(self) -> float:
        # Keep a small part of the same deadline for writing the final trace.
        reserve = 0.0 if self.finalizing else min(0.5, self.seconds / 10)
        remaining = self.seconds - (self.clock() - self.started) - reserve
        if remaining <= 0:
            raise BudgetExceeded("Agent deadline reached")
        return remaining

    def elapsed_ms(self) -> int:
        return max(0, int((self.clock() - self.started) * 1000))


current_budget: ContextVar[RunBudget | None] = ContextVar("agent_budget", default=None)


@contextmanager
def budget_scope(budget: RunBudget) -> Iterator[None]:
    token = current_budget.set(budget)
    try:
        yield
    finally:
        current_budget.reset(token)


def remaining_timeout(default: float) -> float:
    budget = current_budget.get()
    if budget is None:
        return default
    return min(default, budget.remaining())


def check_deadline() -> None:
    budget = current_budget.get()
    if budget is not None:
        budget.remaining()


T = TypeVar("T")


def _provider_attempt(operation: Callable[[], T]) -> T:
    budget = current_budget.get()
    if budget is None:
        return operation()
    timeout = budget.remaining()
    result: Future[T] = Future()
    context = copy_context()

    def request() -> None:
        try:
            result.set_result(context.run(operation))
        except BaseException as error:
            result.set_exception(error)

    # HTTP socket timeouts measure inactivity, not total response time (a server
    # can keep sending small chunks). Bound the caller's wait as well. This worker
    # may only call a provider: tool execution and database writes stay synchronous
    # on the caller, so a late response cannot create a proposal or ticket.
    Thread(target=request, name="hr-provider-request", daemon=True).start()
    try:
        return result.result(timeout=timeout)
    except FutureTimeoutError:
        # A completed provider TimeoutError is retryable; an unfinished request
        # has used the entire run allowance and must never be retried.
        if result.done():
            return result.result()
        raise BudgetExceeded("Provider exceeded the run deadline") from None


def call_with_retry(operation: Callable[[], T]) -> T:
    """Retry provider transport/transient failures only, never a tool execution."""
    for attempt in range(3):
        check_deadline()
        try:
            return _provider_attempt(operation)
        except BudgetExceeded:
            raise
        except (APIConnectionError, APIStatusError, TimeoutError) as error:
            transient = not isinstance(error, APIStatusError)
            if isinstance(error, APIStatusError):
                transient = error.status_code in (408, 409, 429) or error.status_code >= 500
            if not transient or attempt == 2:
                raise
            delay = 0.1 * (2 ** attempt)
            if remaining_timeout(delay + 0.001) <= delay:
                raise BudgetExceeded("No time remaining for retry") from None
            time.sleep(delay)
    raise AssertionError("Unreachable retry state")
