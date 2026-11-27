"""目标登记表直测：内存记录、可行动目标查询、终态收尾、停滞标记与通知文案，不建主循环。"""

from __future__ import annotations

from typing import Any

from maicraft.agent.goal_registry import (
    GoalRegistry,
    decision_notice,
    finish_notice,
    goal_label,
    is_use_on_air_misuse,
    question_notice,
    use_air_misuse_hint,
)
from maicraft.agent.mod_protocol import goal_run_of


def _run(**overrides: Any) -> Any:
    data = {"goal_id": 1, "ability": "maicraft:gather", "state": "running"}
    data.update(overrides)
    run = goal_run_of(data)
    assert run is not None
    return run


# ---------------------------------------------------------------------------
# 内存记录
# ---------------------------------------------------------------------------


def test_notes_and_paused_book() -> None:
    registry = GoalRegistry()
    registry.note(_run(goal_id=2, state="awaiting_answer", question={"text": "往哪边走？"}))
    registry.note(_run(goal_id=3, state="paused"))
    registry.note(_run(goal_id=4, state="running", doing="走向树"))

    assert registry.note_of(4)["doing"] == "走向树"
    assert registry.note_of(99) == {}
    assert registry.actionable_goal_ids() == [2, 3]
    assert registry.notes_for([3, 4, 99]) == [registry.note_of(3), registry.note_of(4)]
    assert [note["goal_id"] for note in registry.all_notes_sorted()] == [2, 3, 4]

    # 恢复（状态更新为 running）后移出暂停记录，不再可行动
    registry.note(_run(goal_id=3, state="running"))
    assert registry.actionable_goal_ids() == [2]

    registry.forget(2)
    assert registry.note_of(2) == {} and registry.actionable_goal_ids() == []


def test_settle_finished_and_recent_finished() -> None:
    registry = GoalRegistry()
    registry.note(_run(goal_id=1, state="running", doing="挖到一半"))
    finished = _run(goal_id=1, state="finished", result={"status": "done", "summary": "砍了 5 块原木"})

    registry.settle_finished(finished)

    assert registry.note_of(1) == {} and registry.actionable_goal_ids() == []
    assert registry.recent_finished() == [
        {"goal_id": 1, "ability": "maicraft:gather", "status": "done", "summary": "砍了 5 块原木"}
    ]

    registry.clear_finished()
    assert registry.recent_finished() == []


def test_mark_gone_records_gone_and_returns_last_note() -> None:
    registry = GoalRegistry()
    registry.note(_run(goal_id=7, state="running", doing="挖到一半"))

    note = registry.mark_gone(7, "换了世界")

    assert note["doing"] == "挖到一半"
    assert registry.note_of(7) == {}
    assert registry.recent_finished()[-1]["status"] == "gone"
    assert registry.recent_finished()[-1]["summary"] == "换了世界"


def test_remember_finished_deduplicates_same_goal_id_keeping_latest() -> None:
    """同一目标从回执与事件流两头都看到终态时，最近结束记录只留一条，不重复占位。"""
    registry = GoalRegistry()
    for gid in (2142, 2143, 2144):
        first = _run(goal_id=gid, state="finished", result={"status": "done", "summary": f"目标 {gid} 回执终态"})
        second = _run(goal_id=gid, state="finished", result={"status": "done", "summary": f"目标 {gid} 事件终态"})
        registry.remember_finished(first)
        registry.settle_finished(second)

    recent = registry.recent_finished()
    assert [item["goal_id"] for item in recent] == [2142, 2143, 2144]
    assert [item["summary"] for item in recent] == [
        "目标 2142 事件终态",
        "目标 2143 事件终态",
        "目标 2144 事件终态",
    ]


def test_recent_finished_keeps_at_most_six_and_carries_mining_notes() -> None:
    registry = GoalRegistry()
    for goal_id in range(1, 9):
        registry.remember_finished(
            _run(
                goal_id=goal_id,
                state="finished",
                result={
                    "status": "done",
                    "summary": f"第 {goal_id} 步",
                    "details": {"chain_mining": {"chained_batches": 1, "chained_blocks": 4}},
                },
            )
        )

    recent = registry.recent_finished()
    assert [item["goal_id"] for item in recent] == list(range(3, 9)), "只留最近 6 条"
    assert recent[-1]["chain_mining"] == "连锁 1 批共 4 格"


