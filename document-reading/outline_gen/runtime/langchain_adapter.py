"""LangChain ChatOpenAI 适配器（可选依赖）。

安装：
  pip install 'langchain-core>=0.3' 'langchain-openai>=0.3'
或：
  pip install -e '.[langchain]'
"""

from __future__ import annotations

from outline_gen.runtime.batch import RuntimeMetrics, RuntimeMetricsSnapshot, run_chat_many
from outline_gen.runtime.port import (
    DEFAULT_MAX_IN_FLIGHT,
    ChatBatchResult,
    ChatItem,
    ChatRequest,
)


def _import_chat_openai():
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "使用 --runtime langchain 需要安装可选依赖："
            " pip install 'langchain-core>=0.3' 'langchain-openai>=0.3'"
        ) from exc
    return ChatOpenAI


class LangChainChatRuntime:
    """用 LangChain ChatOpenAI 实现 ChatRuntime 端口。

    与 OpenAIChatRuntime 相同契约：system+user → 纯文本。
    不引入 Agent/Tool；仅换传输层，便于后续再扩展。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._ChatOpenAI = _import_chat_openai()
        self._max_in_flight = max(1, int(max_in_flight))
        self._metrics = RuntimeMetrics()

    @property
    def metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def get_metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def metrics_snapshot(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def _chat_attempt(self, req: ChatRequest) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        self._metrics.update(generation_attempts=1, http_requests=1)
        llm = self._ChatOpenAI(
            model=req.model,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )
        messages = []
        if req.system:
            messages.append(SystemMessage(content=req.system))
        messages.append(HumanMessage(content=req.user))
        result = llm.invoke(messages)
        content = getattr(result, "content", result)
        if isinstance(content, list):
            # 部分模型返回 content blocks
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(str(block))
            return "".join(parts)
        return str(content or "")

    def chat(self, req: ChatRequest) -> str:
        try:
            result = self._chat_attempt(req)
        except Exception:
            self._metrics.inc("generation_failures")
            raise
        self._metrics.inc("generation_successes")
        return result

    def chat_many(
        self,
        items: list[ChatItem] | tuple[ChatItem, ...],
        max_in_flight: int | None = None,
    ) -> ChatBatchResult:
        return run_chat_many(
            items,
            lambda item: self._chat_attempt(item.request),
            max_in_flight=self._max_in_flight if max_in_flight is None else max_in_flight,
            metrics=self._metrics,
        )
