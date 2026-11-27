"""叫醒规则直测：哪种处境叫醒、消息前缀与文字。"""

from __future__ import annotations

from maicraft.agent.mod_protocol import GoalRun
from maicraft.agent.wake_rules import (
    body_event,
    decision,
    goal_finished,
    goal_gone,
    goal_question,
    goal_stalled,
    world_switched,
)
from maicraft.agent.world_identity import WorldRef


def _run(**overrides: object) -> GoalRun:
    fields: dict = {
        "goal_id": 7,
        "ability": "maicraft:gather",
        "purpose": "砍木头",
        "state": "running",
        "question": None,
        "result": None,
        "doing": "走向树",
        "data": {},
    }
    fields.update(overrides)
    return GoalRun(**fields)


def test_need_unhandled_and_death_wake_with_self_first_hint() -> None:
    for kind in ("need_unhandled", "character_died"):
        decision = body_event({"kind": kind, "message": "身上出事了"})
        assert decision.wake is True
        assert decision.message.startswith("[角色] 身上出事了。")
        assert 'observe(what="self")' in decision.message


def test_timer_due_wakes_with_timer_prefix() -> None:
    decision = body_event({"kind": "timer_due", "message": "炉子该看了"})
    assert decision.wake is True
    assert decision.message == "[计时] 炉子该看了"


def test_other_body_events_carry_without_waking() -> None:
    for kind in ("sky_changed", "hurt", "resumed"):
        decision = body_event({"kind": kind, "message": "别的事"})
        assert decision.wake is False
        assert decision.message == "[角色] 别的事"


def test_goal_finished_wakes_with_finish_notice() -> None:
    run = _run(state="finished", result={"status": "done", "summary": "砍了 5 块原木"})
    decision = goal_finished(run, "站在树下，拿着石斧")
    assert decision.wake is True
    assert decision.message.startswith("[系统] 目标 7（maicraft:gather，砍木头）结束了：done。")
    assert "结束时：站在树下，拿着石斧。" in decision.message


def test_goal_question_wakes_with_question_notice() -> None:
    run = _run(state="awaiting_answer", question={"text": "动哪个箱子？"})
    decision = goal_question(run)
    assert decision.wake is True
    assert "在等你回答" in decision.message
    assert "goal(operation=answer, goal_id=7" in decision.message


def test_goal_stalled_wakes_with_doing_and_hint() -> None:
    decision = goal_stalled(_run(doing="长途赶路"))
    assert decision.wake is True
    assert "目标 7（maicraft:gather，砍木头）长时间没有新事件" in decision.message
    assert "仍在推进中：长途赶路" in decision.message
    assert "goal(operation=get" in decision.message


def test_decision_wakes_with_decision_notice() -> None:
    result = decision(
        _run(goal_id=-1, ability="death_recovery", state="awaiting_answer", question={"text": "重生吗？"})
    )
    assert result.wake is True
    assert "mod 挂出的决定" in result.message
    assert "goal_id=-1" in result.message


def test_goal_gone_carries_without_waking() -> None:
    decision = goal_gone(7, "mod 里查不到这个目标了")
    assert decision.wake is False
    assert decision.message == "[系统] 目标 7 不在了：mod 里查不到这个目标了"


def test_world_switched_first_recognition_and_switch() -> None:
    after = WorldRef(id="w-1", name="生存存档")
    first = world_switched(None, after)
    assert first.wake is True
    assert first.message == "[系统] 现在在存档「生存存档」里。"

    switched = world_switched(WorldRef(id="w-0", name="旧存档"), after)
    assert switched.wake is True
    assert "角色进了另一个存档「生存存档」（原来在「旧存档」）" in switched.message
    assert "之前的目标都不在了" in switched.message


def test_goal_paused_wakes_with_resume_or_cancel_hint() -> None:
    from maicraft.agent.wake_rules import goal_paused

    result = goal_paused(_run(state="paused", doing="等许可"))
    assert result.wake is True
    assert result.message.startswith("[系统] 目标 7（maicraft:gather，砍木头）被暂停了")
    assert "角色没有在做它" in result.message
    assert 'observe(what="self")' in result.message
    assert "goal(operation=resume, goal_id=7)" in result.message
    assert "goal(operation=cancel, goal_id=7)" in result.message