# ---------------------------------------------------------------------------
# 停滞标记
# ---------------------------------------------------------------------------


def test_note_stalled_marks_memory_and_next_note_clears_it() -> None:
    registry = GoalRegistry()
    registry.note(_run(goal_id=5, state="running", doing="挖石料"))

    registry.note_stalled(_run(goal_id=5, state="running"))

    assert registry.note_of(5)["stalled"] is True
    assert registry.stalled_goal_ids() == [5]

    registry.note(_run(goal_id=5, state="running", doing="换了个矿道"))
    assert "stalled" not in registry.note_of(5)
    assert registry.stalled_goal_ids() == []

    # 没记过的目标也能直接打标记：先记下此刻的样子再打
    registry.note_stalled(_run(goal_id=6, state="running", ability="maicraft:obtain"))
    assert registry.note_of(6)["ability"] == "maicraft:obtain"
    assert registry.note_of(6)["stalled"] is True
    assert registry.stalled_goal_ids() == [6]


# ---------------------------------------------------------------------------
# 通知文案
# ---------------------------------------------------------------------------


def test_goal_label_combines_ability_and_purpose() -> None:
    assert goal_label(_run(ability="maicraft:gather", purpose="砍柴")) == "maicraft:gather，砍柴"
    assert goal_label(_run(ability="maicraft:gather")) == "maicraft:gather"


def test_finish_notice_mentions_result_and_folded_path() -> None:
    run = _run(state="finished", result={"status": "done", "summary": "完了", "folded": [{"path": "/attempts"}]})
    notice = finish_notice(run, "在河边，血量满。")

    assert notice.startswith("[系统]")
    assert goal_label(run) in notice and "结束时：在河边，血量满。" in notice
    assert "result.folded" in notice and "goal(operation=get, goal_id=1, path=" in notice
    assert '"summary":"完了"' in notice, "结果原样放进通知"
    assert "【系统判定】" not in notice


def test_finish_notice_carries_air_misuse_hint() -> None:
    run = _run(
        state="finished",
        ability="maicraft:use",
        result={
            "status": "failed",
            "summary": "没放出来",
            "details": {"used_on": "air"},
            "problem": {"kind": "not_possible_here"},
        },
    )
    notice = finish_notice(run, "")
    assert "【系统判定】" in notice and "used_on=air" in notice
    hint = use_air_misuse_hint(run)
    assert hint.startswith("【系统判定】")
    assert "maicraft_goal" not in hint


def test_use_air_misuse_only_for_the_exact_shape() -> None:
    assert not is_use_on_air_misuse(_run(ability="maicraft:use", result={"status": "failed"}))
    assert not is_use_on_air_misuse(
        _run(ability="maicraft:use", result={"status": "failed", "details": {"used_on": "air"}})
    )
    assert is_use_on_air_misuse(
        _run(
            ability="use",
            result={"status": "failed", "details": {"used_on": "air"}, "problem": {"kind": "Not_Possible_Here"}},
        )
    )


def test_question_notice_names_goal_question_and_answer_call() -> None:
    run = _run(goal_id=3, state="awaiting_answer", question={"text": "动哪个箱子？", "options": [{"id": "b5"}]})
    notice = question_notice(run)

    assert notice.startswith("[系统]")
    assert "目标 3" in notice and goal_label(run) in notice
    assert "动哪个箱子？" in notice and "b5" in notice
    assert "goal(operation=answer, goal_id=3, answer=<选项的 id>)" in notice


def test_decision_notice_names_the_mod_decision_and_the_stake() -> None:
    run = _run(goal_id=-1, ability="death_recovery", state="awaiting_answer", question={"text": "接下来怎么办？"})
    notice = decision_notice(run)

    assert notice.startswith("[系统]")
    assert "death_recovery" in notice and "goal_id=-1" in notice
    assert "接下来怎么办？" in notice
    assert "goal(operation=answer, goal_id=-1" in notice
    assert "没人回答" in notice and "一直停" in notice
