"""角色眼下在哪个 Minecraft 存档里：从 mod 报的 ``world`` 认出存档，判定"换了存档"。

mod 在 ``events`` 每一页与 ``observe(self)`` 里都可以带 ``world``：
``{"id", "name", "source", "online_mode"}``。``id`` 同一个存档重进、重启游戏都不变；
``name`` 只给人看；``online_mode`` 说这台服务器验不验正版账号，本模块把它读成三种状态，
只认报出来的 ``"on"`` 与 ``"off"``，其余一律 ``"unknown"``，不猜。

- 旧版 mod：还没见过带 ``world`` 键的事件页——报不出存档，记忆照旧不分存档；
- 不明：``world`` 为 null（在主菜单、刚进世界还没认出），或还没读到过任何一页；
  见过带这个键的页之后某页又缺了它也算不明——mod 不在世界里时 ``world`` 是 null，
  Gson 默认不写 null 字段，整个键就没了；
- 认出：``world.id`` 非空。

"换了存档"只认"上一个认出的编号被另一个认出的编号取代"：回主菜单再进同一个存档不算换；
不看 ``stream_id``（重进同一个世界它也会变）。进程启动后第一次认出不算换，只是认出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from maicraft.utils.logger import get_logger

__all__ = ["WorldChange", "WorldIdentity", "WorldRef", "world_ref_of"]

logger = get_logger("WorldIdentity")

# 账号验证状态：mod 报 "on" 或 "off"，其余（没报、认不得的值）一律按 unknown，不猜。
OnlineMode = Literal["on", "off", "unknown"]

# 存档名进提示词与标签前截到这个长度（服务器列表名可以很长）
_NAME_MAX_CHARS = 32


@dataclass(frozen=True)
class WorldRef:
    """一个认出的存档：编号只做相等比较，名字只显示；账号验证状态不参与比较存档换没换。"""

    id: str
    name: str = ""
    online_mode: OnlineMode = "unknown"

    def label(self) -> str:
        """给人看的称呼：有名字用名字，没有名字用编号前几位。"""
        return self.name or f"编号 {self.id[:8]}"


@dataclass(frozen=True)
class WorldChange:
    """一次存档变化：``before`` 为 None 是进程启动后第一次认出，不算换存档。"""

    before: WorldRef | None
    after: WorldRef

    @property
    def switched(self) -> bool:
        """是否从一个认出的存档换到了另一个。"""
        return self.before is not None and self.before.id != self.after.id


def _online_mode_of(raw: Any) -> OnlineMode:
    if raw == "on" or raw == "off":
        return raw
    return "unknown"


def world_ref_of(raw: Any) -> WorldRef | None:
    """读 mod 的 ``world`` 对象；null、形状不对或编号为空都按"不明"返回 None。"""
    if not isinstance(raw, Mapping):
        return None
    world_id = raw.get("id")
    if not isinstance(world_id, str) or not world_id.strip():
        return None
    name = raw.get("name")
    clean_name = " ".join(name.split())[:_NAME_MAX_CHARS] if isinstance(name, str) else ""
    return WorldRef(id=world_id.strip(), name=clean_name, online_mode=_online_mode_of(raw.get("online_mode")))


class WorldIdentity:
    """存档身份记录：喂入 mod 回复，给出眼下的存档与"换没换"。"""

    def __init__(self) -> None:
        # None = 还没读到过回复（不知道 mod 报不报存档）；True = 旧版 mod，报不出存档
        self._legacy: bool | None = None
        # 眼下认出的存档；None = 不明（主菜单、刚进世界、还没读到）
        self._current: WorldRef | None = None
        # 最近一次认出的存档：回主菜单时保留，再进别的存档时据此判定"换了"
        self._last_known: WorldRef | None = None

    @property
    def legacy(self) -> bool:
        """是否旧版 mod（回复里没有 ``world`` 键）。"""
        return bool(self._legacy)

    @property
    def current(self) -> WorldRef | None:
        """眼下认出的存档；不明或旧版 mod 为 None。"""
        return self._current

    def absorb(self, data: Mapping[str, Any]) -> WorldChange | None:
        """喂入一页 ``events`` 的 data；认出了新的存档时返回变化。

        是不是旧版 mod 只由事件页判定：还没见过带 ``world`` 键的页时，页里没有这个键就是旧版，
        记一次、此后一直按不分存档处理，直到某一页带上了这个键（mod 升级后重连）。
        见过带这个键的页之后再缺它，是 mod 不在世界里、把 null 字段省掉了，按 null（不在世界里）处理。
        """
        if "world" not in data:
            if self._legacy is False:
                # mod 回主菜单、崩溃重载世界的这一段，world 为 null 被 Gson 省掉：
                # 记成不在世界里（存档不明），不退回旧版 mod，重进世界后照常认出、照常判换没换
                return self._take(None)
            if self._legacy is not True:
                logger.info("事件页里没有 world：按旧版 mod 处理，记忆不分存档")
            self._legacy = True
            self._current = None
            return None
        self._legacy = False
        return self._take(data.get("world"))

    def _take(self, raw: Any) -> WorldChange | None:
        """记下眼下的存档；认出的编号与上一个认出的不同时返回变化（null 只算回主菜单）。"""
        ref = world_ref_of(raw)
        self._current = ref
        if ref is None:
            return None
        before = self._last_known
        self._last_known = ref
        if before is not None and before.id == ref.id:
            return None
        return WorldChange(before=before, after=ref)
