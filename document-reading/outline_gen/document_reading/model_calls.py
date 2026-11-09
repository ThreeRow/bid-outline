"""Plain chat only, bounded retries and content-addressed validated caches."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import json
import time
from threading import Lock
from collections import Counter
from outline_gen.io.fs import write_json, write_text
from outline_gen.io.prompts import read_prompt
from outline_gen.runtime.port import ChatRequest
from outline_gen.runtime.call_policy import CallPolicy, stage_tokens
from outline_gen.runtime.tokenization import tokenizer_scope, tokenizer_identity, count_request_tokens
from outline_gen.runtime.output_issues import attach_output_context
from outline_gen.io.json_extract import json_parsing_identity
from outline_gen.runtime.response_recovery import ResponseRecovery, OutputRecoveryExhausted, build_syntax_repair_request
from outline_gen.domain.material_boundary import MaterialBoundaryError
from .config import ReadingConfig

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


class ModelCalls:
    def _count_request_tokens(self, *args, **kwargs):
        kwargs["chat_template_kwargs"] = self.config.extra_body.get("chat_template_kwargs", {})
        return count_request_tokens(*args, **kwargs)

    def __init__(self, runtime, config: ReadingConfig, output: Path):
        if config.extra_body != getattr(runtime, "extra_body", {}):
            raise ValueError("阅读配置与实际运行时扩展参数不一致，不能复用请求缓存")
        self.runtime, self.config, self.output = runtime, config, output
        self._lock = Lock()
        self._events = []
        self._active = 0
        self._max_active = 0
        self._cache_hits = []
        self._json_parsing = []

    def metrics(self):
        with self._lock:
            events = list(self._events)
            return {
                "max_active_requests": self._max_active,
                "request_attempts": len(events),
                "stage_attempts": dict(Counter(e["stage"] for e in events)),
                "events": events,
                "cache_hits": list(self._cache_hits),
                "json_parsing": list(self._json_parsing),
                "local_repair_attempts": sum(d.get("local_repair_attempts", 0)
                    for event in self._json_parsing for d in event["diagnostics"]),
                "model_syntax_repairs": sum(e["stage"] == "json_syntax_repair" for e in events),
                "generation_attempts": sum(e["stage"] != "json_syntax_repair" for e in events),
            }

    def _chat(self, key, request, stage=None):
        stage = stage or (
            "primary"
            if key.startswith("primary_")
            else (
                "secondary"
                if key.startswith("secondary_")
                else "table" if key.startswith("table_") else "toc"
            )
        )
        with self._lock:
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        started = time.perf_counter()
        status = "failed"
        try:
            result = self.runtime.chat(request)
            status = "received"
            return result
        finally:
            with self._lock:
                self._active -= 1
                self._events.append(
                    {
                        "key": key,
                        "stage": stage,
                        "status": status,
                        "elapsed_seconds": time.perf_counter() - started,
                        "input_tokens": self._count_request_tokens(request, tokenizer=self.config.tokenizer,
                            runtime_kind=getattr(self.runtime,"budget_runtime_kind", "train" if getattr(self.runtime,"plain_chat_only",False) else "api")),
                        "output_tokens_limit": request.max_tokens,
                        "temperature": request.temperature,
                    }
                )

    def _record_parsing(self, key, decision, *, kind):
        if decision.parse_diagnostics:
            with self._lock:
                self._json_parsing.append({"key": key, "kind": kind,
                                           "diagnostics": list(decision.parse_diagnostics)})

    def ask(self, *args, **kwargs):
        with tokenizer_scope(self.config.tokenizer):
            return self._ask(*args, **kwargs)

    def _ask(self, key, prompt_name, user, validate, *, max_tokens=None, stage=None, on_boundary_exhausted=None, on_output_exhausted=None, issue_context=None):
        system = read_prompt(PROMPTS / prompt_name)
        settings = asdict(self.config)
        for operational in ("resume", "workers", "heading_workers", "attempts", "model_call_cache"):
            settings.pop(operational, None)
        identity = {
            "tokenization": tokenizer_identity(),
            "system": system,
            "user": user,
            "config": settings,
            "protocol": 2,
            "json_parsing": json_parsing_identity(),
            "max_tokens": max_tokens,
            "boundary_fallback": on_boundary_exhausted is not None,
            "output_fallback": on_output_exhausted is not None,
            "json_syntax_repair": {"limit": self.config.retries.syntax_repairs, "request": asdict(build_syntax_repair_request(
                raw="", error=ValueError(""), model=self.config.model,
                max_tokens=min(max_tokens or self.config.max_tokens, self.config.max_tokens),
                clip_raw=False,
            ))},
        }
        if stage is not None:
            identity["stage"] = stage
        fingerprint = sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        directory = self.output / "model_calls" / key / fingerprint
        cached = directory / "validated.json"
        request_budget = self.config.request_max_tokens
        kind = getattr(self.runtime, "budget_runtime_kind", "train" if getattr(self.runtime,"plain_chat_only",False) else "api")
        if self._count_request_tokens(system=system, user=user, runtime_kind=kind) > request_budget:
            raise ValueError(f"{key} 请求超过token预算，需要分片")
        def validate_cached(raw):
            decision = ResponseRecovery(max_contract_retries=0, max_syntax_repairs=0).evaluate(
                raw, validate, raw_validator=True,
            )
            self._record_parsing(key, decision, kind="cache_revalidation")
            if decision.action != "accept":
                raise decision.error
            return decision.value

        error = ""
        external = getattr(self.config, "model_call_cache", None)
        if external:
            prior = Path(external) / "model_calls" / key / fingerprint / "validated.json"
            if prior.is_file():
                try:
                    saved = json.loads(prior.read_text(encoding="utf-8"))
                    result = validate_cached(saved["response"])
                    write_json(cached, {"protocol": 2, "response": saved["response"], "revalidated_from": str(prior.resolve())})
                    with self._lock:
                        self._cache_hits.append({"key": key, "fingerprint": fingerprint, "source": str(prior.resolve())})
                    return result
                except (ValueError, TypeError, KeyError) as cache_error:
                    from outline_gen.runtime.generation_recovery import record_generation_fallback
                    record_generation_fallback(self.output, "model_cache", cache_error,
                        policy="empty_optional", operation="cache_revalidation",
                        evidence={"cache": str(prior.resolve())},
                        fallback_detail="外部成功缓存无法重验，忽略缓存重新生成。")
        if self.config.resume and cached.exists():
            try:
                return validate_cached(
                    json.loads(cached.read_text(encoding="utf-8"))["response"]
                )
            except (ValueError, TypeError, KeyError) as exc:
                error = f"缓存不再满足契约：{exc}"
                from outline_gen.runtime.generation_recovery import record_generation_fallback
                record_generation_fallback(self.output, "model_cache", exc,
                    policy="empty_optional", operation="cache_revalidation",
                    evidence={"cache": str(cached.resolve())},
                    fallback_detail="本地成功缓存损坏或不再满足契约，重新生成。")
        recovery = ResponseRecovery(
            retry_unrepaired_syntax=True, reject_repair_error_object=True,
            max_syntax_repairs=self.config.retries.syntax_repairs,
        )
        correction = ""
        last_output_error = None
        attempts = self.config.retries.model_attempts
        for attempt in range(1, attempts + 1):
            decision = None
            last_output_error = None
            request = user + correction + (
                f"\n\n待修订候选未通过校验：{error}\n请按规定格式重新返回完整结果。"
                if error and not correction
                else ""
            )
            stem = directory / f"attempt_{attempt:02d}"
            # Avoid overwriting prior evidence if a previous attempt failed.
            suffix = 1
            while stem.with_suffix(".input.txt").exists():
                stem = directory / f"attempt_{attempt:02d}_retry_{suffix}"
                suffix += 1
            write_text(stem.with_suffix(".input.txt"), system + "\n\n" + request)
            try:
                if self._count_request_tokens(system=system, user=request, runtime_kind=kind) > request_budget:
                    raise ValueError("重试输入超过token预算，需要分片")
                # No response_format/guided_json; no automatic context truncation.
                raw = self._chat(
                    key,
                    ChatRequest(
                        system,
                        request,
                        self.config.model,
                        temperature=self.config.temperature,
                        max_tokens=min(max_tokens or self.config.max_tokens, self.config.max_tokens),
                    ),
                    stage=stage,
                )
                write_text(stem.with_suffix(".response.txt"), raw)
                decision = recovery.evaluate(
                    raw, validate, raw_validator=True,
                    retry_allowed=attempt < attempts,
                )
                self._record_parsing(key, decision, kind="generation")
                if decision.action == "repair":
                    write_json(stem.with_suffix(".status.json"), {
                        "status": "syntax_error", "error": str(decision.error)[:1500],
                        "json_parsing": list(decision.parse_diagnostics),
                    })
                    repair_stem = stem.parent / (stem.name + "_syntax_repair")
                    repair_request = build_syntax_repair_request(
                        raw=raw, error=decision.error, model=self.config.model,
                        max_tokens=min(max_tokens or self.config.max_tokens, self.config.max_tokens),
                        clip_raw=False,
                    )
                    write_text(repair_stem.with_suffix(".input.txt"), repair_request.system + "\n\n" + repair_request.user)
                    try:
                        if count_request_tokens(repair_request, runtime_kind=kind) > request_budget:
                            raise ValueError("JSON修复输入超过token预算；不截断原始回复，改为原任务重生成")
                        repaired = self._chat(key, repair_request, stage="json_syntax_repair")
                        write_text(repair_stem.with_suffix(".response.txt"), repaired)
                        raw = repaired
                        decision = recovery.evaluate(
                            repaired, validate, repaired=True, raw_validator=True,
                            retry_allowed=attempt < attempts,
                        )
                        self._record_parsing(key, decision, kind="model_syntax_repair")
                        if decision.action != "accept":
                            raise decision.error
                        write_json(repair_stem.with_suffix(".status.json"), {"status": "validated", "json_parsing": list(decision.parse_diagnostics)})
                    except Exception as repair_error:
                        write_json(repair_stem.with_suffix(".status.json"), {
                            "status": "failed", "error": f"{type(repair_error).__name__}: {repair_error}"[:1500],
                            "json_parsing": list(getattr(repair_error, "parse_diagnostics", decision.parse_diagnostics if decision else ())),
                        })
                        raise
                if decision.action != "accept":
                    raise decision.error
                result = decision.value
                write_json(stem.with_suffix(".status.json"), {
                    "status": "validated", "model_syntax_repairs": recovery.model_repairs,
                    "local_repair_attempts": recovery.local_repairs,
                    "json_parsing": list(decision.parse_diagnostics),
                })
                write_json(cached, {"protocol": 2, "response": raw,
                                    "json_parsing": list(decision.parse_diagnostics)})
                return result
            except Exception as exc:
                partial = getattr(exc, "partial_content", None)
                if isinstance(partial, str) and partial:
                    write_text(stem.with_suffix(".partial.txt"), partial)
                error = f"{type(exc).__name__}: {exc}"[:1500]
                correction = decision.feedback if decision is not None and exc is decision.error else ""
                if decision is not None and exc is decision.error:
                    last_output_error = exc
                write_json(
                    stem.with_suffix(".status.json"),
                    {"status": "failed", "error": error,
                     "json_parsing": list(getattr(exc, "parse_diagnostics", decision.parse_diagnostics if decision else ()))},
                )
                if (attempt == attempts and isinstance(exc, MaterialBoundaryError)
                        and on_boundary_exhausted is not None):
                    attach_output_context(exc, request_key=key, logical_item_id=key,
                        **(issue_context or {}), evidence={"directory": str(directory.resolve()),
                        "requests": [str(p.resolve()) for p in sorted(directory.glob("*.input.txt"))],
                        "responses": [str(p.resolve()) for p in sorted(directory.glob("*.response.txt"))]})
                    try:
                        result = on_boundary_exhausted(raw, exc)
                    except (ValueError, TypeError) as fallback_error:
                        if on_output_exhausted is None:
                            raise
                        exhausted = OutputRecoveryExhausted(
                            f"{key} 待核候选仍未满足结构约束：{fallback_error}",
                            error=fallback_error, attempts=attempts + recovery.model_repairs,
                        )
                        attach_output_context(exhausted, **getattr(exc, "output_context", {}))
                        result = on_output_exhausted(exhausted)
                    write_json(stem.with_suffix(".status.json"), {
                        "status": "needs_review", "error": error, "findings": exc.findings,
                        "json_parsing": list(decision.parse_diagnostics) if decision else [],
                    })
                    # Do not publish a failed candidate as a validated cache.
                    return result
                if "ContextLength" in type(exc).__name__ or "context_length" in str(
                    exc
                ):
                    break
        def contextualize(error):
            return attach_output_context(error, **{
                **(issue_context or {}), "request_key": key, "logical_item_id": key,
                "attempts": {"generation": attempt, "local_repair": recovery.local_repairs,
                             "model_repair": recovery.model_repairs},
                "evidence": {"directory": str(directory.resolve()),
                    "requests": [str(p.resolve()) for p in sorted(directory.glob("*.input.txt"))],
                    "responses": [str(p.resolve()) for p in sorted(directory.glob("*.response.txt"))],
                    "statuses": [str(p.resolve()) for p in sorted(directory.glob("*.status.json"))]},
            })
        if last_output_error is not None:
            exhausted = OutputRecoveryExhausted(f"{key} 重试耗尽：{error}",
                                               error=last_output_error, attempts=attempts + recovery.model_repairs)
            contextualize(exhausted)
            if on_output_exhausted is not None:
                result = on_output_exhausted(exhausted)
                write_json(stem.with_suffix(".status.json"), {
                    "status": "needs_review", "error": str(exhausted),
                    "json_parsing": list(decision.parse_diagnostics), "output_fallback": True,
                })
                return result
            raise exhausted from last_output_error
        raise ValueError(f"{key} 重试耗尽：{error}")
