"""测试用的假件。每个生产接口只在这里做一份假件。"""

from __future__ import annotations

import asyncio
import base64
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


class ManualClock:
    """可控假单调时钟（秒），供测试显式推进时间而不依赖真实等待。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def advance(self, seconds: float) -> float:
        """把假单调时钟向前推进指定秒数，并返回推进后的时刻。"""
        self.now += float(seconds)
        return self.now

    def __call__(self) -> float:
        return self.now


# 只改记忆、只读分析的能力当场完成，其余在后台推进。
_ASIDE_ABILITIES = frozenset({"maicraft:remember"})

# observe(picture) 回的那张图（假 JPEG 的 base64）：够长，测试据此确认它进了哪个内容块。
PICTURE_BASE64 = base64.b64encode(bytes([0xFF, 0xD8, 0xFF]) + b"minecraft-picture" * 200).decode("ascii")


class FakeMod:
    """MaiCraftMod 的替身：observe / lookup / execute / goal / events 五个工具。

    目标运行按真实接口走：要动手的目标返回 running，测试用 ``finish`` / ``ask`` / ``pause``
    推进它，同时往事件流里追加对应事件；事件流按游标分页，没有新事件时等一小会儿再空手返回。
    ``clock`` 注入假时钟时，长轮询的等待改为推进虚拟时间并让出事件循环，测试里没有真实等待。
    """

    def __init__(self, *, clock: ManualClock | None = None) -> None:
        self._clock = clock
        self.goals: dict[int, dict[str, Any]] = {}
        # 结束了的目标里折起来的试过的办法：默认结果里不给，按路径 /attempts 读回。
        self.folded_attempts: dict[int, list[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self.stream_id = "stream-1"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # 角色所在的存档：reports_world 为 False 时扮演旧版 mod（回复里没有 world 键）
        self.reports_world = False
        # 游戏公屏说话是否当场交出去（关了逐字打字）：True 时 execute 直接回结束与 submitted 细节，
        # False 时照要动手的目标回 running，测试用 finish 推进（打字中途被打断即交不出去）
        self.chat_instant = True
        self.world: dict[str, Any] | None = None
        self.self_view: dict[str, Any] = {
            "position": {"x": 3.6, "y": 64.0, "z": -7.2, "dimension": "minecraft:overworld"},
            "facing": "南",
            "health": 18,
            "food": 15,
            "air": 300,
            "max_air": 300,
            "held": "minecraft:stone_axe",
            "armor": [],
            "effects": [],
            "inventory": [{"item": "minecraft:oak_log", "count": 6}],
            "control": "automation",
            "goal": {"doing": "空闲，没有主任务"},
        }
        self.scene_view: dict[str, Any] = {
            "time": "白天",
            "weather": "晴",
            "biome": "minecraft:forest",
            "summary": "前方：e1（minecraft:cow，6格）",
            "entities": [
                {"id": "e1", "type": "minecraft:cow", "direction": "前方", "distance": 6, "hostile": False},
                {"id": "e2", "type": "minecraft:creeper", "direction": "后方", "distance": 90, "hostile": True},
                {
                    "id": "e3",
                    "type": "minecraft:player",
                    "name": "Steve",
                    "direction": "左侧",
                    "distance": 40,
                    "hostile": False,
                },
            ],
            # 远处不相干的生物，mod 只按种类报只数
            "distant_entities": {"minecraft:bat": 12, "minecraft:squid": 30},
            "facilities": [{"id": "b1", "block": "minecraft:chest", "direction": "左侧", "distance": 4}],
        }
        # observe(picture)：和真实 mod 一样回两个内容块——JSON 文字在前、JPEG 图在后（base64 不进 JSON）
        self.picture_view: dict[str, Any] = {
            "picture": {"width": 1280, "height": 720, "format": "image/jpeg", "labeled": True, "menu_included": False},
            "camera": {"direction": "前方", "compass": "北", "pitch": "平视", "first_person": True},
            "in_picture": [
                {"id": "b1", "block": "minecraft:chest", "at": [214, 560], "labeled": True},
                {"id": "e1", "type": "minecraft:cow", "at": [902, 410], "labeled": True},
                {
                    "id": "e9",
                    "type": "minecraft:item",
                    "at": [930, 600],
                    "labeled": False,
                    "not_labeled_because": "太挤",
                },
            ],
            "summary": "画面里看得见 3 个编号，标了 2 个。",
        }
        self.picture_image = PICTURE_BASE64
        self._next_id = 1
        self._next_timer_id = 1
        self.timers: dict[int, dict[str, Any]] = {}
        self._streams = 1
        self._new_event = asyncio.Event()

    async def invoke(self, name: str, arguments: dict[str, Any]) -> mcp_types.CallToolResult:
        """调用替身的工具，回一个 MCP 结果：回复的 JSON 放在第一个文字块里。"""
        arguments = dict(arguments or {})
        self.calls.append((name, arguments))
        reply = await getattr(self, f"_{name}")(arguments)
        images: list[tuple[str, str]] | None = None
        if name == "observe" and arguments.get("what") == "picture" and reply["ok"]:
            images = [("image/jpeg", self.picture_image)]
        return tool_result(reply, images)

    def calls_to(self, name: str) -> list[dict[str, Any]]:
        return [arguments for called, arguments in self.calls if called == name]

    # 五个工具

    async def _observe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        what = arguments.get("what", "scene")
        if what == "picture":
            return ok_envelope(dict(self.picture_view))
        view = dict(self.self_view if what == "self" else self.scene_view)
        if what == "self" and self.reports_world:
            view["world"] = self.world
        return ok_envelope(view)

    async def _lookup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("topic") == "knowledge":
            doc_id = arguments.get("id")
            if doc_id:
                return ok_envelope({"doc": f"{doc_id} 的格式资料正文（替身）"})
            return ok_envelope({"docs": ["building/house", "building/roofs", "building/styles"]})
        return ok_envelope({"abilities": [{"ability": "maicraft:gather"}, {"ability": "maicraft:remember"}]})

    async def _execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        goal = arguments.get("goal") or {}
        ability = str(goal.get("ability") or "")
        ability = ability if ":" in ability else f"maicraft:{ability}"
        goal_id = self._next_id
        self._next_id += 1
        run: dict[str, Any] = {"goal_id": goal_id, "ability": ability, "state": "running"}
        if goal.get("purpose"):
            run["purpose"] = goal["purpose"]
        if ability in _ASIDE_ABILITIES:
            run["state"] = "finished"
            run["result"] = {"status": "done", "summary": "记住了「家」", "changes": [], "remaining": []}
        elif ability == "maicraft:timer":
            params = goal.get("parameters") if isinstance(goal.get("parameters"), dict) else {}
            op = str(params.get("operation") or "set")
            details: dict[str, Any] = {}
            if op == "set":
                tid = self._next_timer_id
                self._next_timer_id += 1
                rem = float(params.get("after_seconds") or 60)
                note = str(params.get("note") or "")
                self.timers[tid] = {"timer_id": tid, "remaining_seconds": rem, "note": note}
                details["timer_id"] = tid
            elif op == "cancel":
                tid_arg = params.get("timer_id")
                if isinstance(tid_arg, int):
                    self.timers.pop(tid_arg, None)
            details["pending"] = [dict(item) for item in self.timers.values()]
            run["state"] = "finished"
            run["result"] = {
                "status": "done",
                "summary": f"当前挂着 {len(self.timers)} 个计时",
                "changes": [],
                "remaining": [],
                "details": details,
            }
        elif ability.endswith(":chat") and self.chat_instant:
            # 游戏公屏说话当场交出去（关了逐字打字）：细节照真实接口写 submitted 与原话；
            # 以 / 开头是游戏命令、带 target 是对某人说，都不算交进游戏公屏
            message = str((goal.get("parameters") or {}).get("message") or "")
            public = bool(message) and not message.startswith("/") and not goal.get("target")
            run["state"] = "finished"
            run["result"] = {
                "status": "done" if public else "failed",
                "summary": "说了这句话" if public else "没有交进游戏公屏",
                "changes": [],
                "remaining": [],
                "details": {"submitted": public, "message": message},
            }
        self.goals[goal_id] = run
        return ok_envelope(dict(run))

    async def _goal(self, arguments: dict[str, Any]) -> dict[str, Any]:
        operation = arguments.get("operation")
        if operation == "list":
            return ok_envelope({"goals": [dict(run) for run in self.goals.values()]})
        run = self.goals.get(arguments.get("goal_id"))
        if run is None:
            return error_envelope("unknown_id", f"没有编号为 {arguments.get('goal_id')} 的目标")
        if operation == "get" and arguments.get("path"):
            return self._read_folded(run, str(arguments["path"]))
        if operation == "answer":
            run.pop("question", None)
            run["state"] = "running"
            self._append("resumed", run["goal_id"], "收到回答")
        elif operation == "resume":
            run["state"] = "running"
        elif operation == "cancel":
            run["state"] = "finished"
            run["result"] = {"status": "cancelled", "summary": "目标被取消", "changes": [], "remaining": []}
        return ok_envelope(dict(run))

    def _read_folded(self, run: dict[str, Any], path: str) -> dict[str, Any]:
        """按路径读结束了的目标里折起来的一段：和真实接口一样，没结束、路径不对都按参数问题回。"""
        goal_id = run["goal_id"]
        if run.get("state") != "finished":
            return error_envelope("invalid_parameter", f"目标 {goal_id} 还没结束，没有结果可按路径读")
        attempts = self.folded_attempts.get(goal_id)
        if path != "/attempts" or attempts is None:
            return error_envelope("invalid_parameter", f"结果里没有 {path}")
        return ok_envelope(
            {"goal_id": goal_id, "path": path, "type": "list", "value": attempts, "total": len(attempts), "offset": 0}
        )

    async def _events(self, arguments: dict[str, Any]) -> dict[str, Any]:
        expected = arguments.get("stream_id")
        after = int(arguments.get("after_cursor") or 0)
        status = "valid"
        if expected is not None and expected != self.stream_id:
            status, after = "stream_changed", 0
        wait_s = int(arguments.get("wait_ms") or 0) / 1000
        if wait_s and not any(event["cursor"] > after for event in self.events):
            if self._clock is not None:
                # 注入假时钟时直接推进虚拟单调时间并让出事件循环，测试里没有真实等待
                self._clock.advance(wait_s)
                await asyncio.sleep(0)
            else:
                self._new_event.clear()
                # 真实服务端最多等 wait_ms，有新事件立刻返回；测试里最多等 1.5 秒，比读取方的空转下限长。
                with_timeout = min(wait_s, 1.5)
                try:
                    await asyncio.wait_for(self._new_event.wait(), timeout=with_timeout)
                except asyncio.TimeoutError:
                    pass
        page = [event for event in self.events if event["cursor"] > after]
        cursor = page[-1]["cursor"] if page else (self.events[-1]["cursor"] if self.events else 0)
        cursor = max(cursor, after) if status == "valid" else cursor
        data: dict[str, Any] = {
            "stream_id": self.stream_id,
            "cursor": cursor,
            "has_more": False,
            "cursor_status": status,
            "events": page,
        }
        if self.reports_world:
            data["world"] = self.world
        return ok_envelope(data)

    # 测试推进目标

    def _append(
        self, kind: str, goal_id: int, message: str, status: str | None = None, self_brief: str | None = None
    ) -> None:
        event: dict[str, Any] = {"cursor": len(self.events) + 1, "kind": kind, "goal_id": goal_id, "message": message}
        if status:
            event["status"] = status
        if self_brief:
            event["self_brief"] = self_brief
        self.events.append(event)
        self._new_event.set()

    def finish(
        self,
        goal_id: int,
        status: str = "done",
        summary: str = "砍了 5 块原木",
        *,
        self_brief: str | None = None,
        attempts: list[dict[str, Any]] | None = None,
        problem: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """目标结束：结果写进运行表、发 finished 事件。

        ``self_brief`` 是结束那一刻角色的一行自身摘要，和真实接口一样只在事件上；
        ``attempts`` 给了就像真实接口那样折起来：默认结果里只留 ``folded`` 一项，按路径 ``/attempts`` 读回；
        ``problem`` / ``details`` 给了就原样写进结果。
        """
        run = self.goals[goal_id]
        run["state"] = "finished"
        run.pop("question", None)
        run["result"] = {
            "status": status,
            "summary": summary,
            "changes": [{"kind": "item_gained", "what": "minecraft:oak_log", "count": 5}],
            "remaining": [],
        }
        if problem is not None:
            run["result"]["problem"] = problem
        if details is not None:
            run["result"]["details"] = details
        if attempts:
            self.folded_attempts[goal_id] = attempts
            run["result"]["folded"] = [
                {
                    "path": "/attempts",
                    "type": "list",
                    "items": len(attempts),
                    "chars": len(json.dumps(attempts, ensure_ascii=False)),
                }
            ]
        self._append("finished", goal_id, summary, status, self_brief)

    def ask(self, goal_id: int, text: str = "动哪个箱子？") -> None:
        run = self.goals[goal_id]
        run["state"] = "awaiting_answer"
        run["question"] = {
            "reason": "choose_one",
            "text": text,
            "options": [{"id": "b5", "meaning": "门口那个"}, {"id": "b6", "meaning": "屋里那个"}],
        }
        self._append("asked", goal_id, text)

    def pause(self, goal_id: int) -> None:
        self.goals[goal_id]["state"] = "paused"
        self._append("paused", goal_id, "暂停")

    def body_event(self, kind: str, message: str, fields: dict[str, Any] | None = None) -> None:
        """发一条与目标无关的事件；``fields`` 是事件自带的结构化字段。"""
        self._append(kind, -1, message)
        if fields:
            self.events[-1].update(fields)

    def wake_events(self) -> None:
        """唤醒当前挂起的长轮询事件页（不追加新事件，供测试在推进假时钟后立刻触发事件页巡检）。"""
        self._new_event.set()

    def die(self) -> int:
        """角色死了：和真实 mod 一样先发 character_died，再挂一条死亡恢复决策（编号是负数）并发 asked。"""
        decision_id = -1 - sum(1 for goal_id in self.goals if goal_id < 0)
        self._append("character_died", -1, "角色死了，等重生；插着的任务已结束，主任务停在原地，重生后接着做")
        question = {
            "reason": "choose_one",
            "text": "角色死了。接下来怎么办？",
            "options": [
                {"id": "respawn", "meaning": "发原版重生请求，回出生点或床，任务原地接着做"},
                {"id": "cancel_goal", "meaning": "取消当前目标，死亡屏幕留给人处理"},
            ],
        }
        self.goals[decision_id] = {
            "goal_id": decision_id,
            "ability": "death_recovery",
            "state": "awaiting_answer",
            "question": question,
        }
        self._append("asked", decision_id, question["text"])
        return decision_id

    def enter_world(self, world_id: str, name: str = "") -> None:
        """进了某个存档（mod 认出了世界）：事件流换新编号，每页与读自己都带上这个存档。"""
        self.reports_world = True
        self.world = {"id": world_id, "name": name, "source": "world_id_file"}
        self._streams += 1
        self.stream_id = f"stream-{self._streams}"
        self._new_event.set()

    def leave_world(self) -> None:
        """退回主菜单：world 为 null，事件流不换。"""
        self.reports_world = True
        self.world = None
        self._new_event.set()

    def change_world(self) -> None:
        """换了世界：事件流换新编号，旧目标都不在了。"""
        self.stream_id = "stream-2"
        self.goals.clear()
        self.events.clear()
        # 每个世界的目标编号各自从 1 数起，可能撞上上一个世界的编号。
        self._next_id = 1
        self._new_event.set()
