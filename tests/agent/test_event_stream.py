"""事件流读取直测：首次读取建游标、翻积压、游标作废与流更换、失败退避、空页等待、topic 参数。"""

from __future__ import annotations

from typing import Any

from maicraft.agent.event_stream import DEFAULT_WAIT_MS, EMPTY_READ_FLOOR_S, RETRY_DELAYS_S, EventStream
from maicraft.agent.mod_protocol import EVENTS, ModReply, events_page_of, no_reply, reply_of
from tests.fakes import ManualClock, ok_envelope, tool_result


class _ScriptedEvents:
    """按脚本回 events 的假调用：记下每次参数，脚本用完再调用就报错。"""

    def __init__(self, clock: ManualClock | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replies: list[ModReply] = []
        self._clock = clock
        # 每次往返消耗的虚拟秒数：调大可以模拟一次耗时的往返
        self.advance_per_call = 0.1

    async def __call__(self, name: str, arguments: dict[str, Any]) -> ModReply:
        assert name == EVENTS
        self.calls.append(dict(arguments))
        if self._clock is not None:
            self._clock.advance(self.advance_per_call)  # 每次往返耗一点时间，供空页等待判定
        if not self.replies:
            raise AssertionError("脚本里没有准备好下一页回复")
        return self.replies.pop(0)


def _page(
    stream_id: str = "s1",
    cursor: int = 5,
    events: list[dict[str, Any]] | None = None,
    has_more: bool = False,
    cursor_status: str = "valid",
) -> ModReply:
    return reply_of(
        tool_result(
            ok_envelope(
                {
                    "stream_id": stream_id,
                    "cursor": cursor,
                    "events": events or [],
                    "has_more": has_more,
                    "cursor_status": cursor_status,
                }
            )
        )
    )


def _recorder() -> tuple[list[float], Any]:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    return sleeps, record


async def test_first_read_only_sets_the_cursor_and_does_not_replay_old_events() -> None:
    mod = _ScriptedEvents()
    old = [{"cursor": 1, "kind": "finished", "goal_id": 1}, {"cursor": 2, "kind": "paused", "goal_id": 1}]
    mod.replies.append(_page(cursor=2, events=old))
    stream = EventStream(call=mod, wait_ms=25_000)

    read = await stream.read()

    assert mod.calls == [{"wait_ms": 0}], "首次读取不带 stream_id 与 after_cursor"
    assert read is not None
    assert read.first is True and read.resync is False and read.stream_changed is False
    assert read.page.events == old
    assert stream.stream_id == "s1" and stream.cursor == 2

    # 之后的读取带上游标与等待时长
    mod.replies.append(_page(cursor=4, events=[{"cursor": 4, "kind": "resumed", "goal_id": 1}]))
    read = await stream.read()
    assert mod.calls[1] == {"wait_ms": 25_000, "stream_id": "s1", "after_cursor": 2}
    assert read is not None and read.first is False and read.resync is False
    assert stream.cursor == 4


async def test_backlog_is_paged_through_without_being_handed_over() -> None:
    mod = _ScriptedEvents()
    mod.replies.append(_page(cursor=2, has_more=True))
    mod.replies.append(_page(cursor=4, has_more=True))
    mod.replies.append(_page(cursor=7, has_more=False))
    stream = EventStream(call=mod, wait_ms=25_000)

    read = await stream.read()

    assert mod.calls == [
        {"wait_ms": 0},
        {"stream_id": "s1", "after_cursor": 2, "wait_ms": 0},
        {"stream_id": "s1", "after_cursor": 4, "wait_ms": 0},
    ]
    assert read is not None and read.first is True and read.resync is False
    assert stream.cursor == 7


async def test_invalid_cursor_resets_and_resyncs_without_stream_change() -> None:
    mod = _ScriptedEvents()
    mod.replies.append(_page(cursor=2))
    stream = EventStream(call=mod)
    await stream.read()

    mod.replies.append(_page(cursor=9, cursor_status="stream_changed"))
    read = await stream.read()

    assert mod.calls[1] == {"wait_ms": DEFAULT_WAIT_MS, "stream_id": "s1", "after_cursor": 2}
    assert read is not None
    assert read.first is False and read.resync is True and read.stream_changed is False
    assert stream.stream_id == "s1" and stream.cursor == 9


async def test_stream_change_is_reported_and_read_resets() -> None:
    mod = _ScriptedEvents()
    mod.replies.append(_page(cursor=2))
    stream = EventStream(call=mod)
    await stream.read()

    # 换了流：新编号连着读两页积压
    mod.replies.append(_page(stream_id="s2", cursor=3, has_more=True))
    mod.replies.append(_page(stream_id="s2", cursor=5, has_more=False))
    read = await stream.read()

    assert read is not None
    assert read.resync is True and read.stream_changed is True
    assert stream.stream_id == "s2" and stream.cursor == 5

    # 连接重建（forget）后第一次读到不同的编号也算换流；同一条流不算
    stream.forget()
    mod.replies.append(_page(stream_id="s2", cursor=5))
    read = await stream.read()
    assert read is not None and read.first is True and read.stream_changed is False

    stream.forget()
    mod.replies.append(_page(stream_id="s3", cursor=1))
    read = await stream.read()
    assert read is not None and read.first is True and read.stream_changed is True


async def test_stream_changing_mid_backlog_fails_the_read_and_forgot_the_cursor() -> None:
    mod = _ScriptedEvents()
    mod.replies.append(_page(stream_id="s2", cursor=1, has_more=True))
    mod.replies.append(_page(stream_id="s3", cursor=2, has_more=False))
    stream = EventStream(call=mod)

    assert await stream.read() is None
    assert stream.stream_id is None and stream.cursor == 0


async def test_failed_reads_back_off_and_keep_the_cursor() -> None:
    mod = _ScriptedEvents()
    mod.replies.append(_page(cursor=2))
    sleeps, do_sleep = _recorder()
    stream = EventStream(call=mod, sleep=do_sleep)
    await stream.read()

    # 连续失败逐档放慢；读不懂的回复与失败回复都算失败
    mod.replies.append(no_reply("连不上"))
    mod.replies.append(reply_of(tool_result(ok_envelope("不是一页事件的形状"))))
    assert await stream.read() is None
    assert await stream.read() is None
    assert sleeps == [RETRY_DELAYS_S[0], RETRY_DELAYS_S[1]]
    assert stream.stream_id == "s1" and stream.cursor == 2

    # 成功一次后退避归零
    mod.replies.append(_page(cursor=4, events=[{"cursor": 4, "kind": "resumed", "goal_id": 1}]))
    read = await stream.read()
    assert read is not None and read.resync is False
    assert stream.cursor == 4

    mod.replies.append(no_reply("又断了"))
    assert await stream.read() is None
    assert sleeps[-1] == RETRY_DELAYS_S[0]


async def test_fast_empty_read_waits_before_returning() -> None:
    clock = ManualClock()
    mod = _ScriptedEvents(clock=clock)
    sleeps, do_sleep = _recorder()
    stream = EventStream(call=mod, clock=clock, sleep=do_sleep)

    # 首次读取只建立游标，空页不触发补等
    mod.replies.append(_page(cursor=0, events=[]))
    read = await stream.read()
    assert read is not None and read.first is True
    assert sleeps == []

    # 之后的读取（wait_ms>0）空页等了不到下限（服务端没按要求等）：补等下限再返回
    mod.replies.append(_page(cursor=0, events=[]))
    read = await stream.read()
    assert read is not None
    assert sleeps == [EMPTY_READ_FLOOR_S]

    # 往返耗时已经达到下限的空页不再多等
    mod.advance_per_call = EMPTY_READ_FLOOR_S  # 这一次往返耗了下限那么久
    mod.replies.append(_page(cursor=0, events=[]))
    sleeps.clear()
    read = await stream.read()
    assert read is not None
    assert sleeps == []


async def test_topic_is_passed_on_every_read() -> None:
    mod = _ScriptedEvents()
    mod.replies.append(_page(stream_id="chat-1", cursor=1, has_more=True))
    mod.replies.append(_page(stream_id="chat-1", cursor=3, has_more=False))
    stream = EventStream(call=mod, topic="chat", wait_ms=25_000)

    read = await stream.read()

    assert mod.calls[0] == {"wait_ms": 0, "topic": "chat"}
    assert mod.calls[1] == {"stream_id": "chat-1", "after_cursor": 1, "wait_ms": 0, "topic": "chat"}
    assert read is not None and read.page == events_page_of(
        {
            "stream_id": "chat-1",
            "cursor": 1,
            "events": [],
            "has_more": True,
            "cursor_status": "valid",
        }
    )
