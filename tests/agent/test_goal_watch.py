"""后台目标跟踪直测：读事件流只认本 agent 的目标，变化时取完整样子，换了世界如实说不在了。"""

from __future__ import annotations

from typing import Any

from maicraft.agent.goal_watch import GoalWatch
from maicraft.agent.mod_protocol import EventsPage, GoalRun, goal_run_of, no_reply, reply_of
from tests.fakes import FakeMod, ok_envelope, tool_result


class _Recorder:
    """记下跟踪器交出来的东西。"""

    def __init__(self) -> None:
        self.changed: list[GoalRun] = []
        self.after_world_change: list[bool] = []
        self.self_briefs: list[str] = []
        self.stalled: list[bool] = []
        self.gone: list[tuple[int, str]] = []
        self.body: list[dict[str, Any]] = []
        self.decisions: list[GoalRun] = []
        self.pages: list[dict[str, Any]] = []

    async def on_changed(self, run: GoalRun, after_world_change: bool, self_brief: str, stalled: bool = False) -> None:
        self.changed.append(run)
        self.after_world_change.append(after_world_change)
        self.self_briefs.append(self_brief)
        self.stalled.append(stalled)

    async def on_gone(self, goal_id: int, reason: str) -> None:
        self.gone.append((goal_id, reason))

    async def on_body(self, event: dict[str, Any]) -> None:
        self.body.append(event)

    async def on_decision(self, run: GoalRun) -> None:
        self.decisions.append(run)

    async def on_page(self, page: EventsPage) -> None:
        self.pages.append(dict(page.world_fields))


async def _noop_sleep(_seconds: float) -> None:
    return None


def _watch(mod: FakeMod, recorder: _Recorder) -> GoalWatch:
    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return reply_of(await mod.invoke(name, arguments))

    return GoalWatch(
        call=call,
        connected=lambda: True,
        on_goal_changed=recorder.on_changed,
        on_goal_gone=recorder.on_gone,
        on_body_event=recorder.on_body,
        on_decision_asked=recorder.on_decision,
        on_page=recorder.on_page,
        wait_ms=10,
        sleep=_noop_sleep,
    )


async def _launch(mod: FakeMod, watch: GoalWatch, ability: str = "maicraft:gather") -> int:
    reply = reply_of(await mod.invoke("execute", {"goal": {"ability": ability}}))
    run = goal_run_of(reply.data)
    assert run is not None
    watch.track(run)
    return run.goal_id


async def test_first_read_only_sets_the_cursor_and_does_not_replay_old_events() -> None:
    """接手前就有的事件不补发：首次读取只建立游标。"""
    mod, recorder = FakeMod(), _Recorder()
    mod.body_event("need_unhandled", "饿了，身上没有吃的")
    watch = _watch(mod, recorder)

    await watch.step()

    assert recorder.body == [] and recorder.changed == []
    assert watch._stream.cursor == 1


async def test_finished_goal_is_read_in_full_and_no_longer_tracked() -> None:
    """目标结束：取它此刻的完整样子交出去，之后不再跟踪。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_id = await _launch(mod, watch)
    mod.finish(goal_id, summary="砍了 5 块原木")

    await watch.step()

    assert [run.goal_id for run in recorder.changed] == [goal_id]
    finished = recorder.changed[0]
    assert finished.finished and finished.result_status == "done"
    assert finished.result["changes"][0]["count"] == 5
    assert not watch.tracking(goal_id)
    assert recorder.self_briefs == [""], "mod 没在结束事件上写自身摘要时交空串"


async def test_the_self_brief_on_a_finished_event_rides_along_with_the_result() -> None:
    """结束事件上那一行自身摘要只在事件里、查询里没有：随查询结果一起交给回调；提问这类事件不带。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_id = await _launch(mod, watch)
    mod.ask(goal_id)
    await watch.step()
    brief = "在 minecraft:overworld 12, 64, -8；血 18，饱食 15；手上 minecraft:iron_pickaxe"
    mod.finish(goal_id, self_brief=f"  {brief}  ")

    await watch.step()

    assert [run.state for run in recorder.changed] == ["awaiting_answer", "finished"]
    assert recorder.self_briefs == ["", brief]
    assert "self_brief" not in (recorder.changed[-1].result or {}), "摘要不混进查询到的结果"


async def test_questions_are_handed_over_and_the_goal_stays_tracked() -> None:
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_id = await _launch(mod, watch)
    mod.ask(goal_id)

    await watch.step()

    assert recorder.changed[0].question["options"][0]["id"] == "b5"
    assert watch.tracking(goal_id)


