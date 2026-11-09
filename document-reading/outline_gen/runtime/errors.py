"""模型调用错误分类。

上层依赖稳定的 category/retryable/attempts 判断失败原因，不需要了解
OpenAI、vLLM 或 LangChain 的具体异常类型。
"""

from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def sanitize_error_message(value: Any) -> str:
    """移除异常文本中常见的密钥形态。"""
    text = str(value or "").strip() or type(value).__name__
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1***", text)
        else:
            text = pattern.sub("***", text)
    return text


class ModelInvocationError(RuntimeError):
    """统一的模型调用失败。"""

    category = "permanent"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        status_code: int | None = None,
        stage: str = "",
        logical_item_id: str | None = None,
    ) -> None:
        self.attempts = max(1, int(attempts))
        self.status_code = status_code
        # ``stage`` and ``logical_item_id`` are optional on transport errors for
        # backwards compatibility.  Structured-output errors always populate
        # them, while callers that only use the old runtime API can continue to
        # construct Model* errors with the original signature.
        self.stage = str(stage or "")
        self.logical_item_id = logical_item_id
        clean = sanitize_error_message(message)
        self.detail = clean
        super().__init__(
            f"model invocation failed [{self.category}] "
            f"after {self.attempts} attempt(s): {clean}"
        )


class ModelContextLengthError(ModelInvocationError):
    category = "context_length"
    retryable = True


class ModelTransientError(ModelInvocationError):
    category = "transient"
    retryable = True


class ModelPermanentError(ModelInvocationError):
    category = "permanent"
    retryable = False


class ModelResponseFormatError(ModelInvocationError):
    """模型回复在有限修复后仍不满足阶段结构契约。"""

    category = "response_format"
    retryable = False


class ModelOutputTruncatedError(ModelTransientError):
    """Provider reported a token-length stop before completing the response."""

    category = "output_truncated"
    retryable = True

    def __init__(
        self,
        message: str = "model output stopped at the output token limit",
        *,
        partial_content: str = "",
        finish_reason: str = "length",
        attempts: int = 1,
        status_code: int | None = None,
        stage: str = "",
        logical_item_id: str | None = None,
    ) -> None:
        # Keep the provider response available to callers that can recover
        # complete objects from a truncated JSON array.
        self.partial_content = str(partial_content or "")
        self.finish_reason = str(finish_reason or "length")
        super().__init__(
            message,
            attempts=attempts,
            status_code=status_code,
            stage=stage,
            logical_item_id=logical_item_id,
        )


class StructuredOutputError(RuntimeError):
    """Provider-independent failure while decoding/validating structured output.

    The runtime deliberately keeps this separate from :class:`ModelInvocationError`:
    a model may have replied successfully and still produce malformed or
    contract-invalid structured data.  Every instance carries enough context for
    stage-level diagnostics without exposing provider-specific exception types.
    """

    category = "structured"
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        stage: str = "",
        logical_item_id: str | None = None,
        category: str | None = None,
        attempts: int = 1,
        detail: str | None = None,
    ) -> None:
        self.stage = str(stage or "")
        self.logical_item_id = logical_item_id
        self.category = str(category or self.category)
        self.attempts = max(1, int(attempts))
        self.detail = sanitize_error_message(
            detail if detail is not None else (message or "structured output failed")
        )
        location = self.stage or "structured output"
        if self.logical_item_id:
            location += f"/{self.logical_item_id}"
        super().__init__(
            f"{location} failed [{self.category}] after "
            f"{self.attempts} attempt(s): {self.detail}"
        )


class SyntaxDecodeError(StructuredOutputError):
    """Structured output could not be safely decoded as JSON."""

    category = "syntax"
    retryable = True


class ContractError(StructuredOutputError):
    """Decoded JSON does not satisfy the stage/domain contract."""

    category = "contract"
    retryable = True


# Explicit aliases used by integrations that name the layer in the exception.
StructuredSyntaxError = SyntaxDecodeError
StructuredContractError = ContractError


def error_status_code(exc: BaseException) -> int | None:
    """兼容 OpenAI/httpx 风格异常读取 HTTP 状态码。"""
    candidates = [
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ]
    for raw in candidates:
        try:
            if raw is not None:
                return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def classify_model_error(exc: BaseException) -> type[ModelInvocationError]:
    """按状态码、异常类名和兼容 API 文本分类。"""
    if isinstance(exc, ModelContextLengthError):
        return ModelContextLengthError
    if isinstance(exc, ModelOutputTruncatedError):
        return ModelOutputTruncatedError
    if isinstance(exc, ModelTransientError):
        return ModelTransientError
    if isinstance(exc, ModelInvocationError):
        return type(exc)

    status = error_status_code(exc)
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    context_markers = (
        "context_length_exceeded",
        "context window",
        "context length",
        "maximum context",
        "maximum input length",
        "max context",
        "max_model_len",
        "input_tokens",
        "prompt_tokens",
        "prompt is too long",
        "too many tokens",
        "token limit",
        "上下文长度",
        "上下文超限",
    )
    if any(marker in message for marker in context_markers):
        return ModelContextLengthError

    transient_statuses = {408, 409, 425, 429}
    transient_names = (
        "timeout",
        "connection",
        "ratelimit",
        "rate_limit",
        "temporarilyunavailable",
        "internalserver",
    )
    if status in transient_statuses or (status is not None and 500 <= status <= 599):
        return ModelTransientError
    if any(marker in name for marker in transient_names):
        return ModelTransientError
    return ModelPermanentError
