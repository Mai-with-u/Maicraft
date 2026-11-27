"""记忆服务空实现直测：取背景得到空、上报不做事、回忆得到空列表。"""

from __future__ import annotations

import asyncio

from maicraft.agent.memory_service import DisabledMemoryService, MemoryContext, MemoryService


def test_disabled_service_returns_empty() -> None:
    service: MemoryService = DisabledMemoryService()

    async def main() -> None:
        context = await service.context()
        assert context == MemoryContext(stable_brief="", live_context="")
        assert await service.recall("家在哪") == []
        # 上报不抛异常也不产生结果
        assert await service.report_event("speech", "说了一句话") is None
        assert await service.report_event("milestone", "第一次挖到钻石", occurred_at_ms=1000) is None

    asyncio.run(main())
