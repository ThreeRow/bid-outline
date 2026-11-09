"""运行配置：provider、默认深度/预算、阶段名。

密钥优先读环境变量：
  OUTLINE_GEN_API_KEY（兼容旧 SILICONFLOW_API_KEY）
  OUTLINE_GEN_BASE_URL
  OUTLINE_GEN_MODEL
  OUTLINE_GEN_API_TIMEOUT_SECONDS
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import TypedDict


class ProviderConfig(TypedDict):
    runtime: str
    base_url: str
    api_key: str
    model: str


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _positive_float_env(name: str, default: float) -> float:
    """Read a finite positive duration from the environment.

    A malformed, non-positive, or non-finite value must not disable request
    timeouts accidentally.  Falling back to the finite default keeps provider
    calls bounded even when deployment configuration is incomplete.
    """
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return max(1.0, value)


PROVIDERS: dict[str, ProviderConfig] = {
    "api": {
        "runtime": "api",
        "base_url": _env(
            "OUTLINE_GEN_BASE_URL",
            # 旧部署无需立刻迁移环境变量；provider 名称已与厂商解耦。
            _env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        ),
        "api_key": _env(
            "OUTLINE_GEN_API_KEY",
            _env("SILICONFLOW_API_KEY", ""),
        ),
        "model": _env(
            "OUTLINE_GEN_MODEL",
            "Qwen/Qwen2.5-72B-Instruct",
        ),
    },
    "vllm": {
        "runtime": "api",
        "base_url": _env("OUTLINE_GEN_BASE_URL", "http://127.0.0.1:8000/v1"),
        "api_key": _env("OUTLINE_GEN_API_KEY", "EMPTY"),
        "model": _env("OUTLINE_GEN_MODEL", "Qwen2.5-72B-Instruct"),
    },
    "train": {
        "runtime": "train",
        "base_url": _env(
            "OUTLINE_GEN_TRAIN_BASE_URL",
            # "http://192.168.0.9:50007",
            "http://10.21.150.137:50007",
        ),
        "api_key": _env("OUTLINE_GEN_TRAIN_API_KEY", ""),
        # train 服务绑定本地模型；该名称主要用于输出目录和运行记录。
        "model": _env("OUTLINE_GEN_TRAIN_MODEL", "qwen35"),
    },
}
DEFAULT_PROVIDER = "vllm"

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
REPO_DIR = PROJECT_DIR.parent
PROMPT_DIR = PACKAGE_DIR / "prompts"
DEFAULT_OUTPUT_ROOT = REPO_DIR / "output"

# 流水线阶段（顺序即默认执行序）
STAGES = (
    "canonicalize",
    "router",
    "cognitive_map",
    "root",
    "expand",
    "review_loop",
    "finalize",
)

DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_CHILDREN = 12
DEFAULT_MAX_REVIEW_ROUNDS = 2
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 0.2
DEFAULT_CALL_BUDGET = 500
DEFAULT_MAX_IN_FLIGHT = max(1, int(_env("OUTLINE_GEN_MAX_IN_FLIGHT", "4")))
# The OpenAI SDK has a provider-dependent default timeout.  Keep this explicit
# and finite so a queued/oversized long-context request cannot wait forever.
DEFAULT_API_TIMEOUT_SECONDS = _positive_float_env(
    "OUTLINE_GEN_API_TIMEOUT_SECONDS", 600.0
)
DEFAULT_RUNTIME = "auto"  # auto 跟随 provider；dry-run 仍强制 stub
DEFAULT_TOTAL_WORDS = 350_000  # 0=不启用字数预算；>0 为技术标正文约数


def resolve_provider(
    name: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ProviderConfig:
    # 保留程序调用兼容；CLI 只展示新的通用名称 api。
    if name == "siliconflow":
        name = "api"
    if name not in PROVIDERS:
        raise SystemExit(f"未知 provider={name!r}，可选: {', '.join(PROVIDERS)}")
    cfg: ProviderConfig = dict(PROVIDERS[name])  # type: ignore[assignment]
    if model:
        cfg["model"] = model
    if api_key:
        cfg["api_key"] = api_key
    if base_url:
        cfg["base_url"] = base_url
    return cfg


def resolve_runtime_kind(cfg: ProviderConfig, requested: str | None) -> str:
    """显式 runtime 优先，否则采用 provider 对应的协议适配器。"""
    kind = (requested or "auto").strip().lower()
    return cfg["runtime"] if kind == "auto" else kind
