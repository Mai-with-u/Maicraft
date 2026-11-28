"""后台目标跟踪：本 agent 下达给 mod 的目标，靠 ``events`` 长轮询知道它们何时需要处理。

mod 把目标运行的处境变化写进事件流，这里只盯本 agent 自己下达、还没结束的目标：

- 目标提问、被暂停、恢复、结束时，再用 ``goal(get)`` 取它此刻的样子交给回调——
  事件只说"变了"，结果（变化、问题、剩下的部分）以查询为准；结束事件另带结束那一刻
  角色的一行自身摘要（``self_brief``），查询里没有这一行，跟着一起交过去；
- 其余所有事件（生存需求、天色、计时到点、与目标无关的一切）原样交给事件回调；
- mod 自己挂出的决策（死亡恢复：编号是负数，不是谁下达的目标）提问时，读一次交给
  决策回调，等模型按选项回答；
- 事件流换了（换世界、重进世界）时游标作废，逐个重新查询在跟踪的目标：查得到就照常
  交回调，查不到或已经不是原来那个目标，就按"不在了"交给回调，不让它挂在待办里
  永远等不到结果；流编号真变了（连接重建后也和之前读到的比）时告诉回调这是换流后
  查到的，目标停着多半是读回来先停着，别的暂停不说成重进世界。换流后读到的第一页
  如果就是新流的全部（积压没有多页），页里的事件照常逐条处理——重进世界最初几秒
  发生的暂停、身体事件都在这一页里；积压多页时按接手处理，旧事件不补发。

连接没就绪或读取失败时退避重试，不抛出；跟踪循环本身不调用模型，空闲时只是一条
挂着的长轮询。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from maicraft.utils.logger import get_logger

from .event_stream import DEFAULT_WAIT_MS, RETRY_DELAYS_S, EventStream
from .mod_protocol import AWAITING_ANSWER, GOAL, EventsPage, GoalRun, McpCall, goal_run_of

logger = get_logger("GoalWatch")

# 这些事件说明目标的处境变了，需要取它此刻的完整样子。
_GOAL_CHANGE_KINDS = frozenset({"asked", "paused", "resumed", "finished"})
# mod 侧停滞判定（目标长时间没有任何新事件仍未结束）：目标处境也算变了，
# 取它此刻的样子交给回调（回调据此记录中间状态并转发），不唤醒模型另做决策。
_GOAL_STALLED_KIND = "stalled"

# 第二个参数：这次是事件流换了编号（重进世界或游戏重启）后重新核对时查到的。
# 第三个参数：结束事件带来的那一行自身摘要；不是结束事件、重新核对时查到的、mod 没写时为空串。
# 第四个参数：这条变化由 mod 的停滞事件（stalled）引来。
GoalChanged = Callable[[GoalRun, bool, str, bool], Awaitable[None]]
GoalGone = Callable[[int, str], Awaitable[None]]
DecisionAsked = Callable[[GoalRun], Awaitable[None]]
BodyEvent = Callable[[dict[str, Any]], Awaitable[None]]
# 每读到一页事件就把这一页交出去：页里带着角色眼下在哪个存档（认存档的唯一喂入点）
PageSeen = Callable[[EventsPage], Awaitable[None]]


@dataclass(slots=True)
class TrackedGoal:
    """一个在跟踪的目标：编号、下达它时是哪种能力（换世界后用来认出是不是同一个）、下达时刻（单调毫秒）。"""

    goal_id: int
    ability: str
    purpose: str
    issued_at_ms: int


class GoalWatch:
    """读事件流、认出本 agent 的目标、在它们需要处理时交给回调。"""

    def __init__(
        self,
        *,
        call: McpCall,
        connected: Callable[[], bool],
        on_goal_changed: GoalChanged,
        on_goal_gone: GoalGone,
        on_body_event: BodyEvent,
        on_decision_asked: DecisionAsked | None = None,
        on_page: PageSeen | None = None,
        wait_ms: int = DEFAULT_WAIT_MS,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._call = call
        self._connected = connected
        self._on_goal_changed = on_goal_changed
        self._on_goal_gone = on_goal_gone
        self._on_body_event = on_body_event
        self._on_decision_asked = on_decision_asked
        self._on_page = on_page
        # 下达时刻的取时入口（单调毫秒域）
        self._clock_ms: Callable[[], int] = clock_ms if clock_ms is not None else self._loop_time_ms
        self._sleep = sleep
        self._goals: dict[int, TrackedGoal] = {}
        self._stream = EventStream(call=call, wait_ms=wait_ms, sleep=sleep)
        self._failures = 0
        self._task: asyncio.Task[None] | None = None

    # ----- 跟踪名单 -----

    def track(self, run: GoalRun) -> None:
        """登记一个刚下达（或恢复）的目标；已经结束的不登记。下达时刻取登记当时。"""
        if run.finished:
            return
        self._goals[run.goal_id] = TrackedGoal(run.goal_id, run.ability, run.purpose, self._clock_ms())

    def untrack(self, goal_id: int) -> None:
        self._goals.pop(goal_id, None)

    def tracking(self, goal_id: int) -> bool:
        return goal_id in self._goals

    def tracked_ids(self) -> list[int]:
        return sorted(self._goals)

    def tracked_goals(self) -> list[TrackedGoal]:
        """在跟踪的目标（按编号排）：急停逐个叫停时要知道每个在做什么，回执才说得出哪个没停下。"""
        return [self._goals[goal_id] for goal_id in sorted(self._goals)]

    def tracked_abilities(self) -> list[str]:
        """在跟踪的目标各是哪种能力（死讯到时据此认出是不是自己在寻死）。"""
        return [goal.ability for goal in self._goals.values()]

    def clear(self) -> None:
        """换了逻辑任务或连接重建：名单清空，游标保留（流没变时不重读旧事件）。"""
        self._goals.clear()

    # ----- 运行 -----

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 停机时的循环异常只记日志
            logger.warning(f"目标跟踪循环退出异常: {type(exc).__name__}: {exc}")

    def forget_stream(self) -> None:
        """连接重建后服务端可能换了进程：下次读取当作首次，重新建立游标并核对在跟踪的目标。"""
        self._stream.forget()

    async def _loop(self) -> None:
        while True:
            if not self._connected():
                await self._back_off()
                continue
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单次读取异常不杀死跟踪循环
                logger.warning(f"读取事件出错，稍后重试: {type(exc).__name__}: {exc}")
                await self._back_off()

    async def _back_off(self) -> None:
        delay = RETRY_DELAYS_S[min(self._failures, len(RETRY_DELAYS_S) - 1)]
        self._failures += 1
        await self._sleep(delay)

    async def step(self) -> None:
        """读一页事件并处理。

        换流且这一页就是新流的全部（``has_more`` 为假）时，页里的角色事件逐条照常处理——
        重进世界最初几秒发生的暂停、身体事件都在这一页里，整页丢弃会让它们永远看不到；
        处理完再重新核对在跟踪的目标（事件已逐个核对过的目标不再重复查）。
        其余的流更换与游标作废按接手或重新核对处理，这一页的事件不补发；首次读取
        （接手或连接重建后）只建立游标，不过此时若还跟踪着目标，也逐个重新核对一遍。
        """
        read = await self._stream.read()
        if read is None:
            return
        await self._see_page(read.page)
        whole_new_stream = not read.first and read.stream_changed and not read.page.has_more
        refreshed: set[int] = set()
        if whole_new_stream:
            for event in read.page.events:
                goal_id = await self._absorb(event)
                if goal_id is not None:
                    refreshed.add(goal_id)
        if read.resync or (read.first and self._goals):
            await self._resync(read.stream_changed, skip=refreshed)
            return
        if read.first:
            return
        if not whole_new_stream:
            for event in read.page.events:
                await self._absorb(event)

    async def _see_page(self, page: EventsPage) -> None:
        """把这一页交给页回调（认存档）；回调出错只记日志，不打断目标跟踪。"""
        if self._on_page is None:
            return
        try:
            await self._on_page(page)
        except Exception as exc:  # noqa: BLE001 - 认存档出错不影响读事件、跟踪目标
            logger.warning(f"处理事件页上的存档信息出错: {type(exc).__name__}: {exc}")

    async def _absorb(self, event: dict[str, Any]) -> int | None:
        """处理一条事件；事件触发了一次目标查询时返回那个目标编号，其余返回 None。"""
        kind = str(event.get("kind") or "")
        goal_id = event.get("goal_id")
        if kind == "asked" and isinstance(goal_id, int) and goal_id < 0 and goal_id not in self._goals:
            # 负数编号是 mod 自己挂出的决策（死亡恢复），不是谁下达的目标：不跟踪，读一次交给决策回调。
            await self._decision_asked(goal_id)
            return None
        if (
            isinstance(goal_id, int)
            and goal_id in self._goals
            and (kind in _GOAL_CHANGE_KINDS or kind == _GOAL_STALLED_KIND)
        ):
            # 目标结束时 mod 在事件上附一行"结束那一刻角色怎样"，goal(get) 里没有：记下来随查询结果一起交给回调；
            # 刚进世界还没看清自己时 mod 不带这个字段，就交空串。
            brief = event.get("self_brief") if kind == "finished" else None
            await self._refresh(
                goal_id, self_brief=brief.strip() if isinstance(brief, str) else "", stalled=kind == _GOAL_STALLED_KIND
            )
            return goal_id
        # 其余事件原样交给事件回调，不限事件的种类。
        await self._on_body_event(event)
        return None

    async def _refresh(
        self, goal_id: int, after_world_change: bool = False, self_brief: str = "", stalled: bool = False
    ) -> None:
        """取目标此刻的样子交给回调；结束了就不再跟踪。查询失败留着，等下一条事件或重新同步。"""
        reply = await self._call(GOAL, {"operation": "get", "goal_id": goal_id})
        tracked = self._goals.get(goal_id)
        if tracked is None:
            return
        if not reply.ok:
            if reply.error_code == "unknown_id":
                self.untrack(goal_id)
                await self._on_goal_gone(goal_id, "mod 里查不到这个目标了")
            else:
                logger.warning(f"查询目标 {goal_id} 失败，等下一条事件再查：{reply.error_code} {reply.error_message}")
            return
        run = goal_run_of(reply.data)
        if run is None or run.goal_id != goal_id:
            logger.warning(f"目标 {goal_id} 的查询结果读不懂：{reply.raw}")
            return
        if tracked.ability and run.ability and run.ability != tracked.ability:
            # 换了世界后编号可能撞上别的目标：能力都不一样，说明原来那个目标已经不在这个世界里。
            self.untrack(goal_id)
            await self._on_goal_gone(goal_id, f"现在这个编号是另一个目标（{run.ability}），原来的目标不在这个世界里")
            return
        if run.finished:
            self.untrack(goal_id)
        await self._on_goal_changed(run, after_world_change, self_brief, stalled)

    async def _decision_asked(self, decision_id: int) -> None:
        """取 mod 挂出的决策此刻的样子：还在等回答就交给决策回调；读不到或已答复就不打扰模型。"""
        if self._on_decision_asked is None:
            return
        reply = await self._call(GOAL, {"operation": "get", "goal_id": decision_id})
        run = goal_run_of(reply.data) if reply.ok else None
        if run is None or run.state != AWAITING_ANSWER:
            logger.info(
                f"决策 {decision_id} 已经不在等回答，不叫醒模型：{reply.error_code or (run.state if run else '读不懂')}"
            )
            return
        await self._on_decision_asked(run)

    async def _resync(self, after_world_change: bool = False, skip: set[int] | None = None) -> None:
        """逐个重新核对在跟踪的目标；``skip`` 里的编号刚被本页的事件核对过，不再重复查。"""
        for goal_id in list(self._goals):
            if skip is not None and goal_id in skip:
                continue
            await self._refresh(goal_id, after_world_change)

    @staticmethod
    def _loop_time_ms() -> int:
        return int(asyncio.get_running_loop().time() * 1000)
