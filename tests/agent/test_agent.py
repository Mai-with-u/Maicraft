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
from maicraft.agent.mod_protocol import LOOKUP, no_reply, reply_of
from maicraft.agent.notes import Notes
from maicraft.config import MaicraftConfig
from maicraft.mod_client import ModUnavailable
from tests.fakes import (
    FakeMod,
    FakeModEndpoint,
    ManualClock,
    ScriptedLLM,
    llm_failure,
    llm_text,
    llm_tool_call,
    tool_result,
)

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
        self.sleep_calls: list[float] = []
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
            self.sleep_calls.append(seconds)
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


async def test_fatal_wait_does_not_spin_on_a_due_chat_batch(tmp_path: Path) -> None:
    """模型服务持续失败后的等待期，已到点的公屏批不再把等待超时算成 0 而空转。

    公屏点名批先到点、随后那一轮模型调用 fatal：修复前等待把到点的批当成 0 超时候选，
    虚拟时间推不进、到不了每小时的重试时刻，主循环一直空转；修复后到重试时刻的那一轮
    通过 take_due 把积压的批带上交给模型，内容不丢。
    """
    harness = Harness(tmp_path)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump(20)
        # 公屏点名进入筛选器，合批窗口还没到点（不会在下一轮开始时被取走）
        chat_message(harness.mod, "Steve", "Mai 过来一下")
        await until(lambda: harness.agent._chat_filter.next_wake_delay_ms() is not None)
        # 随后叫醒的那一轮模型调用 fatal，进入每小时重试的等待期；点名批在这一轮期间到点、
        # 整个等待期都积压在筛选器里
        harness.llm.steps.append(llm_failure("fatal", "鉴权失败"))
        harness.mod.body_event("timer_due", "计时到点")
        await until(lambda: harness.llm.call_count >= 2)
        await pump(20)
        await until(lambda: harness.llm.call_count >= 3, tries=2000)
        joined = all_user_text(harness.llm, start=2)
        assert "上一次调用模型服务失败" in joined
        assert "Mai 过来一下" in joined
        # 覆盖了整个 fatal 等待期（约一小时虚拟时间）：空转的特征是等待超时恒为 0、
        # 反复 0 延迟地睡醒，正常退避与空页补等都是正延迟，0 延迟的 sleep 一次都不该有
        assert [s for s in harness.sleep_calls if s == 0] == []
    finally:
        await harness.stop()


async def test_reenter_world_whole_new_stream_page_wakes_model_once(tmp_path: Path) -> None:
    """同一个存档里事件流换了（重进世界），新流那一页带着恢复成暂停的目标：模型被叫醒一次且只一次。

    这一页就是新流的全部时页里事件照常处理（暂停通知交给模型）；事件核对过的目标不再重复查询，
    不会产生第二条暂停通知。
    """
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
        # 重进世界：mod 换了一条流，恢复出来的目标停着，新流那一页带着它的暂停事件
        harness.mod.stream_id = "stream-2"
        harness.mod.pause(goal_id)
        await until(lambda: harness.llm.call_count >= 3)
        await pump(30)
        assert harness.llm.call_count == 3
        joined = all_user_text(harness.llm, start=2)
        assert f"目标 {goal_id}" in joined
        assert joined.count("被暂停了") == 1
    finally:
        await harness.stop()


async def test_prepare_failure_backs_off_between_reconnects(tmp_path: Path) -> None:
    """探测成功但准备阶段持续失败时，重连之间按 1、2、5、10、30 秒的序列退避，不再无间隔空转。"""
    clock = ManualClock()
    mod = FakeMod(clock=clock)
    lookup_fail = {"flag": True}
    lookup_calls = {"count": 0}

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if name == LOOKUP and lookup_fail["flag"]:
            # 探测用的 observe(self) 一直成功，只有准备阶段的 lookup 断线
            await asyncio.sleep(0)
            lookup_calls["count"] += 1
            raise ModUnavailable("准备阶段断了")
        return reply_of(await mod.invoke(name, arguments))

    harness = Harness(tmp_path, mod=mod, call=call)
    harness.start()
    try:
        t0 = harness.clock.now
        # 推进 60 秒虚拟时间：按序列退避最多 6 次尝试（1+2+5+10+30+30 = 78 秒才第 7 次）
        await until(lambda: harness.clock.now >= t0 + 60, tries=20000)
        assert lookup_calls["count"] <= 6
        # 撤掉故障后，在退避间隔之内恢复连接并跑这一轮
        lookup_fail["flag"] = False
        await until(lambda: harness.llm.call_count >= 1, tries=20000)
        assert harness.agent._connect_failures == 0
    finally:
        await harness.stop()


