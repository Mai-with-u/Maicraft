"""目标登记表：本 agent 下达给 mod 的目标在内存里的记录与通知文案。

内存里记三样东西，可独立构造断言（不需要建主循环）：
- 目标此刻的样子（``note``：状态 / 提问 / 正在做的事 / 停滞标记）；
- 被暂停的目标；
- 最近结束的目标（新任务开始时清空，恢复与集中整理带上看）。

通知路由（结束/暂停/提问时唤醒谁）留在主循环；本模块只出文案：
``finish_notice`` / ``question_notice`` / ``decision_notice`` 是纯函数。
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Sequence
from typing import Any

from .mod_facts import goal_mining_and_pits_notes
from .mod_protocol import AWAITING_ANSWER, PAUSED, GoalRun

__all__ = [
    "GoalRegistry",
    "decision_notice",
    "finish_notice",
    "goal_label",
    "is_use_on_air_misuse",
    "question_notice",
    "use_air_misuse_hint",
]

# 最近结束的目标保留条数：新任务开始时清空，恢复与集中整理带上看
_RECENT_FINISHED_GOALS = 6


def json_text(value: Any) -> str:
    """稳定序列化，避免空格变化制造重复正文。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def goal_label(run: GoalRun) -> str:
    """目标在通知里的人读标签：能力名 + 目的（有目的时）。

    刻意保留带命名空间的能力名，模型读得懂工具口径。
    """
    return f"{run.ability}，{run.purpose}" if run.purpose else run.ability


def is_use_on_air_misuse(run: GoalRun) -> bool:
    """判定是否属于"对着空气用了物品未生效"的用法错误（不是物品或账号用不了）。

    满足三要素：能力是 ``use``、结果 ``status=="failed"``、``details.used_on=="air"`` 且
    ``problem.kind=="not_possible_here"``（大小写容忍）。
    """
    if run.ability not in ("use", "maicraft:use") or run.result_status != "failed":
        return False
    res = run.result if isinstance(run.result, dict) else {}
    details = res.get("details") if isinstance(res.get("details"), dict) else {}
    problem = res.get("problem") if isinstance(res.get("problem"), dict) else {}
    return details.get("used_on") == "air" and str(problem.get("kind") or "").lower() == "not_possible_here"


def use_air_misuse_hint(run: GoalRun) -> str:
    """用法纠正说明：命中对空使用未生效时返回明确指引，其余返回空串。"""
    if not is_use_on_air_misuse(run):
        return ""
    return (
        "【系统判定】这次是对着空气用了手上的物品（used_on=air），是用错了对象或少了朝向面——"
        '放在脚下地面上请传 target={"kind": "here"} 并配 parameters.face="up"，'
        '对指定方块用给 b# 或坐标（放顶面配 face="up"），对生物用给 e#。'
        "不是这件物品坏了，也不是这个账号或权限放不出来，改对 target/face 再用即可。"
    )


def finish_notice(run: GoalRun, self_brief: str) -> str:
    """目标结束的通知：达成情况、结束那一刻角色怎样、结果，以及折起来的大块证据怎么读回。

    结束时在哪、血和饱食、手上拿着什么、开着什么界面由 mod 写成一行附在结束事件上：
    模型接下一步之前不用为这些先看一眼自己。mod 默认把结果里的大块证据（试过的办法、
    大的细节、长列表）折起来列在 ``result.folded``，这里给的不是完整结果：有折起来的段时
    说清用哪个调用、照抄哪个 path 读回来，不让模型以为证据丢了。
    """
    brief = self_brief.strip().rstrip("。")
    body = f"结束时：{brief}。" if brief else ""
    folded = (run.result or {}).get("folded")
    if isinstance(folded, list) and folded:
        # 有折起来的段：告诉模型按段读回，长列表照返回的 next 一页页翻，不必一次全拿。
        label = (
            f"结果（大块证据折在 result.folded 里，要看哪段就照抄它的 path 用 "
            f"goal(operation=get, goal_id={run.goal_id}, path=…) 读，长的照返回的 next 翻页）"
        )
    else:
        label = "结果"
    misuse = use_air_misuse_hint(run)
    tail = f" {misuse}" if misuse else ""
    return (
        f"[系统] 目标 {run.goal_id}（{goal_label(run)}）结束了：{run.result_status}。"
        f"{body}{label}：{json_text(run.result)}{tail}"
    )


def question_notice(run: GoalRun) -> str:
    """目标在等回答的通知：说清目标、问题原文与各选项，以及怎么回答。"""
    return (
        f"[系统] 目标 {run.goal_id}（{goal_label(run)}）在等你回答：{json_text(run.question)}。"
        f"用 goal(operation=answer, goal_id={run.goal_id}, answer=<选项的 id>) 回答。"
    )


