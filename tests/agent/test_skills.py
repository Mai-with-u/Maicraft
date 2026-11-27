"""技能目录直测：只认 skill:// 资源、名字缺了从地址取、读失败保留旧目录、按名字或地址找。"""

from __future__ import annotations

import mcp_types

from maicraft.agent.skills import SkillCatalog
from tests.fakes import SKILL_TEXT, SKILL_URI, FakeMod, FakeModEndpoint


class _FlakyReader:
    """清单读取可以切换成败的替身。"""

    def __init__(self, resources: list[mcp_types.Resource], fail: bool = False) -> None:
        self.resources = resources
        self.fail = fail

    async def list_resources(self) -> list[mcp_types.Resource]:
        if self.fail:
            raise RuntimeError("连不上")
        return self.resources

    async def read_resource(self, uri: str) -> list[mcp_types.TextResourceContents]:
        if self.fail:
            raise RuntimeError("连不上")
        return [mcp_types.TextResourceContents(uri=uri, text=SKILL_TEXT)]


def _resource(uri: str, name: str = "", description: str = "说明") -> mcp_types.Resource:
    return mcp_types.Resource(uri=uri, name=name, description=description, mime_type="text/markdown")


async def test_refresh_keeps_only_skill_resources() -> None:
    endpoint = FakeModEndpoint(FakeMod())
    catalog = SkillCatalog(endpoint)
    assert await catalog.refresh() is True
    names = [skill.name for skill in catalog.all()]
    assert names == ["survival-opening"]
    assert catalog.all()[0].uri == SKILL_URI


async def test_refresh_filters_out_non_skill_resources() -> None:
    reader = _FlakyReader([_resource(SKILL_URI, "survival-opening"), _resource("config://mod/settings", "settings")])
    catalog = SkillCatalog(reader)
    assert await catalog.refresh() is True
    assert [skill.name for skill in catalog.all()] == ["survival-opening"]


async def test_name_falls_back_to_last_directory_in_uri() -> None:
    reader = _FlakyReader([_resource("skill://building/house-basics/SKILL.md")])
    catalog = SkillCatalog(reader)
    await catalog.refresh()
    assert catalog.all()[0].name == "house-basics"
    assert catalog.all()[0].description == "说明"


async def test_refresh_failure_keeps_previous_catalog_and_returns_false() -> None:
    reader = _FlakyReader([_resource(SKILL_URI, "survival-opening")])
    catalog = SkillCatalog(reader)
    await catalog.refresh()
    reader.fail = True
    assert await catalog.refresh() is False
    assert [skill.name for skill in catalog.all()] == ["survival-opening"]


async def test_find_by_name_and_by_uri() -> None:
    catalog = SkillCatalog(FakeModEndpoint(FakeMod()))
    await catalog.refresh()
    assert catalog.find("survival-opening") is not None
    assert catalog.find(SKILL_URI) is not None
    assert catalog.find("没有的技能") is None


async def test_catalog_text_lines_and_empty_catalog() -> None:
    endpoint = FakeModEndpoint(FakeMod())
    catalog = SkillCatalog(endpoint)
    assert catalog.catalog_text() == ""
    await catalog.refresh()
    lines = catalog.catalog_text().splitlines()
    assert lines == ["survival-opening：skill://survival/survival-opening/SKILL.md 的技能"]


async def test_read_returns_text_and_explains_failures() -> None:
    catalog = SkillCatalog(FakeModEndpoint(FakeMod()))
    await catalog.refresh()
    assert "生存开局" in await catalog.read("survival-opening")
    assert "没有名为" in await catalog.read("nope")
    assert "没有名为" in await catalog.read("skill://survival/missing/SKILL.md")


async def test_read_failure_explains_when_connection_breaks() -> None:
    reader = _FlakyReader([_resource(SKILL_URI, "survival-opening")])
    catalog = SkillCatalog(reader)
    await catalog.refresh()
    reader.fail = True
    assert "读不出来" in await catalog.read("survival-opening")
