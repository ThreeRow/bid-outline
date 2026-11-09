"""OpenAI 兼容 API 运行时（原 llm.py）。"""

from __future__ import annotations

import json
import math
from typing import Any

from openai import OpenAI

from outline_gen.config import DEFAULT_API_TIMEOUT_SECONDS
from outline_gen.runtime.batch import RuntimeMetrics, RuntimeMetricsSnapshot, run_chat_many
from outline_gen.runtime.errors import (
    ModelOutputTruncatedError,
    ModelTransientError,
    sanitize_error_message,
)
from outline_gen.runtime.port import (
    DEFAULT_MAX_IN_FLIGHT,
    ChatBatchResult,
    ChatItem,
    ChatRequest,
)


def make_client(
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: float | None = DEFAULT_API_TIMEOUT_SECONDS,
) -> OpenAI:
    """Create an OpenAI-compatible client with a bounded request timeout.

    The previous unbounded/default client timeout could leave a preprocessing
    frontier waiting for many minutes on one oversized or provider-queued
    request.  ``None`` is accepted for callers that explicitly need the SDK
    default, while normal preprocessing uses a finite timeout.
    """
    kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url}
    if timeout_seconds is not None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be a finite positive number or None")
        kwargs["timeout"] = max(1.0, timeout)
    return OpenAI(**kwargs)


def _response_snapshot(response: Any, *, max_chars: int = 4000) -> str:
    """生成可记录的上游响应快照，不包含请求参数或 API Key。"""
    if response is None:
        return "null"
    try:
        if hasattr(response, "model_dump"):
            data = response.model_dump(mode="json")
        elif isinstance(response, dict):
            data = response
        else:
            data = {
                "type": type(response).__name__,
                "id": getattr(response, "id", None),
                "model": getattr(response, "model", None),
                "choices": getattr(response, "choices", None),
            }
        text = json.dumps(data, ensure_ascii=False, default=str)
    except Exception as exc:
        text = f"snapshot_failed={type(exc).__name__}: {exc}; response_type={type(response).__name__}"
    text = sanitize_error_message(text)
    return text if len(text) <= max_chars else text[:max_chars] + "...<truncated>"


def _invalid_response(reason: str, response: Any) -> ModelTransientError:
    return ModelTransientError(
        f"OpenAI-compatible API returned an invalid response: {reason}; "
        f"upstream_response={_response_snapshot(response)}",
        attempts=1,
    )


def chat(
    client: OpenAI,
    *,
    model: str,
    user: str,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    structured_output: Any | None = None,
    extra_body: dict[str, Any] | None = None,
) -> str:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    request_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body:
        request_kwargs["extra_body"] = dict(extra_body)
    if structured_output is not None:
        response_format = getattr(structured_output, "response_format", None)
        guided_json = getattr(structured_output, "guided_json", None)
        if response_format is not None:
            request_kwargs["response_format"] = response_format
        elif guided_json is not None:
            # vLLM extensions are transported through OpenAI SDK's extra_body;
            # passing guided_json as a top-level SDK keyword is not portable.
            # JSON Schema takes precedence when both are supplied: providers
            # generally reject two simultaneous structured-decoding controls.
            request_kwargs.setdefault("extra_body", {})["guided_json"] = guided_json
    response = client.chat.completions.create(
        **request_kwargs,
    )
    if response is None:
        raise _invalid_response("response is null", response)
    choices = getattr(response, "choices", None)
    if not choices:
        raise _invalid_response("choices is null or empty", response)
    choice = choices[0]
    if choice is None:
        raise _invalid_response("choices[0] is null", response)
    message = getattr(choice, "message", None)
    if message is None:
        raise _invalid_response("choices[0].message is null", response)
    content = getattr(message, "content", None)
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise ModelOutputTruncatedError(
            "OpenAI-compatible API stopped at the output token limit",
            partial_content=content if isinstance(content, str) else "",
            finish_reason=str(finish_reason),
            attempts=1,
        )
    if not isinstance(content, str) or not content.strip():
        raise _invalid_response(
            f"choices[0].message.content is empty (finish_reason={finish_reason!r})",
            response,
        )
    return content


class OpenAIChatRuntime:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        timeout_seconds: float | None = DEFAULT_API_TIMEOUT_SECONDS,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self._client = make_client(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        self._max_in_flight = max(1, int(max_in_flight))
        self._metrics = RuntimeMetrics()
        self.extra_body = dict(extra_body or {})

    @property
    def metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def get_metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def metrics_snapshot(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def _chat_attempt(self, req: ChatRequest) -> str:
        # A request made through this adapter is one generation attempt and one
        # provider HTTP request.  Retry accounting around ``invoke_chat`` naturally
        # produces one increment per retry.
        metrics = getattr(self, "_metrics", None)
        if metrics is not None:
            metrics.inc("generation_attempts")
            metrics.inc("http_requests")
        return chat(
            self._client,
            model=req.model,
            user=req.user,
            system=req.system,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            structured_output=req.structured_output,
            extra_body=self.extra_body,
        )

    def chat(self, req: ChatRequest) -> str:
        metrics = getattr(self, "_metrics", None)
        try:
            result = self._chat_attempt(req)
        except Exception:
            if metrics is not None:
                metrics.inc("generation_failures")
            raise
        if metrics is not None:
            metrics.inc("generation_successes")
        return result

    def chat_many(
        self,
        items: list[ChatItem] | tuple[ChatItem, ...],
        max_in_flight: int | None = None,
    ) -> ChatBatchResult:
        metrics = getattr(self, "_metrics", None)
        return run_chat_many(
            items,
            lambda item: self._chat_attempt(item.request),
            max_in_flight=getattr(self, "_max_in_flight", 1)
            if max_in_flight is None
            else max_in_flight,
            metrics=metrics,
        )
