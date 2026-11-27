"""MaiCraftMod 五个工具的注册名、统一回复格式与目标运行状态。

mod 的每个工具都回一份 JSON：成功是 ``{ok: true, data, notes?, next?}``，失败是
``{ok: false, error: {code, message, fields?}}``。这份 JSON 放在结果的第一个文字块里。
需要后台推进的目标由 ``execute`` 下达后返回 ``goal_id`` 与 ``state``；目标的处境
（进行中 / 等回答 / 暂停 / 结束）与结束后的结果都在 ``goal(get)`` 里，变化经 ``events``
推送。这些格式只在本模块解读一次，其余部分按这里给出的含义使用，不各自猜字段。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import mcp_types

# mod 的五个 MCP 工具原名；给模型用的是前四个，events 只由程序自己读。
OBSERVE = "observe"
LOOKUP = "lookup"
EXECUTE = "execute"
GOAL = "goal"
EVENTS = "events"
TOOLS_FOR_MODEL = (OBSERVE, LOOKUP, EXECUTE, GOAL)

# 记地点能力在目标运行里的 ID：它当场改世界记忆，调用方据此作废速览里记住的地点的缓存。
REMEMBER = "maicraft:remember"

# 目标运行的处境（execute 与 goal(get) 里的 state）
RUNNING = "running"
AWAITING_ANSWER = "awaiting_answer"
PAUSED = "paused"
FINISHED = "finished"

# 没拿到 mod 的回复（连不上、超时、读不懂）时的错误码。
NO_REPLY = "no_reply"

McpCall = Callable[[str, dict[str, Any]], Awaitable["ModReply"]]


@dataclass(slots=True)
class ModImage:
    """结果里附带的一张图片：格式与 base64 数据。"""

    mime_type: str
    data: str


@dataclass(slots=True)
class ModReply:
    """一次 mod 工具调用的回复：成败、数据与错误，原样保留整份回复供模型阅读。"""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    images: list[ModImage] = field(default_factory=list)


def no_reply(message: str) -> ModReply:
    """没拿到 mod 的回复（连不上、超时、读不懂）时的统一回复。"""
    return ModReply(ok=False, error_code=NO_REPLY, error_message=message, raw={"ok": False, "error": message})


def reply_of(result: mcp_types.CallToolResult) -> ModReply:
    """把工具调用的结果读成统一回复；拿不到 mod 自己的回复时按 no_reply 如实记下。

    第一个文字块的正文是一段 JSON，就是回复的外层结构：ok 为真取 data，为假取
    error.code 与 error.message。type == "image" 的内容块收进 images。
    """
    images: list[ModImage] = []
    text: str | None = None
    for block in result.content:
        if block.type == "image":
            images.append(ModImage(mime_type=block.mime_type, data=block.data))
        elif text is None and block.type == "text":
            text = block.text
    if text is None:
        return no_reply("结果里没有文字块")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return no_reply(f"回复不是 JSON：{text}")
    if not isinstance(raw, dict) or "ok" not in raw:
        return no_reply(f"回复里没有 ok 键：{text}")
    if raw.get("ok") is True:
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        return ModReply(ok=True, data=data, raw=raw, images=images)
    error = raw.get("error") if isinstance(raw.get("error"), dict) else {}
    return ModReply(
        ok=False,
        error_code=str(error.get("code") or ""),
        error_message=str(error.get("message") or ""),
        raw=raw,
        images=images,
    )


@dataclass(slots=True)
class GoalRun:
    """一个目标运行此刻的样子（``goal(get)`` / ``execute`` 的 data）。"""

    goal_id: int
    ability: str
    purpose: str
    state: str
    question: dict[str, Any] | None
    result: dict[str, Any] | None
    doing: str
    data: dict[str, Any]

    @property
    def finished(self) -> bool:
        return self.state == FINISHED

    @property
    def result_status(self) -> str:
        """结束后的达成情况：done / partial / failed / cancelled；没结束时为空串。"""
        return str((self.result or {}).get("status") or "")

    @property
    def summary(self) -> str:
        """一句话：结束了就是结果的结论，在等回答就是问题，其余是此刻在做什么。"""
        if self.result:
            return str(self.result.get("summary") or "")
        if self.question:
            return str(self.question.get("text") or "")
        return self.doing


def goal_run_of(data: dict[str, Any]) -> GoalRun | None:
    """从 execute / goal(get) 的 data 读出目标运行；不是目标运行的形状时为 None。"""
    goal_id = data.get("goal_id")
    state = data.get("state")
    if not isinstance(goal_id, int) or isinstance(goal_id, bool) or not isinstance(state, str):
        return None
    question = data.get("question") if isinstance(data.get("question"), dict) else None
    result = data.get("result") if isinstance(data.get("result"), dict) else None
    return GoalRun(
        goal_id=goal_id,
        ability=str(data.get("ability") or ""),
        purpose=str(data.get("purpose") or ""),
        state=state,
        question=question,
        result=result,
        doing=str(data.get("doing") or ""),
        data=data,
    )


@dataclass(slots=True)
class EventsPage:
    """一页事件：流编号、读到哪、这一页的事件、后面还有没有、游标是否还有效，以及角色所在的存档。

    ``world_fields`` 原样留着这一页的 ``world`` 键（``{"world": ...}``）；mod 不报存档时为空字典，
    交给 ``WorldIdentity.absorb`` 区分"旧版 mod"与"不在世界里"。
    """

    stream_id: str
    cursor: int
    events: list[dict[str, Any]]
    has_more: bool
    cursor_status: str
    world_fields: dict[str, Any] = field(default_factory=dict)


def events_page_of(data: dict[str, Any]) -> EventsPage | None:
    """读 events 的 data；形状不对时为 None。"""
    stream_id = data.get("stream_id")
    cursor = data.get("cursor")
    events = data.get("events")
    if not isinstance(stream_id, str) or not isinstance(cursor, int) or not isinstance(events, list):
        return None
    return EventsPage(
        stream_id=stream_id,
        cursor=cursor,
        events=[event for event in events if isinstance(event, dict)],
        has_more=bool(data.get("has_more")),
        cursor_status=str(data.get("cursor_status") or "valid"),
        world_fields={"world": data.get("world")} if "world" in data else {},
    )


__all__ = [
    "AWAITING_ANSWER",
    "EVENTS",
    "EXECUTE",
    "EventsPage",
    "FINISHED",
    "GOAL",
    "GoalRun",
    "LOOKUP",
    "McpCall",
    "ModImage",
    "ModReply",
    "NO_REPLY",
    "OBSERVE",
    "PAUSED",
    "REMEMBER",
    "RUNNING",
    "TOOLS_FOR_MODEL",
    "events_page_of",
    "goal_run_of",
    "no_reply",
    "reply_of",
]
