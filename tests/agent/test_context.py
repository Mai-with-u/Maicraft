"""上下文收起直测：编号存取、超长收起占位、较早工具结果换占位与原列表不被改动。"""

from __future__ import annotations

from typing import Any

from maicraft.agent.context import ContextStore, shorten_tool_result, trim_tool_messages


def test_store_keep_and_get() -> None:
    store = ContextStore()
    h1 = store.keep("observe", {"target": "self"}, "看见一片草原", call_id="c1")
    h2 = store.keep("execute", {"action": "dig"}, "挖到了石头", call_id="c2")
    assert (h1, h2) == ("r1", "r2")
    assert len(store) == 2
    assert store.handle_for("c1") == "r1"
    assert store.handle_for("unknown") is None

    kept = store.get("r2")
    assert kept is not None
    assert kept.tool_name == "execute"
    assert kept.arguments == {"action": "dig"}
    assert kept.text == "挖到了石头"
    assert kept.call_id == "c2"
    assert store.get("r99") is None

    store.clear()
    assert len(store) == 0
    assert store.get("r2") is None
    assert store.keep("observe", {}, "新的一轮", call_id="c3") == "r1"


def test_shorten_keeps_short_result() -> None:
    store = ContextStore()
    result = shorten_tool_result(
        "短结果",
        max_result_chars=10,
        store=store,
        tool_name="observe",
        arguments={},
        call_id="c1",
    )
    assert result == "短结果"
    assert len(store) == 0


def test_shorten_stores_long_result() -> None:
    store = ContextStore()
    long_text = "字" * 30
    result = shorten_tool_result(
        long_text,
        max_result_chars=10,
        store=store,
        tool_name="lookup",
        arguments={"target": "block"},
        call_id="c1",
    )
    assert result.startswith("字" * 10)
    assert '……（全文 30 字已收起，用 recall("r1") 取回）' in result
    assert len(store) == 1
    assert store.handle_for("c1") == "r1"


def _message(role: str, content: str, **extra: Any) -> dict[str, Any]:
    return {"role": role, "content": content, **extra}


def test_trim_keeps_recent_and_replaces_older() -> None:
    store = ContextStore()
    store.keep("observe", {}, "旧结果一", call_id="c1")
    store.keep("execute", {}, "旧结果二", call_id="c2")
    messages = [
        _message("user", "去挖矿"),
        _message("assistant", None, tool_calls=[{"id": "c1"}]),
        _message("tool", "旧结果一", tool_call_id="c1"),
        _message("assistant", None, tool_calls=[{"id": "c2"}]),
        _message("tool", "旧结果二", tool_call_id="c2"),
        _message("tool", "最近的结果", tool_call_id="c3"),
        _message("assistant", "挖好了"),
    ]
    trimmed = trim_tool_messages(messages, store, keep_recent=1)
    assert [m["content"] for m in trimmed] == [
        "去挖矿",
        None,
        '[较早的结果已收起：observe，要原文用 recall("r1") 取回]',
        None,
        '[较早的结果已收起：execute，要原文用 recall("r2") 取回]',
        "最近的结果",
        "挖好了",
    ]
    # 原列表不动
    assert messages[2]["content"] == "旧结果一"
    assert messages[4]["content"] == "旧结果二"
    # 占位消息仍带着 tool_call_id
    assert trimmed[2]["tool_call_id"] == "c1"


def test_trim_without_store_entry() -> None:
    store = ContextStore()
    messages = [
        _message("tool", "很久以前的结果", tool_call_id="unknown-call"),
    ]
    trimmed = trim_tool_messages(messages, store, keep_recent=0)
    assert trimmed[0]["content"] == "[较早的结果已收起]"


def test_trim_keep_recent_covers_all() -> None:
    store = ContextStore()
    messages = [
        _message("tool", "结果一", tool_call_id="c1"),
        _message("tool", "结果二", tool_call_id="c2"),
    ]
    trimmed = trim_tool_messages(messages, store, keep_recent=5)
    assert [m["content"] for m in trimmed] == ["结果一", "结果二"]