async def test_other_goals_are_ignored_and_body_events_pass_through() -> None:
    """别人下达的目标不管；生存需求的事件原样交给事件回调。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    reply = reply_of(await mod.invoke("execute", {"goal": {"ability": "maicraft:gather"}}))
    mod.finish(reply.data["goal_id"])
    mod.body_event("temporary_task_started", "自卫：有僵尸打过来")
    mod.body_event("character_died", "角色死了，等重生")

    await watch.step()

    assert recorder.changed == []
    # 别的目标的结束事件不在跟踪名单里：和其他事件一样原样交给事件回调
    assert [event["kind"] for event in recorder.body] == [
        "finished",
        "temporary_task_started",
        "character_died",
    ]
    assert mod.calls_to("goal") == [], "没在跟踪的目标不去查"


async def test_events_outside_the_known_kinds_table_still_pass_through() -> None:
    """不在已知种类表里的事件（如 hurt、discovered）也原样交给事件回调。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    mod.body_event("hurt", "被僵尸打了 3 点血")
    mod.body_event("discovered", "发现了新的村庄")

    await watch.step()

    assert [event["kind"] for event in recorder.body] == ["hurt", "discovered"]
    assert recorder.changed == []


async def test_after_a_world_change_missing_goals_are_reported_gone() -> None:
    """事件流换了：逐个查在跟踪的目标，查不到的如实说不在了。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_id = await _launch(mod, watch)
    mod.change_world()

    await watch.step()

    assert [gone[0] for gone in recorder.gone] == [goal_id]
    assert not watch.tracking(goal_id)


async def test_only_a_changed_stream_is_reported_as_a_world_change() -> None:
    """撤离后挂的暂停是同一条流里的普通事件；重进世界后读回来先停着的目标，流编号变了，交回调时说明是换流后查到的。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_id = await _launch(mod, watch)

    # 同一条流里的暂停事件：不是重进世界。
    mod.pause(goal_id)
    await watch.step()
    assert [run.state for run in recorder.changed] == ["paused"]
    assert recorder.after_world_change == [False]

    # 连接重建后流没换（只是游标重建）：也不是重进世界。
    mod.goals[goal_id]["state"] = "running"
    watch.forget_stream()
    await watch.step()
    assert recorder.after_world_change[-1] is False

    # 重进同一个世界：mod 换了一条流，没做完的目标读回来先停着；连接重建后第一次读也认得出流换了。
    mod.stream_id = "stream-2"
    mod.events.clear()
    mod.goals[goal_id]["state"] = "paused"
    watch.forget_stream()
    await watch.step()
    assert recorder.changed[-1].state == "paused"
    assert recorder.after_world_change[-1] is True
    assert watch.tracking(goal_id)


async def test_a_reused_id_for_another_ability_is_not_mistaken_for_the_old_goal() -> None:
    """换世界后编号撞上别的目标：能力都不一样，按原来的不在了处理。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_id = await _launch(mod, watch, "maicraft:gather")
    mod.change_world()
    await mod.invoke("execute", {"goal": {"ability": "maicraft:fight"}})

    await watch.step()

    assert recorder.gone and recorder.gone[0][0] == goal_id
    assert "另一个目标" in recorder.gone[0][1]
    assert recorder.changed == []


async def test_unreadable_or_failed_reads_do_not_raise() -> None:
    """读事件失败：退避后重试，不抛出、不改游标。"""
    recorder = _Recorder()

    async def failing(name: str, arguments: dict[str, Any]) -> Any:
        return no_reply("连不上")

    watch = GoalWatch(
        call=failing,
        connected=lambda: True,
        on_goal_changed=recorder.on_changed,
        on_goal_gone=recorder.on_gone,
        on_body_event=recorder.on_body,
        sleep=_noop_sleep,
    )
    await watch.step()

    assert watch._stream.stream_id is None and recorder.changed == []


async def test_a_death_recovery_question_is_handed_over_without_tracking_it() -> None:
    """死亡恢复是 mod 自己挂出的决策：死亡照常算事件，提问读一次交给决策回调，不进跟踪名单。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    decision_id = mod.die()

    await watch.step()

    assert [event["kind"] for event in recorder.body] == ["character_died"]
    assert [run.goal_id for run in recorder.decisions] == [decision_id]
    assert recorder.decisions[0].ability == "death_recovery"
    assert recorder.decisions[0].question["options"][0]["id"] == "respawn"
    assert recorder.changed == [] and watch.tracked_ids() == []


async def test_a_stalled_event_refreshes_the_goal_and_flags_the_callback() -> None:
    """mod 的停滞判定：目标还在跟踪、查询刷新后带 stalled 旗标交给回调。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_id = await _launch(mod, watch)
    mod._append("stalled", goal_id, "目标很久没有新事件")

    await watch.step()

    assert [run.state for run in recorder.changed] == ["running"]
    assert recorder.stalled == [True], "停滞旗标随查询结果一起交给回调"
    assert watch.tracking(goal_id), "停滞不是结束，目标继续跟踪"


async def test_goal_watch_hands_every_page_with_its_world() -> None:
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)

    await watch.step()  # 旧版 mod：页里没有 world 键
    mod.enter_world("aaa", "老家")
    await watch.step()
    mod.leave_world()
    await watch.step()

    assert recorder.pages == [{}, {"world": {"id": "aaa", "name": "老家", "source": "world_id_file"}}, {"world": None}]


async def test_tracking_roster_basics() -> None:
    """登记、查询与清空；已经结束的目标不登记。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    goal_id = await _launch(mod, watch, "maicraft:gather")
    finished = goal_run_of({"goal_id": 99, "ability": "maicraft:gather", "state": "finished"})
    assert finished is not None
    watch.track(finished)

    assert watch.tracking(goal_id) and not watch.tracking(99)
    assert watch.tracked_ids() == [goal_id]
    assert [goal.goal_id for goal in watch.tracked_goals()] == [goal_id]
    assert watch.tracked_abilities() == ["maicraft:gather"]

    watch.clear()
    assert watch.tracked_ids() == []


