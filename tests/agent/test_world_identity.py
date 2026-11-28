"""认出所在存档：旧版 mod 不分存档、主菜单不算换、账号验证不猜。"""

from __future__ import annotations

from typing import Any

from maicraft.agent.world_identity import WorldIdentity, WorldRef, world_ref_of


def _world(world_id: str, name: str = "") -> dict[str, Any]:
    return {"world": {"id": world_id, "name": name, "source": "world_id_file"}}


def test_old_mod_without_world_key_reports_no_save() -> None:
    """回复里没有 world 键是旧版 mod：报不出存档。"""
    identity = WorldIdentity()
    assert identity.absorb({"stream_id": "s"}) is None
    assert identity.legacy
    assert identity.current is None


def test_unknown_until_identified_then_first_identification_is_not_a_switch() -> None:
    """还没读到或 world 为 null 时存档不明；进程启动后第一次认出只是认出，不算换。"""
    identity = WorldIdentity()
    assert identity.current is None
    assert identity.absorb({"world": None}) is None
    assert identity.current is None

    change = identity.absorb(_world("aaa", "老家"))

    assert change is not None and change.before is None and not change.switched
    assert identity.current == WorldRef(id="aaa", name="老家")


def test_main_menu_and_reentering_same_world_is_not_a_switch() -> None:
    """回主菜单（null）再进同一个存档：不算换；进另一个存档才算，before 是上一个认出的。"""
    identity = WorldIdentity()
    identity.absorb(_world("aaa", "老家"))
    assert identity.absorb({"world": None}) is None
    assert identity.current is None
    assert identity.absorb(_world("aaa", "老家")) is None

    identity.absorb({"world": None})
    change = identity.absorb(_world("bbb", "新的世界"))

    assert change is not None and change.switched
    assert change.before is not None and change.before.label() == "老家"
    assert change.after.label() == "新的世界"


def test_page_missing_world_after_seeing_it_means_not_in_world() -> None:
    """见过带 world 的页之后，某页缺了这个键：mod 不在世界里时 world 为 null、Gson 不写 null 字段，
    按不在世界里处理（存档不明），不退回旧版 mod；重进同一个存档不算换，进别的存档照常算换。"""
    identity = WorldIdentity()
    identity.absorb(_world("aaa", "老家"))

    assert identity.absorb({"stream_id": "s"}) is None
    assert not identity.legacy
    assert identity.current is None

    assert identity.absorb(_world("aaa", "老家")) is None
    assert identity.current == WorldRef(id="aaa", name="老家")

    identity.absorb({"stream_id": "s"})
    change = identity.absorb(_world("bbb", "新的世界"))
    assert change is not None and change.switched and change.before is not None
    assert change.before.label() == "老家"


def test_world_null_page_also_counts_as_seeing_the_key() -> None:
    """头一页就带 world: null 也说明是新版 mod：之后缺键同样按不在世界里，不当旧版。"""
    identity = WorldIdentity()
    identity.absorb({"world": None})

    assert identity.absorb({"stream_id": "s"}) is None
    assert not identity.legacy
    assert identity.current is None


def test_world_ref_reading_tolerates_bad_shapes() -> None:
    """编号为空或形状不对按不明；名字压成一行、截短；没有名字时用编号前几位称呼。"""
    assert world_ref_of(None) is None
    assert world_ref_of({"id": "  "}) is None
    assert world_ref_of("aaa") is None
    ref = world_ref_of({"id": "0123456789abcdef", "name": "  新的\n世界  "})
    assert ref is not None and ref.name == "新的 世界"
    nameless = world_ref_of({"id": "0123456789abcdef"})
    assert nameless is not None and nameless.label() == "编号 01234567"


def test_online_mode_is_read_and_never_guessed() -> None:
    """账号验证照报的读（on / off / unknown）；没报、报了认不得的值都按 unknown，不参与判换存档。"""
    reads = [world_ref_of({"id": "w1", "online_mode": raw}) for raw in ("on", "off", "unknown", "OFF", None)]
    assert [ref.online_mode for ref in reads if ref is not None] == ["on", "off", "unknown", "unknown", "unknown"]
    assert world_ref_of({"id": "w1"}) == WorldRef(id="w1", online_mode="unknown")
