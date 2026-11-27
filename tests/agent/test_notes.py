"""笔记与待办直测：落盘与读回、换存档隔离、没归属的内容归第一个存档、截断与坏文件。"""

from __future__ import annotations

import json
from typing import Any

from maicraft.agent.notes import MAX_NOTEBOOK_CHARS, Notes


def _read_file(directory: Any, world_id: str) -> dict[str, Any]:
    return json.loads((directory / f"{world_id}.json").read_text(encoding="utf-8"))


def test_save_and_reload(tmp_path) -> None:
    notes = Notes(tmp_path)
    notes.use_world("a" * 64)
    notes.set_notebook("家在出生点附近")
    notes.set_todos(
        [
            {"content": "挖三组铁", "status": "in_progress"},
            {"content": "造一张床", "status": "done"},
        ]
    )
    assert notes.world_id == "a" * 64
    assert _read_file(tmp_path, "a" * 64) == {
        "notebook": "家在出生点附近",
        "todos": [
            {"content": "挖三组铁", "status": "in_progress"},
            {"content": "造一张床", "status": "done"},
        ],
    }

    reloaded = Notes(tmp_path)
    reloaded.use_world("a" * 64)
    assert reloaded.current().notebook == "家在出生点附近"
    assert [t.content for t in reloaded.current().todos] == ["挖三组铁", "造一张床"]
    assert [t.status for t in reloaded.current().todos] == ["in_progress", "done"]


def test_worlds_are_isolated(tmp_path) -> None:
    notes = Notes(tmp_path)
    notes.use_world("a" * 64)
    notes.set_notebook("存档甲的笔记")
    notes.use_world("b" * 64)
    notes.set_notebook("存档乙的笔记")
    assert notes.current().notebook == "存档乙的笔记"

    notes.use_world("a" * 64)
    assert notes.current().notebook == "存档甲的笔记"
    assert _read_file(tmp_path, "a" * 64)["notebook"] == "存档甲的笔记"
    assert _read_file(tmp_path, "b" * 64)["notebook"] == "存档乙的笔记"


def test_unassigned_content_belongs_to_first_world(tmp_path) -> None:
    notes = Notes(tmp_path)
    notes.set_notebook("还没认出存档就写下的")
    notes.set_todos([{"content": "问主人去哪"}])
    assert _read_file_names(tmp_path) == []

    notes.use_world("a" * 64)
    assert notes.current().notebook == "还没认出存档就写下的"
    assert [t.content for t in notes.current().todos] == ["问主人去哪"]
    assert _read_file_names(tmp_path) == [f"{'a' * 64}.json"]

    # 归属之后切走再回来，内容跟着存档走，不再二次合并
    notes.set_notebook("存档甲的笔记")
    notes.use_world("b" * 64)
    notes.use_world("a" * 64)
    assert notes.current().notebook == "存档甲的笔记"


def _read_file_names(directory: Any) -> list[str]:
    return sorted(p.name for p in directory.glob("*.json"))


def test_set_todos_rejects_unknown_status(tmp_path) -> None:
    notes = Notes(tmp_path)
    notes.use_world("a" * 64)
    notes.set_todos([{"content": "挖矿", "status": "someday"}])
    assert notes.current().todos[0].status == "pending"


def test_notebook_truncated(tmp_path) -> None:
    notes = Notes(tmp_path, max_notebook_chars=10)
    notes.use_world("a" * 64)
    notes.set_notebook("字" * (MAX_NOTEBOOK_CHARS + 5))
    assert len(notes.current().notebook) == 10
    assert _read_file(tmp_path, "a" * 64)["notebook"] == "字" * 10


def test_broken_file_starts_empty(tmp_path) -> None:
    (tmp_path / "a.json").write_text("{不是 JSON", encoding="utf-8")
    notes = Notes(tmp_path)
    notes.use_world("a")
    assert notes.current().notebook == ""
    assert notes.current().todos == []


def test_document_export(tmp_path) -> None:
    notes = Notes(tmp_path)
    notes.use_world("a" * 64)
    notes.set_notebook("笔记")
    notes.set_todos([{"content": "挖矿"}])
    assert notes.document() == {
        "notebook": "笔记",
        "todos": [{"content": "挖矿", "status": "pending"}],
    }
