"""Deterministic identities, bounded scheduling and input presentation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any, TypeVar

from bid_outline.model.references import Presented

T = TypeVar("T")
R = TypeVar("R")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity(prefix: str, value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return prefix + hashlib.sha256(encoded).hexdigest()[:20]


def dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [dump(v) for v in value]
    if isinstance(value, dict):
        return {k: dump(v) for k, v in value.items()}
    return value


def present(**sections: Any) -> str:
    """Show complete materials; no implicit summarization or clipping."""
    def semantic(value):
        if isinstance(value, Presented):
            return value.view
        value = dump(value)
        if isinstance(value, list):
            return [semantic(v) for v in value]
        if isinstance(value, dict):
            # These fields are audit metadata, not source/obligation content. The
            # full forms remain in artifacts; prompts carry IDs for source lookup.
            omit = {'result_ref', 'producing_artifact_id', 'producer_ref', 'input_artifact_ids'}
            if "version_id" in value and "nodes" in value:
                omit |= {"created_at", "input_artifact_ids"}
            if "node_id" in value and "title" in value:
                omit.add("source_refs")
            if "requirement_id" in value and "content" in value:
                omit.add("producing_task_id")
            return {k: semantic(v) for k, v in value.items() if k not in omit}
        return value

    return Presented({'__sections__': [[name, semantic(value)] for name, value in sections.items()]})


def ids(text: str) -> list[str]:
    import re

    if text.strip() in {"", "无", "ROOT", "不适用"}:
        return []
    return [part for part in re.split(r"[、,，;；\s]+", text.strip()) if part]


def batches(items: Iterable[T], max_chars: int, render: Callable[[T], str] = str) -> list[list[T]]:
    """Keep semantic records intact; an oversized record is handled explicitly later."""
    output: list[list[T]] = []
    current: list[T] = []
    size = 0
    for item in items:
        length = len(render(item))
        if current and size + length > max_chars:
            output.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        output.append(current)
    return output


async def bounded_map(
    fn: Callable[[T], Awaitable[R]], items: list[T], *, workers: int, capacity: int
) -> list[R]:
    """Producer is separate from workers, so child work cannot deadlock the queue."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
    results: list[Any] = [None] * len(items)

    async def produce() -> None:
        for index, item in enumerate(items):
            await queue.put((index, item))
        for _ in range(workers):
            await queue.put(None)

    async def consume() -> None:
        while True:
            entry = await queue.get()
            try:
                if entry is None:
                    return
                index, item = entry
                results[index] = await fn(item)
            finally:
                queue.task_done()

    async with asyncio.TaskGroup() as group:
        group.create_task(produce())
        for _ in range(workers):
            group.create_task(consume())
    return results
