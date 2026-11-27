"""测试用的假件。每个生产接口只在这里做一份假件。"""

from __future__ import annotations

import json
from typing import Any

import mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

TOOL_NAMES = ("observe", "lookup", "execute", "goal", "events")
SKILL_URI = "skill://survival/survival-opening/SKILL.md"
SKILL_TEXT = "---\nname: survival-opening\n---\n\n# 生存开局\n"


def ok_envelope(data: dict[str, Any] | None = None, notes: list[str] | None = None) -> dict[str, Any]:
    """MaiCraftMod 成功回复的外层结构。"""
    envelope: dict[str, Any] = {"ok": True, "data": data or {}}
    if notes:
        envelope["notes"] = notes
    return envelope


def error_envelope(code: str, message: str) -> dict[str, Any]:
    """MaiCraftMod 失败回复的外层结构。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def tool_result(envelope: dict[str, Any], images: list[tuple[str, str]] | None = None) -> mcp_types.CallToolResult:
    """按 MaiCraftMod 的做法造一个工具调用结果。

    回复的 JSON 放在第一个文字块里；ok 为假时标成工具报错。images 每项是（图片格式，base64 数据）。
    """
    content: list[Any] = [mcp_types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))]
    content.extend(mcp_types.ImageContent(type="image", data=data, mime_type=mime) for mime, data in images or [])
    return mcp_types.CallToolResult(content=content, is_error=not envelope.get("ok", False))


def _tool(name: str, description: str) -> mcp_types.Tool:
    return mcp_types.Tool(name=name, description=description, input_schema={"type": "object"})


class FakeModClient:
    """顶替 fastmcp.Client 的假件：ModClient 用到的接口它都有，行为按剧本设置。

    replies 是每个工具固定回的结果；enter_error、call_error 用来模拟连不上和中途断开；
    calls 记下每次调用（工具名、参数、超时秒数），供断言。
    """

    def __init__(self) -> None:
        self.is_connected = False
        self.enter_error: Exception | None = None
        self.call_error: Exception | None = None
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []
        self.replies: dict[str, mcp_types.CallToolResult] = {}
        self.resource_texts: dict[str, str] = {}
        self.tools = [_tool(name, f"{name} 的说明") for name in TOOL_NAMES]
        self.resources = [mcp_types.Resource(uri=SKILL_URI, name="survival-opening", mime_type="text/markdown")]
        self.server_info = mcp_types.Implementation(name="fake-maicraft", version="0")
        self.protocol_version = "2025-11-25"
        self.instructions = "假 mod 的说明"
        self.closed = False

    def reply(self, name: str, envelope: dict[str, Any], images: list[tuple[str, str]] | None = None) -> None:
        self.replies[name] = tool_result(envelope, images)

    async def __aenter__(self) -> "FakeModClient":
        if self.enter_error is not None:
            raise self.enter_error
        self.is_connected = True
        return self

    async def close(self) -> None:
        self.is_connected = False
        self.closed = True

    async def ping(self) -> bool:
        return True

    async def list_tools(self) -> list[mcp_types.Tool]:
        return list(self.tools)

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_handler: Any = None,
        timeout: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> mcp_types.CallToolResult:
        self.calls.append((name, arguments, timeout))
        if self.call_error is not None:
            raise self.call_error
        if name not in self.replies:
            raise RuntimeError(f"假 mod 没有为工具 {name} 准备回复")
        return self.replies[name]

    async def list_resources(self) -> list[mcp_types.Resource]:
        return list(self.resources)

    async def read_resource_mcp(self, uri: str, meta: dict[str, Any] | None = None) -> mcp_types.ReadResourceResult:
        uri_text = str(uri)
        return mcp_types.ReadResourceResult(
            contents=[mcp_types.TextResourceContents(uri=uri_text, text=self.resource_texts[uri_text])]
        )


def fake_mod_server() -> FastMCP:
    """进程内的小型 MCP 服务端，只用来验证我们对 fastmcp 接口的用法是否正确。

    它的五个工具各带一个写明参数的版本，回复的格式与真 mod 一致。agent 层的测试用
    FakeModClient 就够了，不需要它。
    """
    server = FastMCP("fake-maicraft")

    @server.tool(name="observe")
    async def observe(what: str = "scene", radius: int | None = None) -> Any:
        """看世界和自己"""
        if what == "picture":
            return [
                mcp_types.TextContent(type="text", text=json.dumps(ok_envelope({"picture": {"width": 1280}}))),
                mcp_types.ImageContent(type="image", data="AAAA", mime_type="image/jpeg"),
            ]
        return json.dumps(ok_envelope({"what": what, "radius": radius}), ensure_ascii=False)

    @server.tool(name="lookup")
    async def lookup(topic: str = "abilities", id: str | None = None) -> str:
        """查资料"""
        if id == "maicraft:nope":
            raise ToolError(json.dumps(error_envelope("unknown_id", "没有这个能力"), ensure_ascii=False))
        return json.dumps(ok_envelope({"topic": topic, "id": id}), ensure_ascii=False)

    @server.tool(name="execute")
    async def execute(goal: dict[str, Any], dry_run: bool = False) -> str:
        """下达一个目标"""
        return json.dumps(
            {"ok": True, "data": {"goal_id": 7, "state": "running"}, "next": {"tool": "events"}}, ensure_ascii=False
        )

    @server.tool(name="goal")
    async def goal(operation: str = "list", goal_id: int | None = None, answer: str | None = None) -> str:
        """查看与管理目标"""
        return json.dumps(
            ok_envelope({"goal_id": goal_id, "state": "awaiting_answer", "question": {"text": "要做吗？"}}),
            ensure_ascii=False,
        )

    @server.tool(name="events")
    async def events(topic: str = "self", wait_ms: int = 0) -> str:
        """读角色身上发生的事"""
        return json.dumps(
            ok_envelope(
                {
                    "stream_id": "stream-1",
                    "cursor": 3,
                    "has_more": False,
                    "cursor_status": "valid",
                    "events": [{"cursor": 3, "kind": "finished", "goal_id": 7, "message": "做完了"}],
                    "world": None,
                }
            ),
            ensure_ascii=False,
        )

    @server.resource(SKILL_URI, name="survival-opening", description="生存开局", mime_type="text/markdown")
    def skill() -> str:
        return SKILL_TEXT

    return server
