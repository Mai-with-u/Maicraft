"""主循环直测：连上就跑一轮、事件与公屏叫醒、失败退避、空闲叫醒、换存档、断线重连。

时间用 ManualClock，sleep 用推进假时钟并让出事件循环的替身；等待靠让出事件循环推进，
没有真实时间的等待。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from maicraft.agent.agent import Agent
from maicraft.agent.mod_protocol import reply_of
from maicraft.agent.notes import Notes
from maicraft.config import MaicraftConfig
from maicraft.mod_client import ModUnavailable
from tests.fakes import FakeMod, FakeModEndpoint, ManualClock, ScriptedLLM, llm_failure, llm_text, llm_tool_call

# 一句超过拆分阈值的公屏发言（100 多字）
LONG_CHAT_TEXT = (
    "今天我们先去挖矿。然后去下界找堡垒。最后回家合成装备。"
    "路上小心苦力怕。洞口记得先插上火把。记得带够食物。"
    "到了下界先搭传送门。回来的时候带几组黑曜石。"
    "家里的箱子整理一下。炉子里的铁烧好了记得拿出来。"
)


def make_config(**agent_overrides: Any) -> MaicraftConfig:
    config = MaicraftConfig()
    config.mod.events_wait_ms = 20
    config.agent.idle_replan_minutes = 0
    config.chat.mention_names = ["Mai"]
    for key, value in agent_overrides.items():
        setattr(config.agent, key, value)
    return config


class _NoMemory:
    """什么都不做的记忆服务替身。"""

    async def context(self) -> Any:
        class _Empty:
            stable_brief = ""
            live_context = ""

        return _Empty()

    async def report_event(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def recall(self, query: str) -> list[str]:
        return []


class Harness:
    """Agent 与假 mod、假模型、假时钟的组装：start 之后靠让出事件循环推进。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        llm: ScriptedLLM | None = None,
        mod: FakeMod | None = None,
        config: MaicraftConfig | None = None,
        call: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self.clock = ManualClock()
        self.mod = mod or FakeMod(clock=self.clock)
        self.llm = llm or ScriptedLLM()
        self.config = config or make_config()
        self.endpoint = FakeModEndpoint(self.mod)
        self.notes = Notes(tmp_path / "notes")
        if call is None:

            async def call(name: str, arguments: dict[str, Any]) -> Any:
                return reply_of(await self.mod.invoke(name, arguments))

        self._call = call

        async def sleep(seconds: float) -> None:
            self.clock.advance(seconds)
            await asyncio.sleep(0)

        self.agent = Agent(
            self.config,
            llm=self.llm,  # type: ignore[arg-type]
            call=self._call,  # type: ignore[arg-type]
            mod=self.endpoint,
            memory=_NoMemory(),  # type: ignore[arg-type]
            notes=self.notes,
            clock=self.clock,
            sleep=sleep,
        )
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.agent.run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task


async def until(condition: Callable[[], bool], *, tries: int = 5000) -> None:
    """靠让出事件循环等条件成立，不等待真实时间。"""
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("条件没有在限定次数内成立")


async def pump(rounds: int = 300) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def user_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message.get("content")) for message in messages if message.get("role") == "user")


def all_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message.get("content")) for message in messages)


def all_user_text(llm: ScriptedLLM, start: int = 0) -> str:
    return "\n".join(user_text(call) for call in llm.calls[start:])


def chat_message(mod: FakeMod, sender: str, text: str, *, private: bool = False) -> None:
    mod.body_event("player", text, fields={"sender": sender, "text": text, "private": private})


