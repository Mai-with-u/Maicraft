"""共享记忆服务的接口与空实现：一期只定接口，不做真正的 HTTP 客户端。

记忆服务在本 agent 之外（MaiGO 的共享记忆），这里只约定三种用法：取一段背景放进提示词、
上报一件事、按需回忆。``[maigo] enabled = false`` 时用 ``DisabledMemoryService``，什么都不做。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["DisabledMemoryService", "MemoryContext", "MemoryService"]


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """放进提示词的一段背景。"""

    stable_brief: str = ""
    live_context: str = ""


@runtime_checkable
class MemoryService(Protocol):
    """共享记忆服务的接口。"""

    async def context(self) -> MemoryContext:
        """取一段背景，放进提示词。"""
        ...

    async def report_event(
        self,
        kind: str,
        summary: str,
        *,
        occurred_at_ms: int | None = None,
    ) -> None:
        """上报一件事；``kind`` 用 MaiGO 的取值：speech/interaction/milestone/promise_update/forget_request。"""
        ...

    async def recall(self, query: str) -> list[str]:
        """按需回忆，返回相关的记忆条目。"""
        ...


class DisabledMemoryService:
    """什么都不做的空实现：取背景得到空、上报不做事、回忆得到空列表。"""

    async def context(self) -> MemoryContext:
        return MemoryContext()

    async def report_event(
        self,
        kind: str,
        summary: str,
        *,
        occurred_at_ms: int | None = None,
    ) -> None:
        return None

    async def recall(self, query: str) -> list[str]:
        return []
