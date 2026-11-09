"""Shared JSON decoding: envelope cleanup, stdlib first, json-repair fallback."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from hashlib import sha256
from importlib.metadata import version
import json
import re
from typing import Any, Iterator

from json_repair import repair_json

JSON_REPAIR_VERSION = version("json-repair")
JSON_PARSING_POLICY_VERSION = 5


def json_parsing_identity() -> dict[str, Any]:
    """Include parser policy/dependency changes in request and resume identities."""
    return {"policy": JSON_PARSING_POLICY_VERSION, "backend": "json-repair",
            "version": JSON_REPAIR_VERSION, "schema": False, "multiple_values": "reject"}


class JsonExtractionError(ValueError):
    """The response could not be recovered as an unambiguous JSON candidate."""

    def __init__(self, message: str, *, repairs: tuple[str, ...] = (), diagnostics=None) -> None:
        self.repairs = tuple(repairs)
        self.detail = str(message)
        self.diagnostics = diagnostics or {}
        super().__init__(message)


class JsonRepairBackendError(JsonExtractionError):
    """Local parser/backend failed; preserve evidence and use model recovery."""


@dataclass(frozen=True)
class JsonExtractionResult:
    data: Any
    text: str
    repairs: tuple[str, ...] = ()
    backend: str = "stdlib"
    backend_version: str | None = None
    cleaned_text: str = ""
    preprocessing: tuple[str, ...] = ()
    repair_log: tuple[dict[str, str], ...] = ()
    source_range: tuple[int, int] | None = None

    @property
    def repair_count(self) -> int:
        """Compatibility summary: preprocessing actions plus local repair calls."""
        return len(self.repairs)

    @property
    def local_repair_count(self) -> int:
        return int(self.backend == "json-repair")

    @property
    def syntax_repairs(self) -> int:
        return self.repair_count

    @property
    def value(self) -> Any:
        return self.data

    @property
    def json_data(self) -> Any:
        return self.data

    def diagnostic(self) -> dict[str, Any]:
        return {"status": "parsed", "backend": self.backend, "backend_version": self.backend_version,
                "preprocessing": list(self.preprocessing), "repair_count": self.repair_count,
                "local_repair_attempts": self.local_repair_count, "repair_log": list(self.repair_log),
                "cleaned_text": self.cleaned_text, "parsed_text": self.text,
                "source_range": list(self.source_range) if self.source_range is not None else None,
                "content_preservation": "not_verified" if self.local_repair_count else "unchanged"}


@dataclass
class JsonParseSession:
    """Per-validation memo and evidence, isolated across threads/async contexts."""
    cache: dict[str, JsonExtractionResult | JsonExtractionError] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


_SESSION: ContextVar[JsonParseSession | None] = ContextVar("json_parse_session", default=None)


@contextmanager
def capture_json_parsing() -> Iterator[JsonParseSession]:
    session = JsonParseSession()
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)


def strip_code_fence(text: str) -> str:
    """Only remove a complete outer JSON/untagged Markdown code fence."""
    raw = (text or "").strip().lstrip("\ufeff").strip()
    match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n?```[ \t]*", raw, re.IGNORECASE)
    return match.group(1).strip() if match else raw


def _container_spans(text: str) -> list[tuple[int, int]]:
    """Find complete top-level containers; do not split nested or quoted data.

    This is an envelope/ambiguity check, not a second repair parser. Support
    comments and common quote styles so ordinary local repair isn't preempted.
    Ambiguities not recognizable here still belong to downstream validation.
    """
    spans = []
    stack = []
    start = 0
    quote = None
    escaped = False
    comment = None
    pairs = {"}": "{", "]": "["}
    index = 0
    while index < len(text):
        char = text[index]
        pair = text[index:index + 2]
        if comment:
            if comment == "line" and char in "\r\n":
                comment = None
            elif comment == "block" and pair == "*/":
                comment = None
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif pair in ("//", "/*") and stack:
            comment = "line" if pair == "//" else "block"
            index += 1
        elif char in ('"', "'", "“", "‘") and stack:
            quote = {"“": "”", "‘": "’"}.get(char, char)
        elif char in "{[":
            if not stack:
                start = index
            stack.append(char)
        elif char in "}]" and stack:
            if stack[-1] != pairs[char]:
                # Don't treat a nested fragment of a damaged root as the answer.
                return []
            stack.pop()
            if not stack:
                spans.append((start, index + 1))
        index += 1
    return spans


def _decode(original: str) -> JsonExtractionResult:
    base = original.strip().lstrip("\ufeff").strip()
    raw = strip_code_fence(original)
    preprocessing = []
    if base != original.strip():
        preprocessing.append("bom")
    if raw != base:
        preprocessing.append("code_fence")
    diagnostic = {"status": "failed", "backend": "stdlib", "backend_version": None,
                  "preprocessing": preprocessing, "repair_count": len(preprocessing),
                  "local_repair_attempts": 0, "cleaned_text": raw}
    if not raw:
        raise JsonExtractionError("模型输出为空，无法解析 JSON", diagnostics=diagnostic)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        stdlib_error = str(error)
    else:
        return JsonExtractionResult(data, raw, tuple(preprocessing), cleaned_text=raw,
                                    preprocessing=tuple(preprocessing))

    spans = _container_spans(raw)
    if len(spans) > 1:
        diagnostic["candidate_count"] = len(spans)
        raise JsonExtractionError("回复包含多个顶层JSON候选，不能自动选择或合并", diagnostics=diagnostic)
    if spans and spans[0] != (0, len(raw)):
        start, end = spans[0]
        # Retain only a single complete object surrounded by prose/fences.
        # A remaining opening bracket may be another, truncated candidate.
        remainder = raw[:start] + raw[end:]
        if any(char in remainder for char in "{[}]"):
            raise JsonExtractionError("JSON主体外围仍有未完成或歧义结构", diagnostics=diagnostic)
        raw = raw[start:end]
        preprocessing.append("balanced_extract")
        diagnostic.update(cleaned_text=raw, preprocessing=preprocessing, repair_count=len(preprocessing))
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            pass
        else:
            return JsonExtractionResult(data, raw, tuple(preprocessing), cleaned_text=raw,
                                        preprocessing=tuple(preprocessing))

    diagnostic.update(backend="json-repair", backend_version=JSON_REPAIR_VERSION,
                      local_repair_attempts=1, repair_count=len(preprocessing) + 1)
    try:
        data, log = repair_json(raw, return_objects=True, logging=True, skip_json_loads=True)
    except ValueError as error:
        raise JsonExtractionError(f"JSON本地修复失败: {error}; 标准解析: {stdlib_error}",
                                  diagnostics=diagnostic) from error
    except Exception as error:
        raise JsonRepairBackendError(f"json-repair运行异常: {type(error).__name__}: {error}",
                                     diagnostics=diagnostic) from error
    diagnostic["repair_log"] = log
    if not isinstance(data, (dict, list)):
        raise JsonExtractionError("JSON本地修复未恢复对象或数组（可能是空结果或噪声）",
                                  diagnostics=diagnostic)
    repaired_text = json.dumps(data, ensure_ascii=False)
    return JsonExtractionResult(data, repaired_text, (*preprocessing, "json_repair"),
                                "json-repair", JSON_REPAIR_VERSION, raw, tuple(preprocessing), tuple(log))


def extract_json_detailed(text: str) -> JsonExtractionResult:
    """Stdlib decode first; local repair never fills fields using a schema.

    Local recovery can change or omit content. The caller must still validate
    the business contract. A validation scope memoizes repeated parsing only
    within that scope and records evidence even when the business parser fails.
    """
    original = str(text or "")
    session = _SESSION.get()
    if session is not None and original in session.cache:
        saved = session.cache[original]
        if isinstance(saved, JsonExtractionError):
            raise saved
        return deepcopy(saved)
    input_id = sha256(original.encode(errors="surrogatepass")).hexdigest()
    try:
        result = _decode(original)
        start = original.find(result.cleaned_text)
        if start >= 0:
            result = replace(result, source_range=(start, start + len(result.cleaned_text)))
        if session is not None:
            # A recursion error while copying a deep result must also recover.
            session.cache[original] = deepcopy(result)
            session.diagnostics.append({"input_sha256": input_id, **result.diagnostic()})
    except JsonExtractionError as error:
        if session is not None:
            session.cache[original] = error
            session.diagnostics.append({"input_sha256": input_id, **error.diagnostics, "error": str(error)})
        raise
    except (RecursionError, TypeError, ValueError, OverflowError) as error:
        # Includes stdlib nesting/number limits, envelope scanning and repaired
        # output serialization. These must use the same finite recovery route.
        wrapped = JsonRepairBackendError(
            f"JSON解析处理异常: {type(error).__name__}: {error}",
            diagnostics={"status": "failed", "backend": "json_pipeline",
                         "local_repair_attempts": 0, "repair_count": 0},
        )
        if session is not None:
            session.cache[original] = wrapped
            session.diagnostics.append({"input_sha256": input_id, **wrapped.diagnostics,
                                        "error": str(wrapped)})
        raise wrapped from error
    return result


def extract_json(text: str) -> Any:
    return extract_json_detailed(text).data


safe_extract_json = extract_json_detailed
decode_json = extract_json_detailed
