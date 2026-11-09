"""Shared batch execution and runtime metrics helpers.

The runtime adapters intentionally keep their transport-specific code small.  This
module contains the common, thread-safe accounting and the ordered, failure-isolated
executor used by API/LangChain adapters.  It has no dependency on a particular model
provider, so lightweight test doubles can use it as well.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import concurrent.futures.thread as _thread_pool
from dataclasses import dataclass
import threading
import weakref
from threading import Lock
import time
from typing import Any, Callable, Iterable

from outline_gen.runtime.port import ChatItem, ChatItemResult, ChatBatchResult


__all__ = [
    "RuntimeMetrics",
    "RuntimeMetricsSnapshot",
    "aggregate_metrics",
    "run_chat_many",
]


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """Thread pool whose workers cannot keep the interpreter alive.

    Provider calls already have bounded transport timeouts in the normal
    adapters, but a broken client/test double can still block a worker.  The
    standard ``ThreadPoolExecutor`` registers its non-daemon workers with the
    interpreter's atexit hook, so even ``shutdown(wait=False)`` can leave a
    process stuck while it waits for that worker.  Runtime batches are
    failure-isolated and may safely abandon such a worker at process shutdown;
    use daemon workers and deliberately do not add them to the global atexit
    join registry.

    This is a small compatibility copy of CPython's private worker bootstrap.
    Keeping it local avoids changing the public executor contract while making
    the shutdown behavior explicit.  The fallback to the regular executor is
    intentionally omitted: silently restoring the old process-hang behavior is
    worse than failing loudly if an incompatible Python runtime changes these
    private details.
    """

    def _adjust_thread_count(self) -> None:  # pragma: no cover - branch covered via callers
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads >= self._max_workers:
            return
        thread_name = "%s_%d" % (
            self._thread_name_prefix or self,
            num_threads,
        )
        worker = _thread_pool._worker
        thread = threading.Thread(
            name=thread_name,
            target=worker,
            args=(
                weakref.ref(self, weakref_cb),
                self._work_queue,
                self._initializer,
                self._initargs,
            ),
        )
        thread.daemon = True
        thread.start()
        self._threads.add(thread)


@dataclass(frozen=True)
class RuntimeMetricsSnapshot:
    """Immutable point-in-time runtime counters.

    The first six counters are the stable contract.  The success/failure counters
    are useful to callers but do not replace the logical item count: one logical item
    may account for several generation attempts.
    """

    logical_items: int = 0
    generation_attempts: int = 0
    http_requests: int = 0
    train_submissions: int = 0
    train_polls: int = 0
    transport_retries: int = 0
    generation_successes: int = 0
    generation_failures: int = 0

    @property
    def logical_item_count(self) -> int:
        return self.logical_items

    @property
    def generation_attempt_count(self) -> int:
        return self.generation_attempts

    @property
    def http_request_count(self) -> int:
        return self.http_requests

    @property
    def train_submission_count(self) -> int:
        return self.train_submissions

    @property
    def train_poll_count(self) -> int:
        return self.train_polls

    @property
    def transport_retry_count(self) -> int:
        return self.transport_retries

    def as_dict(self) -> dict[str, int]:
        return {
            "logical_items": self.logical_items,
            "generation_attempts": self.generation_attempts,
            "http_requests": self.http_requests,
            "train_submissions": self.train_submissions,
            "train_polls": self.train_polls,
            "transport_retries": self.transport_retries,
            "generation_successes": self.generation_successes,
            "generation_failures": self.generation_failures,
        }

    to_dict = as_dict

    # Mapping-like conveniences keep the object pleasant for service/telemetry
    # code without exposing the mutable counter store.
    def __getitem__(self, key: str) -> int:
        return self.as_dict()[key]

    def get(self, key: str, default: int = 0) -> int:
        return self.as_dict().get(key, default)

    def keys(self):
        return self.as_dict().keys()

    def items(self):
        return self.as_dict().items()

    def values(self):
        return self.as_dict().values()


class RuntimeMetrics:
    """Thread-safe monotonic counters shared by one runtime instance."""

    _FIELDS = (
        "logical_items",
        "generation_attempts",
        "http_requests",
        "train_submissions",
        "train_polls",
        "transport_retries",
        "generation_successes",
        "generation_failures",
    )

    def __init__(self) -> None:
        self._lock = Lock()
        self._values = {name: 0 for name in self._FIELDS}

    def increment(self, name: str, amount: int = 1) -> int:
        if name not in self._values:
            raise KeyError(f"unknown runtime metric: {name}")
        with self._lock:
            self._values[name] += int(amount)
            return self._values[name]

    # ``inc`` is a concise alias useful in adapters.
    inc = increment

    def update(self, **values: int) -> None:
        with self._lock:
            for name, amount in values.items():
                if name not in self._values:
                    raise KeyError(f"unknown runtime metric: {name}")
                self._values[name] += int(amount)

    def snapshot(self) -> RuntimeMetricsSnapshot:
        with self._lock:
            return RuntimeMetricsSnapshot(**self._values)

    def as_dict(self) -> dict[str, int]:
        return self.snapshot().as_dict()

    def reset(self) -> RuntimeMetricsSnapshot:
        with self._lock:
            before = RuntimeMetricsSnapshot(**self._values)
            for name in self._values:
                self._values[name] = 0
            return before


def aggregate_metrics(*values: Any) -> RuntimeMetricsSnapshot:
    """Aggregate snapshots/results/metric stores for an upper layer.

    Accepting a small range of input forms avoids coupling service code to the
    concrete runtime implementation.  Unknown values are ignored intentionally;
    callers can aggregate a mixed list of optional result objects safely.
    """

    totals = {name: 0 for name in RuntimeMetrics._FIELDS}

    def consume(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, RuntimeMetrics):
            consume(value.snapshot())
            return
        if isinstance(value, RuntimeMetricsSnapshot):
            data = value.as_dict()
        elif isinstance(value, ChatBatchResult):
            consume(value.metrics)
            return
        elif isinstance(value, dict):
            data = value
        else:
            return
        for name in totals:
            try:
                totals[name] += int(data.get(name, 0))
            except (TypeError, ValueError, AttributeError):
                continue

    for value in values:
        if isinstance(value, (list, tuple, set, frozenset)):
            for member in value:
                consume(member)
        else:
            consume(value)
    return RuntimeMetricsSnapshot(**totals)


def _coerce_items(items: Iterable[ChatItem]) -> list[ChatItem]:
    result = list(items)
    seen: set[str] = set()
    for item in result:
        if not isinstance(item, ChatItem):
            raise TypeError("chat_many expects ChatItem values")
        logical_id = str(item.logical_item_id)
        if item.logical_item_id is None or not logical_id.strip():
            raise ValueError("logical_item_id must not be empty")
        if logical_id in seen:
            raise ValueError(f"duplicate logical_item_id: {logical_id}")
        seen.add(logical_id)
    return result


def run_chat_many(
    items: Iterable[ChatItem],
    fn: Callable[[ChatItem], str],
    *,
    max_in_flight: int = 1,
    metrics: RuntimeMetrics | None = None,
) -> ChatBatchResult:
    """Execute ``fn`` with bounded concurrency and input-order results.

    A worker exception is captured into that item's result; other workers continue
    and the call itself does not fail because of one item.  ``as_completed`` is used
    deliberately so a slow item cannot block starting later work, while the final
    list is reconstructed in input order.
    """

    normalized = _coerce_items(items)
    batch_started = time.monotonic()
    workers = max(1, int(max_in_flight))
    if metrics is not None:
        metrics.inc("logical_items", len(normalized))
    if not normalized:
        return ChatBatchResult(
            results=(),
            metrics=metrics.snapshot() if metrics else None,
            wall_seconds=0.0,
            max_in_flight=workers,
        )

    workers = min(workers, len(normalized))
    results: list[ChatItemResult | None] = [None] * len(normalized)

    def execute(index: int, item: ChatItem) -> ChatItemResult:
        started = time.monotonic()
        try:
            output = fn(item)
        except Exception as exc:  # per-item isolation is the batch contract
            if metrics is not None:
                metrics.inc("generation_failures")
            return ChatItemResult(
                logical_item_id=item.logical_item_id,
                response=None,
                error=exc,
                request=item.request,
                elapsed_seconds=max(0.0, time.monotonic() - started),
            )
        if metrics is not None:
            metrics.inc("generation_successes")
        return ChatItemResult(
            logical_item_id=item.logical_item_id,
            response=output,
            error=None,
            request=item.request,
            elapsed_seconds=max(0.0, time.monotonic() - started),
        )

    # Daemon workers ensure a provider/client bug cannot keep process teardown
    # blocked after the batch caller has already received its per-item results.
    with _DaemonThreadPoolExecutor(max_workers=workers, thread_name_prefix="chat") as pool:
        futures = {
            pool.submit(execute, index, item): index
            for index, item in enumerate(normalized)
        }
        for future in as_completed(futures):
            index = futures[future]
            # ``execute`` captures ordinary exceptions.  This guard handles an
            # unexpected worker failure without losing ordering/isolation.
            try:
                results[index] = future.result()
            except Exception as exc:  # pragma: no cover - defensive fallback
                item = normalized[index]
                if metrics is not None:
                    metrics.inc("generation_failures")
                results[index] = ChatItemResult(
                    logical_item_id=item.logical_item_id,
                    response=None,
                    error=exc,
                    request=item.request,
                    elapsed_seconds=0.0,
                )

    complete = tuple(result for result in results if result is not None)
    return ChatBatchResult(
        results=complete,
        metrics=metrics.snapshot() if metrics else None,
        wall_seconds=max(0.0, time.monotonic() - batch_started),
        max_in_flight=workers,
    )
