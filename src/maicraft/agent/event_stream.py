"""事件流长轮询的读取与游标管理：角色事件流和公屏事件流共用。

mod 把事件写进一条流（``events``），调用方带着上次读到的游标去读，没有新事件时
服务端最多等 ``wait_ms`` 再空手返回。本模块只管读：首次读取只建立游标（接手之前
发生的事不补发）、游标作废与流更换的识别、翻积压、读取失败后退避重试。
事件交给谁处理由调用方决定（见 goal_watch.py 的 ``GoalWatch.step``）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from maicraft.utils.logger import get_logger

from .mod_protocol import EVENTS, EventsPage, McpCall, events_page_of

logger = get_logger("EventStream")

# 一次长轮询最多等多久：远低于 MCP 请求超时，服务端上限是 60 秒。
DEFAULT_WAIT_MS = 25_000
# 连接没就绪或读取失败后，隔多久再试；连续失败时逐步放慢，最慢半分钟一次。
RETRY_DELAYS_S = (1.0, 2.0, 5.0, 10.0, 30.0)
# 一次长轮询空手返回得比这还快，说明服务端没有等：停这么久再读，避免空转。
EMPTY_READ_FLOOR_S = 1.0


@dataclass(frozen=True, slots=True)
class StreamRead:
    """一次读到的页与它的处境。

    ``first`` 是接手后第一次读到（这一页与翻掉的积压都不该交给调用方处理）；
    ``stream_changed`` 是流编号和之前读到的不一样（换世界或游戏重启过）；
    ``resync`` 为真时调用方要重新核对自己记着的东西（游标作废后的重读）。
    """

    page: EventsPage
    first: bool
    stream_changed: bool
    resync: bool


class EventStream:
    """一条事件流的游标与读取。所有方法都要在事件循环里调用。"""

    def __init__(
        self,
        *,
        call: McpCall,
        wait_ms: int = DEFAULT_WAIT_MS,
        topic: str | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._call = call
        self._wait_ms = wait_ms
        self._topic = topic
        # 返回单调秒数；缺省用事件循环的时间
        self._clock = clock
        self._sleep = sleep
        self._stream_id: str | None = None
        # 读到过的最后一个流编号：连接重建（forget）时也留着，拿来认出 mod 是不是换了流。
        self._seen_stream_id: str | None = None
        self._cursor = 0
        self._failures = 0

    @property
    def stream_id(self) -> str | None:
        return self._stream_id

    @property
    def cursor(self) -> int:
        return self._cursor

    def forget(self) -> None:
        """连接重建后调用：下次读取当作第一次，重新建立游标。"""
        self._stream_id = None
        self._cursor = 0

    def _now(self) -> float:
        return self._clock() if self._clock is not None else asyncio.get_running_loop().time()

    async def _back_off(self) -> None:
        delay = RETRY_DELAYS_S[min(self._failures, len(RETRY_DELAYS_S) - 1)]
        self._failures += 1
        await self._sleep(delay)

    async def read(self) -> StreamRead | None:
        """读一页事件；读取失败时按退避间隔等待并返回 None，不抛异常。

        首次读取与游标作废后的那一页只用来建立游标，页里的事件不交给调用方处理
        （接手之前发生的事不补发）；积压一页页翻到最新，同样不交给调用方处理。
        翻页途中流换了就忘掉游标，返回 None，下一轮按首次读取重来。
        """
        first = self._stream_id is None
        arguments: dict[str, Any] = {"wait_ms": 0 if first else self._wait_ms}
        if self._topic is not None:
            arguments["topic"] = self._topic
        if not first:
            arguments["stream_id"] = self._stream_id
            arguments["after_cursor"] = self._cursor
        started = self._now()
        reply = await self._call(EVENTS, arguments)
        if not reply.ok:
            logger.warning(f"读取事件流失败：{reply.error_code} {reply.error_message}")
            await self._back_off()
            return None
        page = events_page_of(reply.data)
        if page is None:
            logger.warning(f"事件流的回复读不懂，稍后重试：{reply.raw}")
            await self._back_off()
            return None
        self._failures = 0
        if first or page.cursor_status != "valid" or page.stream_id != self._stream_id:
            # 游标作废：旧游标不再有效，重新核对由调用方做。
            resync = not first
            # 换没换流看编号本身：连接重建后首次读到的编号和之前的不一样，也是重进了世界或 mod 重启过；
            # 同一条流只是游标失效、或接手时第一次读，都不算。
            stream_changed = self._seen_stream_id is not None and page.stream_id != self._seen_stream_id
            self._stream_id = self._seen_stream_id = page.stream_id
            self._cursor = page.cursor
            if not await self._skip_backlog(page.has_more):
                return None
            return StreamRead(page=page, first=first, stream_changed=stream_changed, resync=resync)
        self._cursor = page.cursor
        if not page.events and self._now() - started < EMPTY_READ_FLOOR_S:
            # 服务端没按要求等就空手返回（旧版本或异常）：停一下再读，不让调用方空转。
            await self._sleep(EMPTY_READ_FLOOR_S)
        return StreamRead(page=page, first=False, stream_changed=False, resync=False)

    async def _skip_backlog(self, has_more: bool) -> bool:
        """不带游标读到的是流里最早保留的一页：一页页翻到最新，旧事件不补发。

        返回是否还在同一条流上；翻页途中流又换了或读失败时忘掉游标，返回 False。
        """
        while has_more:
            arguments: dict[str, Any] = {
                "stream_id": self._stream_id,
                "after_cursor": self._cursor,
                "wait_ms": 0,
            }
            if self._topic is not None:
                arguments["topic"] = self._topic
            reply = await self._call(EVENTS, arguments)
            page = events_page_of(reply.data) if reply.ok else None
            if page is None or page.stream_id != self._stream_id:
                self.forget()
                return False
            self._cursor = page.cursor
            has_more = page.has_more
        return True