async def test_first_turn_after_connect_then_silence_when_idle_disabled(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        first = harness.llm.calls[0]
        assert first[0]["role"] == "system"
        joined = user_text(first)
        assert "以下是程序代为读取的开局资料" in joined
        assert "observe what=self" in joined
        assert "observe what=scene" in joined
        assert "goal operation=list" in joined
        # 每轮资料也在第一条消息里
        assert "自己此刻" in joined
        assert "目标清单" in joined
        await pump()
        calls_after_first_turn = harness.llm.call_count
        harness.clock.advance(3600)
        await pump()
        # 空闲叫醒关闭、没有事件：不再有模型调用
        assert harness.llm.call_count == calls_after_first_turn
    finally:
        await harness.stop()


async def test_executed_goal_tracked_then_finish_wakes_model(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "gather", "purpose": "砍木头"}}, "call-exec")
    )
    harness.llm.steps.append(llm_text("去砍了"))
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump()
        goal_id = max(harness.mod.goals)
        assert harness.mod.goals[goal_id]["state"] == "running"
        harness.mod.finish(goal_id, self_brief="站在树下，拿着石斧")
        await until(lambda: harness.llm.call_count >= 3)
        joined = all_user_text(harness.llm, start=2)
        assert f"[系统] 目标 {goal_id}" in joined
        assert "结束了：done" in joined
        assert "结束时：站在树下，拿着石斧。" in joined
    finally:
        await harness.stop()


async def test_goal_question_wakes_and_model_answers(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "gather", "purpose": "砍木头"}}, "call-exec")
    )
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump()
        goal_id = max(harness.mod.goals)
        harness.mod.ask(goal_id, "动哪个箱子？")
        # 被叫醒的那一轮里读问题、用 goal(operation=answer) 回答
        harness.llm.steps.append(
            llm_tool_call("goal", {"operation": "answer", "goal_id": goal_id, "answer": "b5"}, "call-answer")
        )
        harness.llm.steps.append(llm_text())
        await until(lambda: harness.llm.call_count >= 3)
        joined = all_user_text(harness.llm, start=2)
        assert "在等你回答" in joined
        assert "动哪个箱子？" in joined
        await until(lambda: harness.llm.call_count >= 4)
        await until(lambda: harness.mod.goals[goal_id].get("question") is None)
        assert harness.mod.goals[goal_id]["state"] == "running"
    finally:
        await harness.stop()


