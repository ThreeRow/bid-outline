"""Adapter for the asynchronous local ``chat_train`` HTTP API.

The service accepts a submission containing a bounded ``query_list`` and returns a
single ``responseId``.  Poll responses may contain any subset of query ids; this
adapter joins them by id, caches completed answers, and resubmits only failed items.
Transport failures while polling never create a new response id.
"""

from __future__ import annotations

import json as jsonlib
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from outline_gen.runtime.batch import RuntimeMetrics, RuntimeMetricsSnapshot
from outline_gen.runtime.errors import (
    ModelContextLengthError,
    ModelInvocationError,
    ModelPermanentError,
    ModelTransientError,
    classify_model_error,
    sanitize_error_message,
)
from outline_gen.runtime.port import ChatBatchResult, ChatItem, ChatItemResult, ChatRequest


SUBMIT_PATH = "/api/v2/chat_train"
RESULT_PATH = "/api/v2/get_resp_data"
DEFAULT_MAX_QUERY_LIST = 8


class _TrainItemFailed(ModelTransientError):
    """One generation failed and may be retried in a fresh train submission."""


class _HTTPStatusError(RuntimeError):
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = int(status_code)
        detail = body.decode("utf-8", errors="replace")[:2000]
        super().__init__(f"HTTP {status_code}: {detail}")


class _JsonResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _HTTPStatusError(self.status_code, self._body)

    def json(self) -> Any:
        return jsonlib.loads(self._body.decode("utf-8"))


