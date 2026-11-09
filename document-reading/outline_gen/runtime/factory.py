"""运行时工厂。"""

from __future__ import annotations

from outline_gen.runtime.openai_api import OpenAIChatRuntime
from outline_gen.runtime.port import ChatRuntime, RuntimeConfig
from outline_gen.runtime.stub import StubChatRuntime


def _create_runtime(cfg: RuntimeConfig) -> ChatRuntime:
    kind = (cfg.kind or "api").lower()
    if kind in ("stub", "dry-run", "dry_run"):
        return StubChatRuntime()
    if kind in ("api", "openai", "vllm"):
        return OpenAIChatRuntime(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            max_in_flight=cfg.max_in_flight,
            timeout_seconds=cfg.api_timeout_seconds,
            extra_body=cfg.extra_body,
        )
    if kind in ("train", "chat_train"):
        from outline_gen.runtime.train_api import TrainChatRuntime

        return TrainChatRuntime(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout_seconds=cfg.train_timeout_seconds,
            poll_interval_seconds=cfg.train_poll_interval_seconds,
            max_query_list=(cfg.max_in_flight if cfg.train_max_query_list is None
                            else cfg.train_max_query_list),
            max_generation_attempts=cfg.train_max_generation_attempts,
            transport_retry_attempts=cfg.train_transport_retry_attempts,
            transport_retry_base_seconds=cfg.train_transport_retry_base_seconds,
        )
    if kind in ("langchain", "lc"):
        from outline_gen.runtime.langchain_adapter import LangChainChatRuntime

        return LangChainChatRuntime(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            max_in_flight=cfg.max_in_flight,
        )
    raise ValueError(
        f"未知 runtime kind={cfg.kind!r}，可选: api, train, stub, langchain"
    )


def create_runtime(cfg: RuntimeConfig) -> ChatRuntime:
    from .call_policy import CallPolicy
    from .tokenization import get_tokenizer
    policy = cfg.call_policy or CallPolicy()
    get_tokenizer(policy.tokenizer)  # Fail before a job, never fall back to estimates.
    if cfg.call_policy is not None:
        from dataclasses import replace
        cfg = replace(cfg, train_max_generation_attempts=policy.retries.train_generation_attempts,
                      train_transport_retry_attempts=policy.retries.train_transport_retries)
    runtime = _create_runtime(cfg)
    runtime.call_policy = policy
    runtime.budget_runtime_kind = (cfg.kind or "api").lower()
    return runtime
