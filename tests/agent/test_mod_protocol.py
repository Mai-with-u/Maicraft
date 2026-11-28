"""协议层直测：统一回复的读法（成功、业务拒绝、没有回复、图片块）与目标运行、事件页的读法。"""

from __future__ import annotations

import json
from typing import Any

import mcp_types

from maicraft.agent.mod_protocol import (
    GOAL,
    NO_REPLY,
    EventsPage,
    GoalRun,
    ModImage,
    events_page_of,
    goal_run_of,
    no_reply,
    reply_of,
)
from tests.fakes import error_envelope, ok_envelope, tool_result


def test_successful_reply_reads_data_and_raw() -> None:
    result = tool_result(ok_envelope({"goal_id": 7, "state": "running"}, notes=["提示"]))
    reply = reply_of(result)

    assert reply.ok is True
    assert reply.data == {"goal_id": 7, "state": "running"}
    assert reply.error_code == "" and reply.error_message == ""
    assert reply.raw == {"ok": True, "data": {"goal_id": 7, "state": "running"}, "notes": ["提示"]}
    assert reply.images == []


def test_business_rejection_reads_error_code_and_message() -> None:
    result = tool_result(error_envelope("unknown_id", "没有这个目标"))

    reply = reply_of(result)

    assert reply.ok is False
    assert reply.error_code == "unknown_id"
    assert reply.error_message == "没有这个目标"
    assert reply.raw["ok"] is False


def test_unreadable_reply_is_reported_as_no_reply() -> None:
    # 文字块不是 JSON
    not_json = mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="连接断了")], is_error=True)
    reply = reply_of(not_json)
    assert reply.ok is False and reply.error_code == NO_REPLY

    # 是 JSON 但没有 ok 键
    no_ok = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps({"data": 1}))], is_error=False
    )
    assert reply_of(no_ok).error_code == NO_REPLY

    # 没有文字块
    assert reply_of(mcp_types.CallToolResult(content=[], is_error=True)).error_code == NO_REPLY

    # no_reply 构造函数本身
    made = no_reply("超时了")
    assert made.ok is False and made.error_code == NO_REPLY and made.error_message == "超时了"


def test_not_ready_reply_reads_as_a_failed_reply() -> None:
    """mod 已声明但未接线的工具回 ``{"success": false, "error": {...}}``（没有 ok 键）：
    按失败的回复解读，错误码与说明取自 error，不当成没有回复。"""
    not_ready = {
        "success": False,
        "error": {
            "code": "not_ready",
            "message": "工具 observe 已声明，但它的真实行为还没有接入。",
            "retryable": False,
        },
    }
    result = tool_result(not_ready)

    reply = reply_of(result)

    assert reply.ok is False
    assert reply.error_code == "not_ready"
    assert "还没有接入" in reply.error_message
    assert reply.raw == not_ready


def test_image_blocks_are_collected() -> None:
    result = tool_result(ok_envelope({"picture": {}}), images=[("image/jpeg", "AAAA"), ("image/png", "BBBB")])
    reply = reply_of(result)

    assert reply.ok is True
    assert reply.images == [
        ModImage(mime_type="image/jpeg", data="AAAA"),
        ModImage(mime_type="image/png", data="BBBB"),
    ]


def test_goal_run_reading() -> None:
    data: dict[str, Any] = {
        "goal_id": 3,
        "ability": "maicraft:gather",
        "purpose": "砍柴",
        "state": "awaiting_answer",
        "question": {"text": "动哪个箱子？", "options": [{"id": "b5"}]},
        "doing": "等指示",
    }
    run = goal_run_of(data)
    assert run == GoalRun(
        goal_id=3,
        ability="maicraft:gather",
        purpose="砍柴",
        state="awaiting_answer",
        question={"text": "动哪个箱子？", "options": [{"id": "b5"}]},
        result=None,
        doing="等指示",
        data=data,
    )
    assert not run.finished
    assert run.result_status == ""
    assert run.summary == "动哪个箱子？", "在等回答时摘要是问题"

    finished = goal_run_of(
        {
            "goal_id": 4,
            "ability": "maicraft:gather",
            "state": "finished",
            "result": {"status": "partial", "summary": "砍了 5 块，还差 3 块"},
        }
    )
    assert finished.finished and finished.result_status == "partial"
    assert finished.summary == "砍了 5 块，还差 3 块"

    running = goal_run_of({"goal_id": 5, "ability": "maicraft:gather", "state": "running", "doing": "走向树"})
    assert running.summary == "走向树"

    assert goal_run_of({"goal_id": "3", "state": "running"}) is None
    assert goal_run_of({"state": "running"}) is None
    assert goal_run_of({"goal_id": True, "state": "running"}) is None


def test_events_page_reading_with_and_without_world() -> None:
    data = {
        "stream_id": "stream-1",
        "cursor": 9,
        "events": [{"cursor": 9, "kind": "finished", "goal_id": 7}, "不是对象的条目"],
        "has_more": True,
        "cursor_status": "stream_changed",
        "world": {"id": "aaa", "name": "老家"},
    }
    page = events_page_of(data)
    assert page == EventsPage(
        stream_id="stream-1",
        cursor=9,
        events=[{"cursor": 9, "kind": "finished", "goal_id": 7}],
        has_more=True,
        cursor_status="stream_changed",
        world_fields={"world": {"id": "aaa", "name": "老家"}},
    )

    without_world = events_page_of({"stream_id": "s", "cursor": 0, "events": []})
    assert without_world is not None
    assert without_world.world_fields == {}
    assert without_world.has_more is False
    assert without_world.cursor_status == "valid", "mod 没写游标状态时按有效处理"

    assert events_page_of({"cursor": 0, "events": []}) is None
    assert events_page_of({"stream_id": 1, "cursor": 0, "events": []}) is None


def test_tool_name_constants() -> None:
    from maicraft.agent.mod_protocol import EVENTS, EXECUTE, LOOKUP, OBSERVE, TOOLS_FOR_MODEL

    assert TOOLS_FOR_MODEL == (OBSERVE, LOOKUP, EXECUTE, GOAL)
    assert EVENTS == "events"