async def test_death_decision_wakes_model_with_decision_notice(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump()
        harness.mod.die()
        await until(lambda: harness.llm.call_count >= 2)
        joined = all_user_text(harness.llm, start=1)
        assert "mod 挂出的决定" in joined
        assert "death_recovery" in joined
    finally:
        await harness.stop()


async def test_timer_and_unhandled_wake_sky_and_hurt_only_carried(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump()
        harness.mod.body_event("sky_changed", "天黑了")
        harness.mod.body_event("hurt", "被僵尸打了")
        await pump()
        # 不叫醒
        assert harness.llm.call_count == 1
        harness.mod.body_event("timer_due", "计时到点：看看炉子")
        await until(lambda: harness.llm.call_count >= 2)
        joined = all_user_text(harness.llm, start=1)
        assert "[计时] 计时到点：看看炉子" in joined
        assert "[角色] 天黑了" in joined
        assert "[角色] 被僵尸打了" in joined
        harness.mod.body_event("need_unhandled", "饿了，身上没有吃的")
        await until(lambda: harness.llm.call_count >= 3)
        joined = all_user_text(harness.llm, start=2)
        assert "[角色] 饿了，身上没有吃的。" in joined
        assert "这是观察，不是命令" in joined
    finally:
        await harness.stop()


async def test_mentioned_chat_wakes_unmentioned_stays_in_recent(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump()
        chat_message(harness.mod, "Alex", "今天天气不错")
        harness.clock.advance(120)
        await pump()
        # 没点名：不叫醒，只进最近的聊天记录
        assert harness.llm.call_count == 1
        chat_message(harness.mod, "Steve", "Mai 过来一下")
        await until(lambda: harness.llm.call_count >= 2)
        joined = all_user_text(harness.llm, start=1)
        assert "[游戏公屏] Steve：Mai 过来一下" in joined
        assert "最近的公屏（不一定要回应）" in joined
        assert "[游戏公屏] Alex：今天天气不错" in joined
    finally:
        await harness.stop()


async def test_private_chat_marked_in_wake_message(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump()
        chat_message(harness.mod, "Steve", "悄悄话", private=True)
        await until(lambda: harness.llm.call_count >= 2)
        assert "[游戏公屏] Steve：悄悄话（私聊）" in all_user_text(harness.llm, start=1)
    finally:
        await harness.stop()


async def test_turn_ends_after_max_steps_per_turn(tmp_path: Path) -> None:
    harness = Harness(tmp_path, config=make_config(max_steps_per_turn=2))

    def keep_calling(_messages: list[dict[str, Any]]) -> dict[str, Any]:
        return llm_tool_call("observe", {"what": "self"}, f"call-{len(harness.llm.calls)}")

    harness.llm.script = keep_calling
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump()
        harness.clock.advance(3600)
        await pump()
        assert harness.llm.call_count == 2
    finally:
        await harness.stop()


async def test_transient_failure_retries_then_replays_wake_contents(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump()
        # 叫醒的那一轮里模型连续瞬时失败：初次 + 3 次重试
        harness.llm.steps = [llm_failure("transient", "超时") for _ in range(4)]  # type: ignore[assignment]
        harness.mod.body_event("timer_due", "计时到点：看看炉子")
        await until(lambda: harness.llm.call_count >= 5)
        # 初次 + 3 次重试，这一轮结束
        assert harness.llm.call_count == 5
        count = harness.llm.call_count
        # 到点后叫醒内容回到队列，再次叫醒时还在
        harness.clock.advance(120)
        await pump()
        await until(lambda: harness.llm.call_count >= count + 1)
        assert "[计时] 计时到点：看看炉子" in user_text(harness.llm.calls[count])
    finally:
        await harness.stop()


async def test_fatal_failure_stops_model_calls_until_hourly_retry(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.llm.steps.append(llm_failure("fatal", "鉴权失败"))
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump(20)  # 让事件流先建立游标，之后的事件才会被处理
        harness.mod.body_event("timer_due", "计时到点")
        # fatal 之后停一个小时才重试；fatal 期间事件照读、内容进队列
        await until(lambda: harness.llm.call_count >= 2)
        assert "上一次调用模型服务失败" in user_text(harness.llm.calls[1])
        # fatal 期间事件照读：攒下的叫醒内容随重试或下一轮交给模型
        await until(lambda: "[计时] 计时到点" in all_user_text(harness.llm, start=1))
        # 重试成功后恢复正常，不再每小时一次
        count = harness.llm.call_count
        harness.clock.advance(7200)
        await pump()
        assert harness.llm.call_count == count
    finally:
        await harness.stop()


async def test_invalid_failure_ends_turn_without_retry(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.llm.steps.append(llm_failure("invalid", "参数被拒"))
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump()
        harness.clock.advance(7200)
        await pump()
        assert harness.llm.call_count == 1
    finally:
        await harness.stop()


async def test_idle_wake_until_max_empty_turns_then_external_event_resumes(tmp_path: Path) -> None:
    harness = Harness(tmp_path, config=make_config(idle_replan_minutes=1, max_empty_turns=2))
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)  # 连上后的第一轮：空轮 1
        harness.clock.advance(61)
        await pump()
        await until(lambda: harness.llm.call_count >= 2)  # 空闲叫醒：空轮 2
        assert "你已经空闲 1 分钟" in user_text(harness.llm.calls[1])
        harness.clock.advance(61)
        await pump()
        # 连续空轮达到上限：不再空闲叫醒
        assert harness.llm.call_count == 2
        # 外部事件叫醒：连续空轮清零，空闲叫醒恢复
        harness.mod.body_event("timer_due", "计时到点")
        await until(lambda: harness.llm.call_count >= 3)
        harness.clock.advance(61)
        await pump()
        await until(lambda: harness.llm.call_count >= 4)
        assert "你已经空闲 1 分钟" in user_text(harness.llm.calls[3])
    finally:
        await harness.stop()


async def test_no_idle_wake_while_goal_tracked(tmp_path: Path) -> None:
    harness = Harness(tmp_path, config=make_config(idle_replan_minutes=1, max_empty_turns=1))
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "gather", "purpose": "砍木头"}}, "call-exec")
    )
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        goal_id = max(harness.mod.goals)
        harness.clock.advance(120)
        await pump()
        # 目标还在跟踪：不算空闲
        assert harness.llm.call_count == 2
        harness.mod.finish(goal_id)
        await until(lambda: harness.llm.call_count >= 3)
    finally:
        await harness.stop()


async def test_world_switch_moves_notes_and_rebuilds_opening(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    # 第一轮里下达一个目标、写下笔记（此时还没认出存档，笔记先留在内存里）
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "gather", "purpose": "砍木头"}}, "call-exec")
    )
    harness.llm.steps.append(llm_text())
    harness.llm.steps.append(llm_tool_call("notes", {"notebook": "老存档的笔记"}, "call-notes"))
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump()
        # 进第一个存档：第一次认出，还没归属的笔记归它
        harness.mod.enter_world("aaaaaaaa-1111")
        await until(lambda: harness.notes.world_id == "aaaaaaaa-1111")
        await until(lambda: harness.notes.document()["notebook"] == "老存档的笔记")
        # 换存档：旧目标都不在了，笔记换一份
        harness.mod.change_world()
        harness.mod.enter_world("bbbbbbbb-2222")
        await until(lambda: harness.llm.call_count >= 5)
        assert harness.notes.world_id == "bbbbbbbb-2222"
        assert harness.notes.document()["notebook"] == ""
        assert "另一个存档" in user_text(harness.llm.calls[4])
        # 开局资料在换存档后重建：资料里的 self 带新存档编号、目标清单里没有旧存档的目标
        opening = next(
            str(m.get("content"))
            for m in harness.llm.calls[4]
            if m.get("role") == "user" and "以下是程序代为读取的开局资料" in str(m.get("content"))
        )
        assert "bbbbbbbb-2222" in opening
        assert "砍木头" not in opening
    finally:
        await harness.stop()


