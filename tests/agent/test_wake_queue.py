"""唤醒队列直测：wake 与 carry 两种内容、天色替换、取走清空与去重、捎带条数上限。"""

from __future__ import annotations

from maicraft.agent.wake_queue import WakeQueue


def test_empty_queue() -> None:
    queue = WakeQueue()
    assert not queue
    assert len(queue) == 0
    assert not queue.can_wake()
    assert queue.take_all() == []


def test_push_wake_and_carry() -> None:
    queue = WakeQueue()
    queue.push_wake("有话对角色说")
    queue.push_carry("背包里有新物品")
    assert queue
    assert len(queue) == 2
    assert queue.can_wake()
    assert queue.take_all() == ["有话对角色说", "背包里有新物品"]
    assert not queue
    assert not queue.can_wake()


def test_carry_alone_does_not_wake() -> None:
    queue = WakeQueue()
    queue.push_carry("背包里有新物品")
    assert queue
    assert not queue.can_wake()


def test_replace_sky_keeps_only_latest() -> None:
    queue = WakeQueue()
    queue.replace_sky("天黑了")
    queue.replace_sky("天亮了")
    queue.push_carry("背包里有新物品")
    assert len(queue) == 2
    assert queue.take_all() == ["天亮了", "背包里有新物品"]


def test_take_all_orders_and_dedupes() -> None:
    queue = WakeQueue()
    queue.push_carry("下雨了")
    queue.push_wake("有话对角色说")
    queue.push_carry("下雨了")
    queue.push_wake("有话对角色说")
    assert queue.take_all() == ["有话对角色说", "下雨了"]


def test_carry_overflow_drops_oldest() -> None:
    queue = WakeQueue(max_carry=2)
    queue.push_carry("第一条")
    queue.push_carry("第二条")
    queue.push_carry("第三条")
    assert queue.take_all() == ["第二条", "第三条"]


def test_clear() -> None:
    queue = WakeQueue()
    queue.push_wake("有话对角色说")
    queue.push_carry("下雨了")
    queue.clear()
    assert not queue
    assert queue.take_all() == []