async def test_turn_material_no_reply_is_treated_as_disconnect(tmp_path: Path) -> None:
    """每轮资料里 goal(list) 根本没拿到回复时按断线处理：这一轮不调模型，恢复后内容照常交给模型。"""
    clock = ManualClock()
    mod = FakeMod(clock=clock)
    goal_down = {"flag": False}

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if goal_down["flag"] and name == "goal" and arguments.get("operation") == "list":
            # 先让出事件循环再回 no_reply：模拟真实往返，不让调用方在无让出的协程里连续循环
            await asyncio.sleep(0)
            return no_reply("没有拿到回复")
        return reply_of(await mod.invoke(name, arguments))

    harness = Harness(tmp_path, mod=mod, call=call)
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 1)
        await pump(30)
        # 这一轮的每轮资料读不到目标清单：不调模型、按断线处理
        goal_down["flag"] = True
        harness.mod.body_event("timer_due", "计时到点：看看炉子")
        await pump(200)
        assert harness.llm.call_count == 1
        # 恢复后重连，积压的内容随下一轮交给模型
        goal_down["flag"] = False
        await until(lambda: harness.llm.call_count >= 2, tries=20000)
        assert "计时到点：看看炉子" in all_user_text(harness.llm, start=1)
    finally:
        await harness.stop()


async def test_not_ready_tool_reply_is_data_not_disconnect(tmp_path: Path) -> None:
    """mod 未接线工具回 ``{"success": false, "error": {...}}``：作为失败结果交给模型，不当断线处理。"""
    mod = FakeMod()
    not_ready = {
        "success": False,
        "error": {"code": "not_ready", "message": "工具 observe 已声明，但它的真实行为还没有接入。"},
    }

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if name == "observe":
            return reply_of(tool_result(not_ready))
        return reply_of(await mod.invoke(name, arguments))

    harness = Harness(tmp_path, mod=mod, call=call)
    harness.llm.steps.append(llm_tool_call("observe", {"what": "self"}, "call-obs"))
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump(30)
        tool_message = next(m for m in harness.llm.calls[1] if m.get("role") == "tool")
        content = str(tool_message["content"])
        assert "not_ready" in content
        assert "还没有接入" in content
        assert "现在连不上游戏" not in content
        # 没有标记断线：agent 还连着，主循环没有回到重连
        assert harness.agent._connected
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


async def test_world_switch_rebuild_failing_midway_does_not_kill_agent(tmp_path: Path) -> None:
    """换存档后重建开局资料的四次 mod 调用中途断线：agent 不退出，重连后按新存档重建并跑轮。

    断线要真的落在 ``_build_opening_material`` 的调用上，而不是它之前的每轮资料
    （``_turn_material`` 只调 observe(self) 和 goal(list)，没有 lookup），否则这条回归测试
    在"重建块不在 try 里"的旧代码上也能通过。做法：换存档后布防，假件见到布防后的
    第一个 lookup（开局资料的第 3 次调用）起抛 ``ModUnavailable``，恢复前一直断着；
    断线时挂起一小段虚拟时间再失败，让重连环不独占事件循环。
    """
    clock = ManualClock()
    mod = FakeMod(clock=clock)
    armed = {"flag": False}
    fail_on_lookup = {"flag": False}

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        if armed["flag"] and name == LOOKUP:
            fail_on_lookup["flag"] = True  # 布防后第一次 lookup 就是开局资料里那次
        if fail_on_lookup["flag"]:
            # 断线：挂起再失败，重连环里等在别处的任务能被调度到
            await asyncio.sleep(0)
            clock.advance(0.1)
            await asyncio.sleep(0)
            raise ModUnavailable("重建开局资料时断了")
        return reply_of(await mod.invoke(name, arguments))

    harness = Harness(tmp_path, mod=mod, call=call)
    harness.llm.steps.append(
        llm_tool_call("execute", {"goal": {"ability": "gather", "purpose": "砍木头"}}, "call-exec")
    )
    harness.llm.steps.append(llm_text())
    harness.start()
    try:
        await until(lambda: harness.llm.call_count >= 2)
        await pump(30)
        # 进第一个存档并留一个在跑的目标，然后换存档：旧目标全没了
        mod.enter_world("aaaaaaaa-1111")
        await until(lambda: harness.notes.world_id == "aaaaaaaa-1111")
        await pump(30)
        # 布防后布防轮的每轮资料（observe self、goal list）先成功，
        # 布防后第一次 lookup（重建开局资料里那次）起断线，抛进重建块；恢复前一直断着
        armed["flag"] = True
        calls_before = harness.llm.call_count
        mod.change_world()
        mod.enter_world("bbbbbbbb-2222")
        await pump(120)
        assert fail_on_lookup["flag"]  # 断线确实落在了开局资料的 lookup 上
        assert harness.llm.call_count == calls_before  # 没去调模型
        assert harness.agent._need_opening  # 重建没完成，重连后还要重建
        # 重连：开局资料按新存档重建，之后正常跑这一轮
        armed["flag"] = False
        fail_on_lookup["flag"] = False
        for _ in range(2000):
            await asyncio.sleep(0)
            if harness.llm.call_count >= calls_before + 1:
                break
        assert harness.llm.call_count >= calls_before + 1
        opening = next(
            str(m.get("content"))
            for m in harness.llm.calls[-1]
            if m.get("role") == "user" and "以下是程序代为读取的开局资料" in str(m.get("content"))
        )
        assert "bbbbbbbb-2222" in opening
        assert "砍木头" not in opening
        assert not harness.agent._need_opening
    finally:
        await harness.stop()