async def test_long_public_chat_split_into_several_executes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "chat", "parameters": {"message": LONG_CHAT_TEXT}}}, "call-chat")
    )
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump()
        chat_calls = [
            arguments["goal"]["parameters"]["message"]
            for name, arguments in harness.mod.calls
            if name == "execute"
            and str((arguments.get("goal") or {}).get("ability") or "") in ("chat", "maicraft:chat")
        ]
        assert 2 <= len(chat_calls) <= 3
        assert "".join(chat_calls) == LONG_CHAT_TEXT
        joined = all_text(harness.llm.calls[1])
        assert "第 1 句" in joined
        assert "第 2 句" in joined
    finally:
        await harness.stop()


async def test_disconnect_notice_then_reconnect_rebuilds_opening(tmp_path: Path) -> None:
    clock = ManualClock()
    mod = FakeMod(clock=clock)
    down = {"flag": False}

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if down["flag"]:
            raise ModUnavailable("连接断了")
        return reply_of(await mod.invoke(name, arguments))

    harness = Harness(tmp_path, mod=mod, call=call)

    def call_observe_and_disconnect(_messages: list[dict[str, Any]]) -> dict[str, Any]:
        # 模型发起 observe 的那一刻把连接断掉：这次工具调用没有送达
        down["flag"] = True
        return llm_tool_call("observe", {"what": "self"}, "call-obs")

    harness.llm.steps.append(call_observe_and_disconnect)
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        joined = all_text(harness.llm.calls[1])
        assert "现在连不上游戏" in joined
        assert "这次调用没有送达" in joined
        # 重新连上：重建开局资料
        down["flag"] = False
        await until(lambda: harness.llm.call_count >= 3)
        assert "以下是程序代为读取的开局资料" in user_text(harness.llm.calls[2])
        # 重连后事件照常处理
        await pump(30)
        mod.body_event("timer_due", "计时到点：重连后的事件")
        await until(lambda: harness.llm.call_count >= 4)
        assert "计时到点：重连后的事件" in user_text(harness.llm.calls[3])
    finally:
        await harness.stop()


