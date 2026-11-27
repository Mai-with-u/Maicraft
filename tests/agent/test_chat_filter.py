"""公屏筛选直测：待唤醒分流、合批、冷却、最近聊天记录与事件字段读取，时间用注入的假时钟推进。"""

from __future__ import annotations

from typing import Any

from maicraft.agent.chat_filter import ChatFilter, ChatMessage


class FakeClock:
    """手动推进的毫秒假时钟。"""

    def __init__(self, now_ms: int = 0) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, delta_ms: int) -> None:
        self.now_ms += delta_ms


def make_filter(clock: FakeClock, **overrides: Any) -> ChatFilter:
    kwargs: dict[str, Any] = {"names": ("Mai", "小麦"), "clock_ms": clock}
    kwargs.update(overrides)
    return ChatFilter(**kwargs)


def player(cursor: int, sender: str, text: str, **overrides: Any) -> ChatMessage:
    return ChatMessage(cursor=cursor, kind="player", sender=sender, text=text, **overrides)


# ---------------------------------------------------------------------------
# 分流：私聊与点名进待唤醒，其余不进
# ---------------------------------------------------------------------------


def test_private_and_mention_go_to_pending() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "悄悄话", private=True))
    chat_filter.add(player(2, "Steve", "Mai 你在哪"))
    chat_filter.add(player(3, "Steve", "小麦过来一下"))

    assert chat_filter.next_wake_delay_ms() == 2000
    clock.advance(2000)
    assert chat_filter.take_due() == [
        player(1, "Alex", "悄悄话", private=True),
        player(2, "Steve", "Mai 你在哪"),
        player(3, "Steve", "小麦过来一下"),
    ]


def test_plain_chat_and_system_message_do_not_wake() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "大家好"))
    chat_filter.add(ChatMessage(cursor=2, kind="system", text="服务器十分钟后重启"))
    chat_filter.add(player(3, "Bob", "谁要组队"))

    assert chat_filter.next_wake_delay_ms() is None
    assert chat_filter.take_due() == []


def test_mention_match_is_case_insensitive_on_text() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock, names=("Mai",))
    chat_filter.add(player(1, "Alex", "mai 帮个忙"))

    assert chat_filter.next_wake_delay_ms() == 2000


# ---------------------------------------------------------------------------
# 合批：窗口、条数上限、私聊在前、同人折叠
# ---------------------------------------------------------------------------


def test_messages_within_window_form_one_batch_and_window_opens_at_first_message() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "Mai 在吗"))
    clock.advance(1500)
    chat_filter.add(player(2, "Bob", "来打怪 Mai"))

    # 窗口从第一条消息起算，第二条在 1500ms 时加入不重置到点时刻
    assert chat_filter.next_wake_delay_ms() == 500
    clock.advance(500)
    assert chat_filter.take_due() == [player(1, "Alex", "Mai 在吗"), player(2, "Bob", "来打怪 Mai")]


def test_private_messages_come_before_mentions_in_a_batch() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "Mai 看"))
    chat_filter.add(player(2, "Bob", "私聊内容", private=True))
    chat_filter.add(player(3, "Cara", "Mai 再看"))
    chat_filter.add(player(4, "Dan", "私聊二", private=True))
    clock.advance(2000)

    assert chat_filter.take_due() == [
        player(2, "Bob", "私聊内容", private=True),
        player(4, "Dan", "私聊二", private=True),
        player(1, "Alex", "Mai 看"),
        player(3, "Cara", "Mai 再看"),
    ]


def test_batch_item_limit_drops_excess() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock, max_batch_items=2)
    chat_filter.add(player(1, "Alex", "Mai 一"))
    chat_filter.add(player(2, "Bob", "Mai 二"))
    chat_filter.add(player(3, "Cara", "Mai 三"))
    clock.advance(2000)

    assert chat_filter.take_due() == [player(1, "Alex", "Mai 一"), player(2, "Bob", "Mai 二")]


def test_same_sender_same_text_folds_with_repeat_count() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "Mai 来"))
    chat_filter.add(player(2, "Alex", "Mai 来"))
    chat_filter.add(player(3, "Alex", "Mai 来"))
    clock.advance(2000)

    due = chat_filter.take_due()
    assert len(due) == 1
    assert due[0].text == "Mai 来"
    assert due[0].repeat == 3


def test_same_sender_distinct_content_capped() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock, max_distinct_per_sender=2)
    chat_filter.add(player(1, "Alex", "Mai 第一句"))
    chat_filter.add(player(2, "Alex", "Mai 第二句"))
    chat_filter.add(player(3, "Alex", "Mai 第三句"))
    clock.advance(2000)

    due = chat_filter.take_due()
    assert [item.text for item in due] == ["Mai 第一句", "Mai 第二句"]


def test_not_due_batch_stays() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "Mai 在吗"))
    clock.advance(1000)

    assert chat_filter.take_due() == []
    assert chat_filter.next_wake_delay_ms() == 1000
    clock.advance(1000)
    assert len(chat_filter.take_due()) == 1


