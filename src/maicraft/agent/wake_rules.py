"""叫醒规则：一种处境要不要叫醒模型、给模型看的那句话怎么写。

全部是纯函数，不碰队列也不碰模型：主循环拿到 ``WakeDecision`` 后，
``wake`` 为真就把 ``message`` 推进唤醒队列并叫醒，为假就作为捎带内容留到下次。
给模型看的消息前缀只用 ``[系统]``、``[角色]``、``[游戏公屏]``、``[计时]`` 四种。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .goal_registry import decision_notice, finish_notice, goal_label, question_notice
from .mod_protocol import GoalRun
from .world_identity import WorldRef

__all__ = [
    "WakeDecision",
    "body_event",
    "decision",
    "goal_finished",
    "goal_gone",
    "goal_paused",
    "goal_question",
    "goal_stalled",
    "world_switched",
]


@dataclass(frozen=True, slots=True)
class WakeDecision:
    """要不要叫醒模型，以及这次给模型看的那句话。"""

    wake: bool
    message: str


# 角色自己处理不了、需要模型知道的身体事件：观察优先，先看自己再决定
_SELF_FIRST_HINT = '。这是观察，不是命令：先看一眼自己（observe(what="self")）再决定怎么办。'


def body_event(event: dict[str, Any]) -> WakeDecision:
    """角色身上的一件事：计时到点、身体处理不了的要叫醒；其余捎带不叫醒。

    ``event["message"]`` 是 mod 写好的一句话。
    """
    kind = str(event.get("kind") or "")
    message = str(event.get("message") or "")
    if kind in ("need_unhandled", "character_died"):
        return WakeDecision(wake=True, message=f"[角色] {message}{_SELF_FIRST_HINT}")
    if kind == "timer_due":
        return WakeDecision(wake=True, message=f"[计时] {message}")
    return WakeDecision(wake=False, message=f"[角色] {message}")


def goal_finished(run: GoalRun, self_brief: str) -> WakeDecision:
    return WakeDecision(wake=True, message=finish_notice(run, self_brief))


def goal_question(run: GoalRun) -> WakeDecision:
    return WakeDecision(wake=True, message=question_notice(run))


def goal_paused(run: GoalRun) -> WakeDecision:
    """目标被暂停：角色没有在做它，叫醒模型决定继续还是取消。"""
    return WakeDecision(
        wake=True,
        message=(
            f"[系统] 目标 {run.goal_id}（{goal_label(run)}）被暂停了，角色没有在做它：{run.doing or '（mod 没说原因）'}。"
            '先看一眼现场（observe(what="self")）再决定：用 goal(operation=resume, goal_id='
            f"{run.goal_id}) 继续，或用 goal(operation=cancel, goal_id={run.goal_id}) 取消。"
        ),
    )


def goal_stalled(run: GoalRun) -> WakeDecision:
    """mod 判定目标长时间没有新事件：还在推进就让模型确认后继续等。"""
    return WakeDecision(
        wake=True,
        message=(
            f"[系统] 目标 {run.goal_id}（{goal_label(run)}）长时间没有新事件，仍在推进中：{run.doing}。"
            "用 goal(operation=get, goal_id=…) 看它在做什么；"
            "还在正常推进（长途赶路、连续作业、等前置完成）就结束这一轮继续等；"
            "确认走不通了再取消或换个办法。"
        ),
    )


def decision(run: GoalRun) -> WakeDecision:
    return WakeDecision(wake=True, message=decision_notice(run))


def goal_gone(goal_id: int, reason: str) -> WakeDecision:
    """目标不在了（多半换了存档）：只捎带，不值得单独叫醒。"""
    return WakeDecision(wake=False, message=f"[系统] 目标 {goal_id} 不在了：{reason}")


def world_switched(before: WorldRef | None, after: WorldRef) -> WakeDecision:
    """认出存档：第一次认出告知现在在哪，换了存档则提醒旧目标都不在了。"""
    if before is None:
        return WakeDecision(wake=True, message=f"[系统] 现在在存档「{after.label()}」里。")
    return WakeDecision(
        wake=True,
        message=(
            f"[系统] 角色进了另一个存档「{after.label()}」（原来在「{before.label()}」）。"
            "之前的目标都不在了，重新看现场再决定做什么。"
        ),
    )