async def test_invalid_json_arguments_get_retry_hint(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.llm.steps.append(
        {
            "success": True,
            "content": None,
            "finish_reason": "tool_calls",
            "tool_calls": [
                {"id": "call-bad", "type": "function", "function": {"name": "observe", "arguments": "{不是 JSON"}}
            ],
        }
    )
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        tool_message = next(m for m in harness.llm.calls[1] if m.get("role") == "tool")
        assert "参数不是合法的 JSON" in str(tool_message["content"])
    finally:
        await harness.stop()


async def test_chat_loop_survives_disconnect_and_wakes_after_reconnect(tmp_path: Path) -> None:
    clock = ManualClock()
    mod = FakeMod(clock=clock)

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if events_down["flag"] and name == "events":
            raise ModUnavailable("读公屏时断了")
        return reply_of(await mod.invoke(name, arguments))

    events_down = {"flag": False}
    harness = Harness(tmp_path, mod=mod, call=call)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump(30)
        # 公屏读取在途时断线：公屏循环不死，主循环回去重连
        events_down["flag"] = True
        await pump(30)
        chat_message(mod, "Steve", "Mai 过来一下")
        await pump(30)
        events_down["flag"] = False
        await pump(30)  # 重连后让公屏流重新建立游标
        await until(lambda: harness.llm.call_count >= 2)  # 重连后的第一轮
        harness.clock.advance(5)
        chat_message(mod, "Steve", "Mai 又点了一次")
        await until(lambda: harness.llm.call_count >= 3)
        await until(lambda: "[游戏公屏] Steve：Mai 又点了一次" in all_user_text(harness.llm, start=1))
    finally:
        await harness.stop()


async def test_in_game_name_counts_as_mention(tmp_path: Path) -> None:
    harness = Harness(tmp_path, config=make_config())
    harness.config.chat.mention_names = []
    harness.mod.self_view["name"] = "Mai"
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump(30)
        # 名字是从 observe(self) 读到的：读到之后游戏内名字就算点名
        chat_message(harness.mod, "Steve", "Mai 过来一下")
        await until(lambda: harness.llm.call_count >= 2)
        assert "[游戏公屏] Steve：Mai 过来一下" in all_user_text(harness.llm, start=1)
    finally:
        await harness.stop()


async def test_sky_changes_keep_only_latest_carry(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump(30)
        harness.mod.body_event("sky_changed", "天黑了")
        harness.mod.body_event("sky_changed", "下雨了")
        harness.mod.body_event("sky_changed", "天亮了")
        await pump(30)
        harness.mod.body_event("timer_due", "计时到点")
        await until(lambda: harness.llm.call_count >= 2)
        joined = all_user_text(harness.llm, start=1)
        assert "[角色] 天亮了" in joined
        assert "天黑了" not in joined
        assert "下雨了" not in joined
    finally:
        await harness.stop()


async def test_paused_goal_wakes_model_with_pause_notice(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "gather", "purpose": "砍木头"}}, "call-exec")
    )
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump(30)
        goal_id = max(harness.mod.goals)
        harness.mod.pause(goal_id)
        await until(lambda: harness.llm.call_count >= 3)
        joined = all_user_text(harness.llm, start=2)
        assert f"[系统] 目标 {goal_id}（maicraft:gather，砍木头）被暂停了" in joined
        assert "goal(operation=resume, goal_id=" in joined
    finally:
        await harness.stop()


async def test_idle_wake_fires_while_goal_paused(tmp_path: Path) -> None:
    harness = Harness(tmp_path, config=make_config(idle_replan_minutes=1, max_empty_turns=5))
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "gather", "purpose": "砍木头"}}, "call-exec")
    )
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump(30)
        goal_id = max(harness.mod.goals)
        harness.mod.pause(goal_id)
        await until(lambda: harness.llm.call_count >= 3)
        await pump(30)
        # 被暂停的目标不算忙：空闲叫醒照常触发
        harness.clock.advance(61)
        await pump()
        await until(lambda: harness.llm.call_count >= 4)
        assert "你已经空闲 1 分钟" in user_text(harness.llm.calls[3])
    finally:
        await harness.stop()
