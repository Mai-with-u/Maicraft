"""模型写给自己的笔记和待办：每个 Minecraft 存档一份，落成 JSON 文件。

mod 用 64 位十六进制的存档编号区分存档，直接当文件名。还没认出存档时写下的内容
留在内存里没有归属，第一次认出存档时归它；读到的文件不覆盖本次运行里已写下的内容。
落盘先写临时文件再替换，避免写一半留下坏文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maicraft.utils.logger import get_logger

__all__ = ["MAX_NOTEBOOK_CHARS", "Notes", "TodoItem", "WorldNotes"]

logger = get_logger("Notes")

# 笔记本字符数上限：超过就截断
MAX_NOTEBOOK_CHARS = 2000

# 待办状态合法取值，其余一律按 pending
_TODO_STATUSES = ("pending", "in_progress", "done")


@dataclass(slots=True)
class TodoItem:
    """一条待办。"""

    content: str
    status: str = "pending"


@dataclass(slots=True)
class WorldNotes:
    """一个存档的笔记和待办。"""

    notebook: str = ""
    todos: list[TodoItem] = field(default_factory=list)


def _todo_from_dict(item: dict[str, Any]) -> TodoItem:
    status = item.get("status")
    return TodoItem(
        content=str(item.get("content", "")),
        status=status if status in _TODO_STATUSES else "pending",
    )


class Notes:
    """按存档隔离的笔记与待办，写在 ``directory`` 下的 ``<存档编号>.json``。"""

    def __init__(self, directory: Path, *, max_notebook_chars: int = MAX_NOTEBOOK_CHARS) -> None:
        self._directory = directory
        self._max_notebook_chars = max_notebook_chars
        self._world_id: str | None = None
        self._notes = WorldNotes()
        # 还没认出存档时写下的内容，第一次认出存档时归它
        self._unassigned = WorldNotes()

    @property
    def world_id(self) -> str:
        """当前存档编号，还没认出存档时是空字符串。"""
        return self._world_id or ""

    def current(self) -> WorldNotes:
        """当前生效的笔记和待办。"""
        return self._notes

    def use_world(self, world_id: str) -> None:
        """换到某个存档的那一份。

        没归属的内容按字段归到这个存档：内存里那个字段非空就用内存里的，否则用文件里的。
        文件读不出来（坏了）时记一条警告，从空的开始。
        """
        self._world_id = world_id
        loaded = self._load(world_id)
        if self._unassigned.notebook:
            loaded.notebook = self._unassigned.notebook
        if self._unassigned.todos:
            loaded.todos = self._unassigned.todos
        self._unassigned = WorldNotes()
        self._notes = loaded
        self._save()

    def set_notebook(self, content: str) -> None:
        """全量覆盖笔记本，超过上限截断；落盘到当前存档，还没认出存档时只留在内存里。"""
        self._notes.notebook = content[: self._max_notebook_chars]
        self._save_or_hold()

    def set_todos(self, items: list[dict[str, Any]]) -> None:
        """全量覆盖待办；``status`` 只认 pending/in_progress/done，其余按 pending。落盘规则同笔记本。"""
        self._notes.todos = [_todo_from_dict(item) for item in items]
        self._save_or_hold()

    def document(self) -> dict[str, Any]:
        """导出文件同款结构：``{"notebook": str, "todos": [{"content", "status"}]}``。"""
        return {
            "notebook": self._notes.notebook,
            "todos": [{"content": t.content, "status": t.status} for t in self._notes.todos],
        }

    def _load(self, world_id: str) -> WorldNotes:
        path = self._path_for(world_id)
        if not path.exists():
            return WorldNotes()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            notes = WorldNotes(notebook=str(data.get("notebook", "")))
            notes.todos = [_todo_from_dict(item) for item in data.get("todos", [])]
            return notes
        except (OSError, ValueError, AttributeError):
            logger.warning("笔记文件读不出来，按空的开头：%s", path)
            return WorldNotes()

    def _save_or_hold(self) -> None:
        if self._world_id is not None:
            self._save()
        else:
            self._unassigned = WorldNotes(
                notebook=self._notes.notebook,
                todos=list(self._notes.todos),
            )

    def _save(self) -> None:
        assert self._world_id is not None
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for(self._world_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.document(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _path_for(self, world_id: str) -> Path:
        return self._directory / f"{world_id}.json"
