"""No implicit transport retries; complete raw protocol evidence is retained."""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import os
from typing import Protocol
import httpx
from bid_outline.config import AppConfig
from .budget import TaskExecutionError


@dataclass
class ModelResponse:
    """Final text plus untouched server payload and optional reasoning metadata."""
    content: str
    finish_reason: str = 'stop'
    raw: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    request_id: str | None = None
    reasoning: str | None = None
    reused_from: dict | None = None


class ModelClient(Protocol):
    async def generate(self, request: dict) -> ModelResponse:
        ...

    async def aclose(self) -> None:
        ...


def retry_after_seconds(value: str) -> float:
    """Interpret delta-seconds or an HTTP date; ignore malformed/nonfinite values."""
    try:
        delay = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            delay = (deadline - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


class ModelClientError(TaskExecutionError):
    def __init__(self, message, *, retryable=False, raw=None, status_code=None, retry_after=0, context_exceeded=False):
        super().__init__(message)
        self.retryable, self.raw, self.status_code = retryable, raw, status_code
        self.retry_after, self.context_exceeded = retry_after, context_exceeded


class VllmChatClient:
    """Local OpenAI-compatible HTTP client with execution-layer retry ownership."""

    def __init__(self, config: AppConfig):
        if not config.model.strip() or config.model == 'fake' or config.model.startswith('REPLACE_'):
            raise ValueError('real calls require an actual served model name')
        self.config = config
        key = os.environ.get(config.api_key_env)
        self.http = httpx.AsyncClient(timeout=config.timeout_seconds,
            limits=httpx.Limits(max_connections=config.max_in_flight, max_keepalive_connections=config.max_in_flight),
            transport=httpx.AsyncHTTPTransport(retries=0),
            headers={'Authorization': 'Bearer ' + key} if key else {})

    async def generate(self, request: dict) -> ModelResponse:
        body = {k: v for k, v in request.items() if not k.startswith('_')}
        try:
            response = await self.http.post(self.config.base_url + '/chat/completions', json=body)
        except httpx.TransportError as exc:
            raise ModelClientError(type(exc).__name__, retryable=True) from exc
        try:
            raw = response.json()
        except ValueError:
            raw = response.text
        if response.is_error:
            retry_after = retry_after_seconds(response.headers.get('retry-after', '0'))
            context = response.status_code == 400 and any(s in str(raw).lower() for s in ('context length', 'context_length', 'maximum context', 'too many tokens'))
            raise ModelClientError(f'HTTP {response.status_code}', raw=raw, status_code=response.status_code,
                retryable=response.status_code == 429 or response.status_code in (500, 502, 503, 504),
                retry_after=retry_after, context_exceeded=context)
        try:
            choice = raw['choices'][0]
            message = choice['message']
            content = message['content']
            # Reasoning-enabled servers can exhaust the output budget before
            # emitting final content. Preserve this as truncation, not bad JSON.
            if content is None and choice.get('finish_reason') == 'length':
                content = ''
            if not isinstance(content, str):
                raise ValueError('non-text content')
            return ModelResponse(content, choice.get('finish_reason'), raw, raw.get('usage', {}),
                response.headers.get('x-request-id') or raw.get('id'), message.get('reasoning_content', message.get('reasoning')))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelClientError('invalid Chat Completions response', raw=raw) from exc

    async def aclose(self) -> None:
        await self.http.aclose()


class FakeModelClient:
    """Consume explicit fixtures in order, optionally routed by task type."""

    def __init__(self, responses):
        self.responses = {k: deque(v) for k, v in responses.items()} if isinstance(responses, dict) else deque(responses)
        self.requests = []

    async def generate(self, request: dict) -> ModelResponse:
        self.requests.append(request)
        queue = self.responses.get(request['_task_type'], deque()) if isinstance(self.responses, dict) else self.responses
        if not queue:
            raise ModelClientError('no predetermined fake response remains')
        result = queue.popleft()
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, str):
            return ModelResponse(result)
        if isinstance(result, dict):
            return ModelResponse(**result)
        return result

    async def aclose(self) -> None:
        pass


def make_model_client(config: AppConfig):
    """Select a provider while keeping the executor's audited task interface."""
    if not config.tokenizer:
        raise ValueError('real model calls require a local fast tokenizer; set config.tokenizer')
    if config.context_tokens > config.service_context_tokens:
        raise ValueError('logical context_tokens exceeds service_context_tokens')
    if config.provider == 'train':
        from .train_client import TrainModelClient
        return TrainModelClient(config)
    return VllmChatClient(config)
