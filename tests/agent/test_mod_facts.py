"""mod 报的事实短句：各函数覆盖"没报这个字段""报了空值""报了正常值"。"""

from __future__ import annotations

from typing import Any

from maicraft.agent.mod_facts import chain_mining_text, goal_mining_and_pits_notes, pits_text

# ---------------------------------------------------------------------------
# chain_mining_text
# ---------------------------------------------------------------------------


def test_chain_mining_missing_field_is_none() -> None:
    assert chain_mining_text(None) is None
    assert chain_mining_text("不是对象") is None
    assert chain_mining_text({}) is None, "什么都不报时没有短句"


def test_chain_mining_empty_counts_are_still_a_sentence() -> None:
    assert chain_mining_text({"chained_batches": 0, "chained_blocks": 0, "single_blocks": 0}) == "未触发连锁"


def test_chain_mining_normal_value_is_one_line() -> None:
    assert chain_mining_text({"chained_batches": 3, "chained_blocks": 12, "single_blocks": 2}) == (
        "连锁 3 批共 12 格、单挖 2 格"
    )
    assert chain_mining_text({"single_blocks": 5, "not_chained_reason": "没有相邻同类方块"}) == (
        "单挖 5 格，未触发连锁（没有相邻同类方块）"
    )
    assert chain_mining_text({"chained_batches": 2, "chained_blocks": 8, "not_chained_reason": "中途被打断"}) == (
        "连锁 2 批共 8 格（部分未连：中途被打断）"
    )


# ---------------------------------------------------------------------------
# pits_text
# ---------------------------------------------------------------------------


def test_pits_missing_field_is_none() -> None:
    assert pits_text(None) is None
    assert pits_text([]) is None
    assert pits_text(["不是对象"]) is None


def test_pits_normal_values_cover_filled_and_unfilled() -> None:
    filled = [{"filled": True, "depth": 2, "note": "填了泥土"}]
    assert pits_text(filled) == "顺手回填留坑 1 处（深 2 格：填了泥土）"

    unfilled = [{"filled": False, "depth": 3, "note": "旁边的羊挡着没法填"}]
    assert pits_text(unfilled) == "留坑 1 处未填平（深 3 格：旁边的羊挡着没法填）"

    mixed = [
        {"filled": True, "depth": 1},
        {"filled": False, "depth": 4, "note": "回来说"},
        {"filled": True, "depth": 2, "note": "填了石头"},
    ]
    assert pits_text(mixed) == "留坑 3 处（已填 2 处、未填 1 处，最深 4 格：回来说）"

    # 备注里的括号坐标剥掉，超长的截短
    assert "12, 64, -8" not in pits_text([{"filled": True, "note": "填了泥土（12, 64, -8）"}] * 1)
    long_note = pits_text([{"filled": False, "note": "这个备注特别长" * 10}])
    assert long_note is not None and "…" in long_note


# ---------------------------------------------------------------------------
# goal_mining_and_pits_notes
# ---------------------------------------------------------------------------


def test_goal_notes_missing_details_is_empty() -> None:
    assert goal_mining_and_pits_notes(None) == {}
    assert goal_mining_and_pits_notes({"status": "done"}) == {}
    assert goal_mining_and_pits_notes({"details": "不是对象"}) == {}


def test_goal_notes_empty_details_is_empty() -> None:
    assert goal_mining_and_pits_notes({"details": {}}) == {}
    assert goal_mining_and_pits_notes({"details": {"chain_mining": {}, "pits": []}}) == {}


def test_goal_notes_normal_values_become_short_notes() -> None:
    result: dict[str, Any] = {
        "status": "done",
        "details": {
            "chain_mining": {"chained_batches": 2, "chained_blocks": 10, "single_blocks": 0},
            "pits": [{"filled": True, "depth": 3, "note": "填了泥土"}],
        },
    }
    assert goal_mining_and_pits_notes(result) == {
        "chain_mining": "连锁 2 批共 10 格",
        "pits": "顺手回填留坑 1 处（深 3 格：填了泥土）",
    }
