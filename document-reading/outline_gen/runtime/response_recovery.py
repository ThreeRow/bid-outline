"""Shared response recovery, independent of transport, caching and scheduling.

Each logical request owns one ResponseRecovery. Callers execute its decisions
(repair, regenerate, accept or fail), retaining their own I/O and batch layout.
Text validators are supported without forcing their output through JSON.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from typing import Any, Callable, Literal

from outline_gen.io.json_extract import (
    JsonExtractionError, JsonExtractionResult,
    capture_json_parsing, extract_json_detailed,
)
from .errors import sanitize_error_message
from .port import ChatRequest
from .tokenization import count_text_tokens, clip_tokens


def contract_feedback(error: BaseException, raw: str | None = None) -> str:
    evidence = getattr(error, "findings", [])
    try:
        evidence_text = json.dumps(evidence, ensure_ascii=False, default=str) if evidence else ""
    except (TypeError, ValueError, RecursionError):
        evidence_text = "证据无法序列化；请依据错误及候选回答修订。"
    return (
        "\n\n请根据前面的任务要求和资料，修改下方内容。"
        "\n检查发现的问题：" + sanitize_error_message(error)[:2000]
        + ("\n具体位置与原句：" + evidence_text if evidence_text else "")
        + ("\n待修改内容：\n<candidate_answer>\n" + raw + "\n</candidate_answer>" if raw is not None else "")
        + "\n修改要求：修正上述问题及相关表述，保留其余正确内容，不添加资料中没有的事实或要求。"
          "按要求的格式返回修改后的完整内容，不只返回修改部分，不附解释。"
    )


def _syntax_repair_system() -> str:
    return (
        "你是 JSON 结构修复器（仅语法层）。只修复 JSON 语法、转义、代码围栏、"
        "首尾噪声和确定性的截断配平问题；不得新增、删减、概括、改写、"
        "翻译或虚构任何业务字段和值。只输出一个 JSON，禁止代码围栏、"
        "解释或前后缀。"
    )


def _clip_for_repair(raw: str, limit: int = 12000) -> str:
    budget = limit
    if count_text_tokens(raw) <= budget:
        return raw
    return clip_tokens(raw, budget * 3 // 5) + "\n...\n" + clip_tokens(raw, budget * 2 // 5, tail=True)


def build_syntax_repair_request(
    *, raw: str, error: BaseException, model: str, max_tokens: int,
    clip_raw: bool = True,
) -> ChatRequest:
    repair_user = (
        f"语法错误：{sanitize_error_message(error)}\n\n"
        "请将以下原始回复恢复为合法 JSON；完整保留原有业务字段和值，"
        "不得补写或修改业务字段。无法恢复时返回"
        '{"error":"invalid_source"}，不要编造内容：\n\n'
        + (_clip_for_repair(raw) if clip_raw else raw)
    )
    return ChatRequest(
        system=_syntax_repair_system(), user=repair_user, model=model,
        temperature=0.0, max_tokens=max_tokens,
    )


class OutputRecoveryExhausted(ValueError):
    """Finite model output recovery failed; stage may use source-based fallback."""

    def __init__(self, message, *, error=None, attempts=0):
        self.output_error = error
        self.attempts = attempts
        super().__init__(message)


@dataclass(frozen=True)
class RecoveryDecision:
    action: Literal["accept", "repair", "retry", "fail"]
    value: Any = None
    decoded: JsonExtractionResult | None = None
    error: Exception | None = None
    phase: Literal["syntax", "contract"] | None = None
    feedback: str = ""
    parse_diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass
class ResponseRecovery:
    max_contract_retries: int = 1
    max_syntax_repairs: int = 1
    # ModelCalls historically retries every failed original attempt; shared
    # structured callers only regenerate syntax failures after a failed repair.
    retry_unrepaired_syntax: bool = False
    reject_repair_error_object: bool = False
    model_repairs: int = 0
    deterministic_repairs: int = 0
    contract_retries: int = 0
    local_repairs: int = 0
    parse_history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.max_contract_retries = max(0, int(self.max_contract_retries))
        self.max_syntax_repairs = max(0, int(self.max_syntax_repairs))

    def evaluate(
        self, raw: str, validate: Callable[[Any], Any] | None = None, *,
        repaired: bool = False, raw_validator: bool = False,
        retry_allowed: bool | None = None,
    ) -> RecoveryDecision:
        with capture_json_parsing() as trace:
            try:
                decision = self._evaluate(raw, validate, repaired=repaired,
                                          raw_validator=raw_validator, retry_allowed=retry_allowed)
            finally:
                self.parse_history.extend(trace.diagnostics)
                self.local_repairs += sum(d.get("local_repair_attempts", 0) for d in trace.diagnostics)
                self.deterministic_repairs += sum(d.get("repair_count", 0) for d in trace.diagnostics)
        return replace(decision, parse_diagnostics=tuple(trace.diagnostics))

    def _evaluate(
        self, raw: str, validate: Callable[[Any], Any] | None = None, *,
        repaired: bool = False, raw_validator: bool = False,
        retry_allowed: bool | None = None,
    ) -> RecoveryDecision:
        """Decode and validate, then decide the next response-level action.

        raw_validator preserves ModelCalls' text/JSON validator API. Only its
        JsonExtractionError signals JSON syntax failure. retry_allowed lets an
        outer total-attempt budget account for transport failures as well.
        """
        decoded = None
        phase = "syntax"
        has_candidate = not (raw_validator and repaired and self.reject_repair_error_object)
        try:
            if raw_validator:
                if repaired and self.reject_repair_error_object:
                    data = extract_json_detailed(raw).data
                    phase = "contract"
                    if isinstance(data, dict) and ("error" in data or "invalid_source" in data):
                        raise ValueError(f"JSON语法修复无法恢复原始回复：{data}")
                phase = "contract"
                has_candidate = True
                try:
                    value = validate(raw)
                except JsonExtractionError:
                    phase = "syntax"
                    raise
            else:
                decoded = extract_json_detailed(raw)
                phase = "contract"
                value = validate(decoded.data) if validate is not None else decoded.data
            return RecoveryDecision("accept", value=value, decoded=decoded)
        except Exception as error:
            if phase == "syntax" and not isinstance(error, JsonExtractionError):
                raise
            if phase == "syntax" and not repaired and self.model_repairs < max(0, self.max_syntax_repairs):
                self.model_repairs += 1
                return RecoveryDecision("repair", error=error, phase=phase)
            feedback = contract_feedback(error, raw) if phase == "contract" and has_candidate else ""
            can_retry = (self.contract_retries < max(0, self.max_contract_retries)
                         if retry_allowed is None else retry_allowed)
            if can_retry and (phase == "contract" or repaired or self.retry_unrepaired_syntax):
                self.contract_retries += 1
                return RecoveryDecision("retry", error=error, phase=phase, feedback=feedback, decoded=decoded)
            return RecoveryDecision("fail", error=error, phase=phase, feedback=feedback, decoded=decoded)
