"""Dry-run / 测试用 stub 运行时（不调模型）。"""

from __future__ import annotations

from outline_gen.runtime.batch import RuntimeMetrics, RuntimeMetricsSnapshot, run_chat_many
from outline_gen.runtime.port import ChatBatchResult, ChatItem, ChatRequest


class StubChatRuntime:
    def __init__(self, *, max_in_flight: int = 1) -> None:
        self._max_in_flight = max(1, int(max_in_flight))
        self._metrics = RuntimeMetrics()

    @property
    def metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def get_metrics(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def metrics_snapshot(self) -> RuntimeMetricsSnapshot:
        return self._metrics.snapshot()

    def chat(self, req: ChatRequest) -> str:
        self._metrics.update(generation_attempts=1, generation_failures=1)
        raise RuntimeError(
            "StubChatRuntime 不应被调用；dry-run 阶段应走 stub_* 分支，不进入 live 调用"
        )

    def chat_many(
        self,
        items: list[ChatItem] | tuple[ChatItem, ...],
        max_in_flight: int | None = None,
    ) -> ChatBatchResult:
        # Keep the fallback deterministic and failure-isolated.  Calling ``chat``
        # through run_chat_many would double-count failures, so use a tiny private
        # function that records attempts and lets the helper record outcomes.
        def unavailable(item: ChatItem) -> str:
            self._metrics.inc("generation_attempts")
            raise RuntimeError(
                "StubChatRuntime 不应被调用；dry-run 阶段应走 stub_* 分支，不进入 live 调用"
            )

        return run_chat_many(
            items,
            unavailable,
            max_in_flight=1,
            metrics=self._metrics,
        )
