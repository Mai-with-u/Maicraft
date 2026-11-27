"""本地工具直测：笔记读写、读技能、按编号取回收起的结果。"""

from __future__ import annotations

from pathlib import Path

from maicraft.agent.context import ContextStore
from maicraft.agent.local_tools import LOCAL_TOOL_NAMES, LocalTools, local_tool_definitions
from maicraft.agent.notes import Notes
from maicraft.agent.skills import SkillCatalog
from tests.fakes import FakeMod, FakeModEndpoint


def _tools(tmp_path: Path) -> LocalTools:
    notes = Notes(tmp_path / "notes")
    catalog = SkillCatalog(FakeModEndpoint(FakeMod()))
    return LocalTools(notes, catalog, ContextStore())


def test_local_tool_definitions_names_and_parameters() -> None:
    definitions = local_tool_definitions()
    assert [item["function"]["name"] for item in definitions] == list(LOCAL_TOOL_NAMES)
    notes = definitions[0]["function"]["parameters"]["properties"]
    assert notes["notebook"]["type"] == ["string", "null"]
    assert notes["todos"]["type"] == ["array", "null"]
    assert "换存档后是另一份" in definitions[0]["function"]["description"]
    for item in definitions:
        assert item["type"] == "function"
        assert item["function"]["parameters"]["type"] == "object"


async def test_notes_read_returns_document_json(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    result = await tools.call("notes", {})
    assert '"notebook":""' in result.replace(" ", "")
    assert '"todos":[]' in result.replace(" ", "")


async def test_notes_write_overrides_and_invalid_status_falls_back(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    await tools.call("notes", {"notebook": "家里的坐标", "todos": [{"content": "挖矿", "status": "in_progress"}]})
    result = await tools.call("notes", {})
    assert "家里的坐标" in result
    assert '"status":"in_progress"' in result.replace(" ", "")
    await tools.call("notes", {"todos": [{"content": "回家", "status": "怪状态"}]})
    result = await tools.call("notes", {})
    assert "家里的坐标" in result  # 只给 todos 时笔记本不动
    assert '"status":"pending"' in result.replace(" ", "")


async def test_read_skill_needs_refreshed_catalog(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    # 目录还没刷新时找不到技能，返回说明文字
    assert "没有名为" in await tools.call("read_skill", {"name": "survival-opening"})


async def test_read_skill_returns_text(tmp_path: Path) -> None:
    notes = Notes(tmp_path / "notes")
    catalog = SkillCatalog(FakeModEndpoint(FakeMod()))
    await catalog.refresh()
    tools = LocalTools(notes, catalog, ContextStore())
    result = await tools.call("read_skill", {"name": "survival-opening"})
    assert "生存开局" in result
    assert "没有名为" in await tools.call("read_skill", {"name": "nope"})


async def test_recall_returns_kept_text_or_explanation(tmp_path: Path) -> None:
    store = ContextStore()
    notes = Notes(tmp_path / "notes")
    tools = LocalTools(notes, SkillCatalog(FakeModEndpoint(FakeMod())), store)
    long_text = "很长" * 20000
    handle = store.keep("observe", {}, long_text, call_id="call-1")
    assert handle == "r1"
    assert await tools.call("recall", {"handle": "r1"}) == long_text
    assert "没有编号为 r9" in await tools.call("recall", {"handle": "r9"})


async def test_unknown_tool_name_gets_explanation(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    assert "没有名为 write_file 的本地工具" in await tools.call("write_file", {})