class _ScriptedEventsCall:
    """events 按脚本回页、其余调用转发给 FakeMod：用来构造 FakeMod 给不出的页（积压多页、游标失效）。

    pages 是调用方持有的列表，脚本按次序取用；调用方可以在构造之后继续往里追加页。
    """

    def __init__(self, mod: FakeMod, pages: list[dict[str, Any]]) -> None:
        self._mod = mod
        self._pages = pages

    async def __call__(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "events":
            assert self._pages, "脚本里没有准备好下一页回复"
            return reply_of(tool_result(ok_envelope(self._pages.pop(0))))
        return reply_of(await self._mod.invoke(name, arguments))


def _watch_with_script(mod: FakeMod, recorder: _Recorder, pages: list[dict[str, Any]]) -> GoalWatch:
    return GoalWatch(
        call=_ScriptedEventsCall(mod, pages),
        connected=lambda: True,
        on_goal_changed=recorder.on_changed,
        on_goal_gone=recorder.on_gone,
        on_body_event=recorder.on_body,
        on_decision_asked=recorder.on_decision,
        on_page=recorder.on_page,
        wait_ms=10,
        sleep=_noop_sleep,
    )


async def test_a_whole_new_stream_page_is_processed_then_resynced() -> None:
    """换流后这一页就是新流的全部：页里的暂停事件与不叫醒事件都照常处理，然后重新核对没被事件核对过的目标。"""
    mod, recorder = FakeMod(), _Recorder()
    watch = _watch(mod, recorder)
    await watch.step()
    goal_a = await _launch(mod, watch, "maicraft:gather")
    goal_b = await _launch(mod, watch, "maicraft:obtain")
    # 重进世界：mod 换了一条流，恢复出来的目标 A 停着，页里还带一条身体事件；目标 B 没有事件
    mod.stream_id = "stream-2"
    mod.pause(goal_a)
    mod.body_event("sky_changed", "天黑了")

    await watch.step()

    # 暂停事件走现有的目标变更路径，只产生一次通知；身体事件原样交给事件回调
    assert [run.goal_id for run in recorder.changed] == [goal_a, goal_b]
    assert recorder.changed[0].state == "paused"
    assert [event["kind"] for event in recorder.body] == ["sky_changed"]
    # 事件里的目标 A 刚被核对过不再重复查；目标 B 由重新核对查到，交回调时说明是换流后查到的
    assert recorder.after_world_change == [False, True]
    assert watch.tracking(goal_a) and watch.tracking(goal_b)


async def test_a_stream_change_with_backlog_still_takes_over_without_processing() -> None:
    """换流后积压多页：按接手处理，返回的那一页事件不处理，只重新核对在跟踪的目标。"""
    mod, recorder = FakeMod(), _Recorder()
    pages: list[dict[str, Any]] = [{"stream_id": "stream-1", "cursor": 0, "events": [], "has_more": False}]
    watch = _watch_with_script(mod, recorder, pages)
    await watch.step()
    goal_id = await _launch(mod, watch)
    mod.stream_id = "stream-2"
    mod.pause(goal_id)
    pages.append({"stream_id": "stream-2", "cursor": 1, "events": [dict(mod.events[-1])], "has_more": True})
    pages.append({"stream_id": "stream-2", "cursor": 1, "events": [], "has_more": False})

    await watch.step()

    assert recorder.body == [], "积压多页的换流按接手处理，页里的事件不补发"
    assert [run.goal_id for run in recorder.changed] == [goal_id]
    assert recorder.changed[0].state == "paused"
    assert recorder.after_world_change == [True]


async def test_an_invalid_cursor_on_the_same_stream_does_not_reprocess_events() -> None:
    """同一条流只是游标失效：这一页的事件不处理（不重复），在跟踪的目标照常重新核对。"""
    mod, recorder = FakeMod(), _Recorder()
    pages: list[dict[str, Any]] = [{"stream_id": "stream-1", "cursor": 0, "events": [], "has_more": False}]
    watch = _watch_with_script(mod, recorder, pages)
    await watch.step()
    goal_id = await _launch(mod, watch)
    mod.pause(goal_id)
    pages.append(
        {
            "stream_id": "stream-1",
            "cursor": 1,
            "events": [dict(mod.events[-1])],
            "has_more": False,
            "cursor_status": "stream_changed",
        }
    )

    await watch.step()

    assert recorder.body == [], "同一流游标失效的页事件不重复处理"
    assert [run.goal_id for run in recorder.changed] == [goal_id]
    assert recorder.after_world_change == [False]