# ---------------------------------------------------------------------------
# 冷却：短时间连续唤醒后到点时刻被推后
# ---------------------------------------------------------------------------


def test_cooldown_delays_due_after_frequent_wakes() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    # 第一次唤醒
    chat_filter.add(player(1, "Alex", "Mai 一"))
    clock.advance(2000)
    chat_filter.take_due()
    # 第二次唤醒
    chat_filter.add(player(2, "Bob", "Mai 二"))
    clock.advance(2000)
    chat_filter.take_due()
    # 第三次唤醒达到 wake_cooldown_after=3，之后进入 6000ms 冷却
    chat_filter.add(player(3, "Cara", "Mai 三"))
    clock.advance(2000)
    chat_filter.take_due()
    # 冷却期内新消息攒批，到点时刻至少是上次唤醒 + 冷却时长
    chat_filter.add(player(4, "Dan", "Mai 四"))
    clock.advance(2000)
    # 冷却到 12000ms（上次唤醒在 6000ms），当前在 8000ms，还差 4000ms
    assert chat_filter.take_due() == []
    assert chat_filter.next_wake_delay_ms() == 4000
    clock.advance(4000)
    assert [item.text for item in chat_filter.take_due()] == ["Mai 四"]


def test_cooldown_expires_after_long_silence() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    for i in range(3):
        chat_filter.add(player(i, f"P{i}", f"Mai 第{i}轮"))
        clock.advance(2000)
        chat_filter.take_due()
    # 静默超过两倍冷却期后冷却解除，下一条消息按正常窗口到点
    clock.advance(20000)
    chat_filter.add(player(10, "Alex", "Mai 恢复了"))
    assert chat_filter.next_wake_delay_ms() == 2000


# ---------------------------------------------------------------------------
# 最近聊天记录
# ---------------------------------------------------------------------------


def test_recent_keeps_all_kinds_and_is_bounded() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock, max_recent=3)
    chat_filter.add(player(1, "Alex", "大家好"))
    chat_filter.add(ChatMessage(cursor=2, kind="system", text="服务器重启"))
    chat_filter.add(player(3, "Bob", "Mai 你好"))
    chat_filter.add(player(4, "Cara", "再来一条"))

    recent = chat_filter.recent()
    assert [item.cursor for item in recent] == [2, 3, 4]
    assert recent[0].kind == "system"


def test_recent_folds_adjacent_same_sender_same_text() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "刷屏啦"))
    chat_filter.add(player(2, "Alex", "刷屏啦"))

    recent = chat_filter.recent()
    assert len(recent) == 1
    assert recent[0].repeat == 2


def test_clear_empties_everything() -> None:
    clock = FakeClock()
    chat_filter = make_filter(clock)
    chat_filter.add(player(1, "Alex", "Mai 在吗"))
    chat_filter.add(player(2, "Bob", "闲聊"))
    chat_filter.clear()

    assert chat_filter.next_wake_delay_ms() is None
    assert chat_filter.take_due() == []
    assert chat_filter.recent() == []


# ---------------------------------------------------------------------------
# from_event 读取事件字段
# ---------------------------------------------------------------------------


def test_from_event_reads_all_fields() -> None:
    event = {
        "cursor": 7,
        "kind": "player",
        "sender": "Alex",
        "sender_id": "uuid-1",
        "sender_online": True,
        "sender_distance": 12.5,
        "private": True,
        "text": "过来一下",
        "mentions": [{"name": "Mai", "type": "minecraft:player", "uuid": "uuid-2"}],
    }
    message = ChatMessage.from_event(event)
    assert message == ChatMessage(
        cursor=7,
        kind="player",
        sender="Alex",
        sender_id="uuid-1",
        sender_online=True,
        sender_distance=12.5,
        private=True,
        text="过来一下",
        mentions=("Mai",),
    )


def test_from_event_defaults_optional_fields() -> None:
    message = ChatMessage.from_event({"cursor": 1, "kind": "system", "text": "公告"})
    assert message.sender == ""
    assert message.sender_online is None
    assert message.sender_distance is None
    assert message.private is False
    assert message.mentions == ()
    assert message.repeat == 1


def test_add_names_extends_wake_names_at_runtime() -> None:
    from maicraft.agent.chat_filter import ChatFilter, ChatMessage

    now_ms = [0]
    filter_ = ChatFilter(names=[], clock_ms=lambda: int(now_ms[0]))
    filter_.add(ChatMessage(cursor=1, kind="player", sender="Steve", text="Mai 过来一下"))
    now_ms[0] += 5000
    assert filter_.take_due() == []  # 还没认名字，不叫醒
    filter_.add_names(["Mai", "Mai", " "])
    filter_.add(ChatMessage(cursor=2, kind="player", sender="Steve", text="Mai 过来一下"))
    now_ms[0] += 5000
    due = filter_.take_due()
    assert [message.text for message in due] == ["Mai 过来一下"]
    # 重复的名字去重，不改变判定
    filter_.add_names(["Mai"])
    filter_.add(ChatMessage(cursor=3, kind="player", sender="Steve", text="Mai 在吗"))
    now_ms[0] += 5000
    assert len(filter_.take_due()) == 1