class _JsonClient:
    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers
        self._timeout = timeout

    def post(self, path: str, *, json: Any) -> _JsonResponse:
        request = Request(
            self._base_url + "/" + path.lstrip("/"),
            data=jsonlib.dumps(json, ensure_ascii=False).encode("utf-8"),
            headers=self._headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return _JsonResponse(response.status, response.read())
        except HTTPError as exc:
            return _JsonResponse(exc.code, exc.read())
        except URLError as exc:
            raise ConnectionError(f"train API connection failed: {exc.reason}") from exc


def _snapshot(value: Any, *, max_chars: int = 2000) -> str:
    try:
        text = jsonlib.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    text = sanitize_error_message(text)
    return text if len(text) <= max_chars else text[:max_chars] + "...<truncated>"


def _item_failure_detail(item: dict[str, Any]) -> str:
    """Extract the provider's newly exposed generation failure reason.

    Keep the complete sanitized item snapshot as evidence, while promoting the
    common reason fields to the front of the message so service/job reports are
    immediately actionable across provider versions.
    """

    reasons: list[str] = []
    for key in (
        "error",
        "error_message",
        "error_msg",
        "reason",
        "message",
        "msg",
        "detail",
        "failure_reason",
    ):
        value = item.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = sanitize_error_message(_snapshot(value, max_chars=1000))
        reasons.append(f"{key}={rendered}")
    prefix = "; ".join(reasons) if reasons else "provider did not include a failure reason"
    return f"{prefix}; item={_snapshot(item)}"


def _item_failure_error(
    item: dict[str, Any], *, query_id: str, response_id: str
) -> ModelInvocationError:
    detail = _item_failure_detail(item)
    message = (
        f"train generation failed for query id {query_id}; "
        f"response_id={response_id}: {detail}"
    )
    # ``is_success=false`` used to carry no reason and is retryable.  Newer
    # provider versions expose a textual error; promote context-limit reasons
    # to the typed error expected by invoke_chat so it can compact once rather
    # than blindly repeating the identical oversized prompt three times.
    classified = classify_model_error(RuntimeError(detail))
    if classified is ModelContextLengthError:
        return ModelContextLengthError(message)
    return _TrainItemFailed(message)


@dataclass
class _TrainTask:
    item: ChatItem
    query_id: str
    response_id: str | None = None
    generation_attempt: int = 1


@dataclass
class _PollOutcome:
    answers: dict[str, str]
    failed: dict[str, BaseException]


def _transport_error(exc: BaseException, *, operation: str) -> ModelInvocationError:
    """Classify transport/HTTP failures without leaking provider secrets."""

    if isinstance(exc, ModelInvocationError):
        return exc
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if isinstance(exc, _HTTPStatusError) or status is not None:
        text = sanitize_error_message(exc)
        # 429 and 5xx are explicitly retryable for result polling.  Other 4xx
        # responses are permanent (bad auth/request, unknown response id, ...).
        if status == 429 or (status is not None and status >= 500):
            return ModelTransientError(text, status_code=status)
        if status is not None and 400 <= status <= 499:
            return ModelPermanentError(text, status_code=status)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return ModelTransientError(
            f"train {operation} transport failed: {sanitize_error_message(exc)}"
        )
    return ModelTransientError(
        f"train {operation} transport failed: {sanitize_error_message(exc)}"
    )


def _body_status_code(body: dict[str, Any]) -> int | None:
    raw = body.get("status")
    # ``status=1`` is the train API success marker, not HTTP 1.
    if raw in (None, 0, 1, True, False):
        return None
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return None
    return code if 400 <= code <= 599 else None


class TrainChatRuntime:
    """Synchronous ``ChatRuntime`` facade over asynchronous train jobs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
        max_query_list: int = DEFAULT_MAX_QUERY_LIST,
        max_generation_attempts: int = 3,
        transport_retry_attempts: int = 3,
        transport_retry_base_seconds: float = 0.5,
        client: Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._max_query_list = max(1, int(max_query_list))
        self._max_generation_attempts = max(1, int(max_generation_attempts))
        self._transport_retry_attempts = max(0, int(transport_retry_attempts))
        self._transport_retry_base_seconds = max(0.0, float(transport_retry_base_seconds))
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._metrics = RuntimeMetrics()
        self._pending_lock = threading.Lock()
        # Compatibility state for invoke_chat retries: a timed-out single request
        # keeps its original response id and is resumed by the next ``chat`` call.
        self._pending: dict[ChatRequest, tuple[str, str]] = {}
        if client is None:
            headers = {"Content-Type": "application/json"}
            if api_key and api_key != "EMPTY":
                headers["Authorization"] = f"Bearer {api_key}"
            client = _JsonClient(
                base_url=base_url.rstrip("/"),
                headers=headers,
                timeout=self._timeout_seconds,
            )
        self._client = client

    @property
    def max_query_list(self) -> int:
        return self._max_query_list

    @property
    def max_in_flight(self) -> int:
        # Train batches are intentionally submitted serially; query_list is the
        # provider-side bound and therefore the effective in-flight batch count is 1.
        return 1

    @property
    def metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def get_metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def metrics_snapshot(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    @staticmethod
    def _new_query_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _build_query(system: str | None, user: str) -> str:
        from outline_gen.runtime.tokenization import train_query
        return train_query(system, user)

    @staticmethod
    def _json(response: Any, *, operation: str) -> dict[str, Any]:
        try:
            raw_status = getattr(response, "status_code", None)
            if raw_status is not None and int(raw_status) >= 400:
                raise _HTTPStatusError(int(raw_status), b"")
            response.raise_for_status()
        except Exception as exc:
            raise _transport_error(exc, operation=operation) from exc
        try:
            body = response.json()
        except Exception as exc:
            raise ModelTransientError(
                f"train {operation} returned non-JSON response: {sanitize_error_message(exc)}"
            ) from exc
        if not isinstance(body, dict):
            raise ModelTransientError(
                f"train {operation} returned invalid body: {_snapshot(body)}"
            )
        return body

    def _post_json(self, path: str, *, payload: Any, operation: str) -> dict[str, Any]:
        self._metrics.inc("http_requests")
        if operation == "submission":
            self._metrics.inc("train_submissions")
        else:
            self._metrics.inc("train_polls")
        try:
            response = self._client.post(path, json=payload)
        except Exception as exc:
            raise _transport_error(exc, operation=operation) from exc
        return self._json(response, operation=operation)

    def _submit_tasks(self, tasks: list[_TrainTask]) -> str:
        if not tasks:
            raise ValueError("cannot submit an empty train batch")
        if len(tasks) > self._max_query_list:
            raise ValueError(
                f"train query_list exceeds max_query_list={self._max_query_list}"
            )
        payload = {
            "query_list": [
                {
                    "id": task.query_id,
                    "query": self._build_query(
                        task.item.request.system,
                        task.item.request.user,
                    ),
                    "timeout": int(self._timeout_seconds),
                    "temperature": task.item.request.temperature,
                    "max_tokens": task.item.request.max_tokens,
                    "res_type": "str",
                }
                for task in tasks
            ]
        }
        body = self._post_json(SUBMIT_PATH, payload=payload, operation="submission")
        response_id = body.get("responseId") or body.get("response_id")
        body_status = _body_status_code(body)
        if body_status is not None and body_status != 429 and body_status < 500:
            raise ModelPermanentError(
                f"train submission rejected with HTTP-like status {body_status}: "
                f"{_snapshot(body)}",
                status_code=body_status,
            )
        if (
            body.get("status") != 1
            or body.get("data") is not True
            or response_id in (None, "")
        ):
            raise ModelTransientError(
                "train submission was not accepted or missing responseId: "
                f"{_snapshot(body)}"
            )
        return str(response_id)

    def _poll_tasks(
        self,
        tasks: list[_TrainTask],
        response_id: str,
        *,
        strict_missing: bool = False,
    ) -> _PollOutcome:
        """Poll one response id, joining arbitrary partial data by query id."""

        remaining = {task.query_id: task for task in tasks}
        answers: dict[str, str] = {}
        failed: dict[str, BaseException] = {}
        deadline = self._monotonic() + self._timeout_seconds
        transport_retries = 0

        while remaining:
            try:
                body = self._post_json(
                    RESULT_PATH,
                    payload={"response_id": response_id},
                    operation="result query",
                )
            except ModelTransientError as exc:
                # A transient query must retain this response id.  Retry a bounded
                # number immediately; if exhausted, leave unresolved items pending
                # so a later invoke_chat call can resume the same job.
                if self._monotonic() >= deadline or transport_retries >= self._transport_retry_attempts:
                    for query_id in remaining:
                        failed[query_id] = exc
                    return _PollOutcome(answers=answers, failed=failed)
                transport_retries += 1
                self._metrics.inc("transport_retries")
                delay = self._transport_retry_base_seconds * (2 ** (transport_retries - 1))
                if delay:
                    self._sleep(delay)
                continue
            except ModelPermanentError as exc:
                # Permanent 4xx is terminal for every unresolved item in this
                # response.  Completed answers remain in the cache/result.
                for query_id in remaining:
                    failed[query_id] = exc
                return _PollOutcome(answers=answers, failed=failed)

            status = body.get("status")
            if status != 1:
                body_status = _body_status_code(body)
                if body_status is not None and body_status != 429 and body_status < 500:
                    exc = ModelPermanentError(
                        f"train result query rejected with HTTP-like status {body_status}: "
                        f"{_snapshot(body)}",
                        status_code=body_status,
                    )
                    for query_id in remaining:
                        failed[query_id] = exc
                    return _PollOutcome(answers=answers, failed=failed)
                # Application-level non-success has no reliable new response id;
                # treat it like a transient poll and continue the same job.
                exc = ModelTransientError(
                    f"train result query failed: {_snapshot(body)}"
                )
                if self._monotonic() >= deadline or transport_retries >= self._transport_retry_attempts:
                    for query_id in remaining:
                        failed[query_id] = exc
                    return _PollOutcome(answers=answers, failed=failed)
                transport_retries += 1
                self._metrics.inc("transport_retries")
                delay = self._transport_retry_base_seconds * (2 ** (transport_retries - 1))
                if delay:
                    self._sleep(delay)
                continue

            # A successful response (including ``data=[]`` while the job is
            # still running) clears the consecutive transport-failure budget.
            # Application-level 429/5xx bodies above remain bounded by
            # ``transport_retry_attempts`` just like HTTP exceptions.
            transport_retries = 0

            data = body.get("data")
            if data == []:
                # Empty data means “not ready”, never “submit again”.
                if self._monotonic() >= deadline:
                    timeout = ModelTransientError(
                        f"train result polling timed out after {self._timeout_seconds:g}s; "
                        f"response_id={response_id}"
                    )
                    for query_id in remaining:
                        failed[query_id] = timeout
                    return _PollOutcome(answers=answers, failed=failed)
                if self._poll_interval_seconds:
                    self._sleep(self._poll_interval_seconds)
                continue
            if not isinstance(data, list):
                exc = ModelTransientError(
                    f"train result data is not a list: {_snapshot(body)}"
                )
                for query_id in remaining:
                    failed[query_id] = exc
                return _PollOutcome(answers=answers, failed=failed)

            for item in data:
                if not isinstance(item, dict):
                    continue
                query_id = str(item.get("id"))
                task = remaining.get(query_id)
                if task is None:
                    continue
                is_success = item.get("is_success")
                if is_success is False:
                    failed[query_id] = _item_failure_error(
                        item,
                        query_id=query_id,
                        response_id=response_id,
                    )
                    remaining.pop(query_id, None)
                    continue
                answer = item.get("answer")
                if is_success is True and isinstance(answer, str) and answer.strip():
                    answers[query_id] = answer
                    remaining.pop(query_id, None)
                    continue
                if is_success is True:
                    failed[query_id] = _TrainItemFailed(
                        f"train generation returned an empty answer: {_snapshot(item)}"
                    )
                    remaining.pop(query_id, None)

            if remaining:
                # A non-empty ``data`` body is the terminal response for this
                # response id.  Every submitted id must be represented in that
                # body; otherwise isolate the missing ids as generation failures.
                # (Only data=[] means “not ready” and is polled again above.)
                joined_ids = ",".join(remaining)
                exc = _TrainItemFailed(
                    "train completed result does not contain query id "
                    f"{joined_ids}; upstream={_snapshot(body)}"
                )
                for query_id in remaining:
                    failed[query_id] = exc
                return _PollOutcome(answers=answers, failed=failed)

        return _PollOutcome(answers=answers, failed=failed)

    def _single_task(self, req: ChatRequest) -> tuple[_TrainTask, bool]:
        with self._pending_lock:
            pending = self._pending.get(req)
        if pending is not None:
            query_id, response_id = pending
            return (
                _TrainTask(
                    item=ChatItem(logical_item_id=query_id, request=req),
                    query_id=query_id,
                    response_id=response_id,
                ),
                True,
            )
        query_id = str(self._new_query_id())
        task = _TrainTask(
            item=ChatItem(logical_item_id=query_id, request=req),
            query_id=query_id,
        )
        task.query_id = task.item.logical_item_id
        task.response_id = self._submit_tasks([task])
        with self._pending_lock:
            self._pending[req] = (task.query_id, task.response_id)
        return task, False

    def chat(self, req: ChatRequest) -> str:
        self._metrics.update(logical_items=1, generation_attempts=1)
        try:
            task, _resumed = self._single_task(req)
            assert task.response_id is not None
            outcome = self._poll_tasks([task], task.response_id, strict_missing=True)
            if task.query_id in outcome.answers:
                answer = outcome.answers[task.query_id]
                with self._pending_lock:
                    self._pending.pop(req, None)
                self._metrics.inc("generation_successes")
                return answer
            error = outcome.failed.get(task.query_id) or ModelTransientError(
                f"train result did not complete query id {task.query_id}"
            )
            # A transient timeout/transport failure keeps pending state for resume;
            # item generation failures and permanent HTTP failures start cleanly.
            if not isinstance(error, ModelTransientError) or isinstance(error, _TrainItemFailed):
                with self._pending_lock:
                    self._pending.pop(req, None)
            raise error
        except Exception:
            # Submission failures occur before a pending entry exists; all failures
            # are counted here exactly once.  Poll timeouts retain pending state for
            # a subsequent invoke_chat retry.
            self._metrics.inc("generation_failures")
            raise

    def chat_many(
        self,
        items: Iterable[ChatItem],
        max_in_flight: int | None = None,
    ) -> ChatBatchResult:
        normalized = list(items)
        seen: set[str] = set()
        for item in normalized:
            if not isinstance(item, ChatItem):
                raise TypeError("chat_many expects ChatItem values")
            if item.logical_item_id is None or not str(item.logical_item_id).strip():
                raise ValueError("logical_item_id must not be empty")
            if item.logical_item_id in seen:
                raise ValueError(f"duplicate logical_item_id: {item.logical_item_id}")
            seen.add(item.logical_item_id)
        self._metrics.inc("logical_items", len(normalized))
        if not normalized:
            return ChatBatchResult(results=[], metrics=self.metrics, wall_seconds=0.0, max_in_flight=1)

        # A stage may request a smaller logical-node limit than the runtime's
        # transport ceiling. Poll each chunk to completion before submitting
        # the next, so both initial requests and retries respect that limit.
        chunk_size = self._max_query_list
        if max_in_flight is not None:
            chunk_size = min(chunk_size, max(1, int(max_in_flight)))
        batch_started = time.monotonic()
        answers: dict[str, str] = {}
        errors: dict[str, BaseException] = {}
        unresolved = list(normalized)
        attempts: dict[str, int] = {item.logical_item_id: 0 for item in normalized}
        elapsed: dict[str, float] = {item.logical_item_id: 0.0 for item in normalized}

        while unresolved:
            next_unresolved: list[ChatItem] = []
            for start in range(0, len(unresolved), chunk_size):
                chunk = unresolved[start : start + chunk_size]
                tasks: list[_TrainTask] = []
                for item in chunk:
                    attempts[item.logical_item_id] += 1
                    self._metrics.inc("generation_attempts")
                    tasks.append(
                        _TrainTask(
                            item=item,
                            query_id=str(self._new_query_id()),
                            generation_attempt=attempts[item.logical_item_id],
                        )
                    )
                chunk_started = time.monotonic()
                try:
                    response_id = self._submit_tasks(tasks)
                    for task in tasks:
                        task.response_id = response_id
                    outcome = self._poll_tasks(tasks, response_id)
                except Exception as exc:
                    classified = _transport_error(exc, operation="submission")
                    outcome = _PollOutcome(
                        answers={},
                        failed={task.query_id: classified for task in tasks},
                    )
                chunk_elapsed = max(0.0, time.monotonic() - chunk_started)
                for item in chunk:
                    elapsed[item.logical_item_id] += chunk_elapsed

                by_query = {task.query_id: task for task in tasks}
                for query_id, answer in outcome.answers.items():
                    task = by_query.get(query_id)
                    if task is not None:
                        answers[task.item.logical_item_id] = answer
                for query_id, error in outcome.failed.items():
                    task = by_query.get(query_id)
                    if task is None:
                        continue
                    logical_id = task.item.logical_item_id
                    if isinstance(error, _TrainItemFailed) and attempts[logical_id] < self._max_generation_attempts:
                        next_unresolved.append(task.item)
                    else:
                        errors[logical_id] = error
            unresolved = next_unresolved

        results: list[ChatItemResult] = []
        for item in normalized:
            logical_id = item.logical_item_id
            if logical_id in answers:
                self._metrics.inc("generation_successes")
                results.append(
                    ChatItemResult(
                        logical_item_id=logical_id,
                        response=answers[logical_id],
                        request=item.request,
                        attempts=attempts[logical_id],
                        elapsed_seconds=elapsed[logical_id],
                    )
                )
            else:
                self._metrics.inc("generation_failures")
                results.append(
                    ChatItemResult(
                        logical_item_id=logical_id,
                        error=errors.get(logical_id)
                        or ModelTransientError("train generation did not complete"),
                        request=item.request,
                        attempts=attempts[logical_id],
                        elapsed_seconds=elapsed[logical_id],
                    )
                )
        return ChatBatchResult(
            results=results,
            metrics=self.metrics,
            wall_seconds=max(0.0, time.monotonic() - batch_started),
            max_in_flight=1,
        )
