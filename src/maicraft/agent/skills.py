"""mod 发布的技能：从 MCP 资源清单里认出 ``skill://`` 资源，按名字读正文。

技能回答"一类目标怎么一步步做成"，正文是一篇 SKILL.md。清单在连上后刷新一次，
读失败时保留上一次的清单，不让一次网络问题清空模型能看到的技能目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from maicraft.utils.logger import get_logger

__all__ = ["Skill", "SkillCatalog"]


# 技能资源的地址前缀与正文文件名
_SKILL_PREFIX = "skill://"
_SKILL_FILENAME = "SKILL.md"


class ResourceReader(Protocol):
    """能列资源清单、按地址读内容的对象（``ModClient`` 满足）。"""

    async def list_resources(self) -> list[Any]:
        """列出资源清单。"""
        ...

    async def read_resource(self, uri: str) -> list[Any]:
        """按地址读资源，返回内容项列表。"""
        ...


@dataclass(frozen=True, slots=True)
class Skill:
    """一项技能：地址、名字与一句说明。"""

    uri: str
    name: str
    description: str


def _name_of(uri: str, server_name: Any) -> str:
    """技能名以服务器给的为准；缺了取地址末段的目录名（SKILL.md 前那一段）。"""
    name = str(server_name or "").strip()
    if name:
        return name
    path = uri[len(_SKILL_PREFIX) :].split("/")
    if len(path) >= 2 and path[-1] == _SKILL_FILENAME:
        return path[-2]
    return path[-1] if path and path[-1] else uri


class SkillCatalog:
    """mod 发布的技能目录。所有方法都要在事件循环里调用。"""

    def __init__(self, mod: ResourceReader, *, logger: Any | None = None) -> None:
        self._mod = mod
        self._logger = logger if logger is not None else get_logger("SkillCatalog")
        self._skills: list[Skill] = []

    async def refresh(self) -> bool:
        """重新取资源清单，只留 ``skill://`` 开头的；失败时保留上一次的清单并返回 False。"""
        try:
            resources = await self._mod.list_resources()
        except Exception as exc:  # noqa: BLE001 - 清单读不到时保留旧目录，不打断调用方
            self._logger.warning(f"读取技能资源清单失败，沿用上一次的目录：{type(exc).__name__}: {exc}")
            return False
        skills: list[Skill] = []
        for resource in resources:
            uri = str(getattr(resource, "uri", "") or "")
            if not uri.startswith(_SKILL_PREFIX):
                continue
            skills.append(
                Skill(
                    uri=uri,
                    name=_name_of(uri, getattr(resource, "name", "")),
                    description=str(getattr(resource, "description", "") or ""),
                )
            )
        self._skills = skills
        return True

    def all(self) -> list[Skill]:
        return list(self._skills)

    def find(self, name_or_uri: str) -> Skill | None:
        """按名字或地址找一项技能。"""
        target = name_or_uri.strip()
        for skill in self._skills:
            if skill.name == target or skill.uri == target:
                return skill
        return None

    def catalog_text(self) -> str:
        """技能目录文本，每项一行"名字：说明"，放进提示词；没有技能时为空串。"""
        return "\n".join(f"{skill.name}：{skill.description}" for skill in self._skills)

    async def read(self, name_or_uri: str) -> str:
        """读技能正文；找不到或读失败时返回一句说明原因的文字，不抛异常。"""
        skill = self.find(name_or_uri)
        if skill is None:
            return f"没有名为 {name_or_uri} 的技能，可用技能见消息里的技能目录。"
        try:
            contents = await self._mod.read_resource(skill.uri)
        except Exception as exc:  # noqa: BLE001 - 读取失败时给模型一句可读的说明
            return f"技能 {skill.name} 读不出来（{type(exc).__name__}），稍后再试。"
        for item in contents:
            text = getattr(item, "text", None)
            if text:
                return str(text)
        return f"技能 {skill.name} 的内容是空的。"
