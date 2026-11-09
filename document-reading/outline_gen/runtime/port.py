"""运行时端口定义。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from outline_gen.config import DEFAULT_API_TIMEOUT_SECONDS
from outline_gen.runtime.call_policy import CallPolicy


DEFAULT_MAX_IN_FLIGHT = 8


@dataclass(frozen=True)
class StructuredOutputConfig:
    """Optional provider-side structured-output request configuration.

    ``response_format`` is the OpenAI-compatible JSON-schema/json-object form.
    ``guided_json`` is the vLLM-compatible grammar hint.  Providers may receive
    either or both; the adapter decides how to map the values to its API.
    """

    response_format: dict[str, Any] | None = None
    guided_json: dict[str, Any] | None = None

    def __hash__(self) -> int:
        # Train runtime keeps ChatRequest as a dictionary key.  Preserve that
        # contract even though provider payloads naturally contain dictionaries.
        return hash((
            json.dumps(self.response_format, ensure_ascii=False, sort_keys=True)
            if self.response_format is not None else None,
            json.dumps(self.guided_json, ensure_ascii=False, sort_keys=True)
            if self.guided_json is not None else None,
        ))


@dataclass(frozen=True)
class ChatRequest:
    system: str | None
    user: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 8192
    structured_output: StructuredOutputConfig | None = None


@dataclass(frozen=True)
class ChatItem:
    """One logical generation in a batch.

    ``logical_item_id`` belongs to the caller and is never replaced by a provider
    query id.  It therefore remains stable across retries and makes result joining
    deterministic even when a provider completes requests out of order.
    """

    logical_item_id: str
    request: ChatRequest


@dataclass
class ChatItemResult:
    """Outcome for one :class:`ChatItem`.

    Exactly one of ``response`` and ``error`` is normally populated.  The aliases
    below intentionally keep the result ergonomic for callers using ``value`` or
    ``result`` terminology.
    """

    logical_item_id: str
    response: str | None = None
    error: BaseException | None = None
    request: ChatRequest | None = None
    attempts: int = 1
    elapsed_seconds: float = 0.0

    @property
    def result(self) -> str | None:
        return self.response

    @property
    def value(self) -> str | None:
        return self.response

    @property
    def output(self) -> str | None:
        return self.response

    @property
    def text(self) -> str | None:
        return self.response

    @property
    def answer(self) -> str | None:
        return self.response

    @property
    def typed_error(self) -> BaseException | None:
        return self.error

    @property
    def exception(self) -> BaseException | None:
        return self.error

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass
class ChatBatchResult:
    """Ordered collection of per-item outcomes and an optional metric snapshot."""

    results: list[ChatItemResult] | tuple[ChatItemResult, ...] = field(default_factory=list)
    metrics: Any | None = None
    wall_seconds: float = 0.0
    max_in_flight: int | None = None

    def __post_init__(self) -> None:
        # A list is convenient to callers and ensures a stable, independent result
        # container even when an adapter internally uses a tuple.
        self.results = list(self.results)

    @property
    def items(self) -> list[ChatItemResult]:
        return self.results

    @property
    def successes(self) -> list[ChatItemResult]:
        return [item for item in self.results if item.error is None]

    @property
    def failures(self) -> list[ChatItemResult]:
        return [item for item in self.results if item.error is not None]

    @property
    def errors(self) -> list[BaseException]:
        return [item.error for item in self.results if item.error is not None]

    @property
    def successful(self) -> list[ChatItemResult]:
        return self.successes

    @property
    def failed(self) -> list[ChatItemResult]:
        return self.failures

    @property
    def ordered_results(self) -> list[ChatItemResult]:
        return self.results

    @property
    def results_in_input_order(self) -> list[ChatItemResult]:
        return self.results

    @property
    def stats(self) -> Any | None:
        return self.metrics

    @property
    def provider_stats(self) -> Any | None:
        if self.metrics is None:
            return None
        as_dict = getattr(self.metrics, "as_dict", None)
        return as_dict() if callable(as_dict) else self.metrics

    @property
    def elapsed_seconds(self) -> float:
        return self.wall_seconds

    @property
    def by_id(self) -> dict[str, ChatItemResult]:
        return {item.logical_item_id: item for item in self.results}

    def __iter__(self):
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __getitem__(self, index: int) -> ChatItemResult:
        return self.results[index]


class ChatRuntime(Protocol):
    def chat(self, req: ChatRequest) -> str: ...

    def chat_many(
        self,
        items: list[ChatItem] | tuple[ChatItem, ...],
        max_in_flight: int | None = None,
    ) -> ChatBatchResult: ...


@dataclass
class RuntimeConfig:
    kind: str  # api | train | stub | langchain
    api_key: str = "EMPTY"
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "Qwen2.5-72B-Instruct"
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    # Optional low-level transport ceiling; normal callers use max_in_flight.
    train_max_query_list: int | None = None
    train_timeout_seconds: float = 600.0
    train_poll_interval_seconds: float = 1.0
    train_max_generation_attempts: int = 3
    train_transport_retry_attempts: int = 3
    train_transport_retry_base_seconds: float = 0.5
    # Provider-side HTTP read/connect timeout.  Keep this field last so adding
    # the API safeguard does not shift existing positional train settings.
    # ``None`` is retained as an explicit opt-out for low-level callers.
    api_timeout_seconds: float | None = DEFAULT_API_TIMEOUT_SECONDS
    call_policy: CallPolicy | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