def decision_notice(run: GoalRun) -> str:
    """mod 自己挂出的决定（如死亡恢复，目标编号是负数）在等回答的通知。

    决策不是本 agent 下达的目标：不进跟踪名单。没人回答，角色就一直停在那里。
    """
    return (
        f"[系统] mod 挂出的决定（{run.ability}，goal_id={run.goal_id}）在等你回答：{json_text(run.question)}。"
        f"按问题里给的选项，用 goal(operation=answer, goal_id={run.goal_id}, answer=<选项的 id>) 回答。"
        f"没人回答，角色就一直停在那里。"
    )


class GoalRegistry:
    """目标的内存记录。非线程安全；仅在主循环的事件循环内使用。"""

    def __init__(self) -> None:
        self._notes: dict[int, dict[str, Any]] = {}
        self._paused: set[int] = set()
        self._finished: deque[dict[str, Any]] = deque(maxlen=_RECENT_FINISHED_GOALS)

    def note(self, run: GoalRun) -> None:
        """记下目标此刻的样子：状态、提问与正在做的事。"""
        note: dict[str, Any] = {"goal_id": run.goal_id, "ability": run.ability, "state": run.state}
        if run.purpose:
            note["purpose"] = run.purpose
        if run.question:
            note["question"] = run.question
        if run.doing:
            note["doing"] = run.doing
        self._notes[run.goal_id] = note
        if run.state == PAUSED:
            self._paused.add(run.goal_id)
        else:
            self._paused.discard(run.goal_id)

    def note_of(self, goal_id: int) -> dict[str, Any]:
        """读目标此刻的样子；没有记录时返回空 dict（调用方按字段判差异）。"""
        return self._notes.get(goal_id, {})

    def forget(self, goal_id: int) -> None:
        """目标移出跟踪时的收尾：此刻的样子与暂停标记一并清除。"""
        self._notes.pop(goal_id, None)
        self._paused.discard(goal_id)

    def note_stalled(self, run: GoalRun) -> None:
        """记下这个目标停住了：``note_of`` 里多一个 ``"stalled": True``。

        只改内存里的记录；下一次 ``note(run)`` 整条覆盖后标记消失。不在记录里的目标
        先照此刻的样子记一条，再打标记。
        """
        if run.goal_id not in self._notes:
            self.note(run)
        self._notes[run.goal_id]["stalled"] = True

    def stalled_goal_ids(self) -> list[int]:
        """被标记为停住的目标编号（按编号排序）。"""
        return sorted(goal_id for goal_id, note in self._notes.items() if note.get("stalled"))

    def _append_finished_deduped(self, entry: dict[str, Any]) -> None:
        """按 goal_id 去重写入最近结束记录：同一目标从回执与事件流两头都看到终态时只留最新一条。"""
        gid = entry.get("goal_id")
        if gid is not None:
            kept = [item for item in self._finished if item.get("goal_id") != gid]
            self._finished.clear()
            self._finished.extend(kept)
        self._finished.append(entry)

    def remember_finished(self, run: GoalRun) -> None:
        """终态目标进最近结束记录（按 goal_id 去重、保留最近若干条，供恢复与集中整理带上看）。"""
        entry: dict[str, Any] = {
            "goal_id": run.goal_id,
            "ability": run.ability,
            "status": run.result_status,
            "summary": run.summary,
        }
        entry.update(goal_mining_and_pits_notes(run.result))
        self._append_finished_deduped(entry)

    def settle_finished(self, run: GoalRun) -> None:
        """目标结束的完整收尾：清此刻的样子与暂停标记、进最近结束记录。"""
        self.forget(run.goal_id)
        self.remember_finished(run)

    def mark_gone(self, goal_id: int, reason: str) -> dict[str, Any]:
        """在跟踪的目标查不到了（多半是换了世界）：按"不在了"记进最近结束记录，返回它最后的样子。"""
        note = self._notes.pop(goal_id, {"goal_id": goal_id})
        self._paused.discard(goal_id)
        self._append_finished_deduped({**note, "status": "gone", "summary": reason})
        return note

    def actionable_goal_ids(self) -> list[int]:
        """要模型动手才会往下走的目标：在等回答的、被暂停的。"""
        return sorted(
            goal_id
            for goal_id, note in self._notes.items()
            if note.get("state") == AWAITING_ANSWER or goal_id in self._paused
        )

    def notes_for(self, goal_ids: Sequence[int]) -> list[dict[str, Any]]:
        """按目标号取此刻的样子；没有记录的跳过。"""
        return [self._notes[goal_id] for goal_id in goal_ids if goal_id in self._notes]

    def all_notes_sorted(self) -> list[dict[str, Any]]:
        """全部在记的目标样子，按目标号排序（恢复与集中整理用）。"""
        return [note for _, note in sorted(self._notes.items())]

    def recent_finished(self) -> list[dict[str, Any]]:
        """最近结束的目标（旧到新）。"""
        return list(self._finished)

    def clear_finished(self) -> None:
        """新任务开始：上个任务的目标终态记录不再带看。"""
        self._finished.clear()
