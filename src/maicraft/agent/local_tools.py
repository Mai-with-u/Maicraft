"""程序自己加给模型的三个本地工具：笔记、读技能、取回收起的结果。

它们不走 mod，直接读程序自己的状态；工具定义用 OpenAI 的 function 格式，
和 mod 的四个工具放在一起交给模型。调用结果一律返回给模型看的文字，不抛异常。
"""

from __future__ import annotations

from typing import Any

from .context import ContextStore
from .goal_registry import json_text
from .notes import Notes
from .skills import SkillCatalog

__all__ = ["LOCAL_TOOL_NAMES", "LocalTools", "local_tool_definitions"]

LOCAL_TOOL_NAMES = ("notes", "read_skill", "recall")


def local_tool_definitions() -> list[dict[str, Any]]:
    """三个本地工具的 OpenAI 工具格式定义。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "notes",
                "description": (
                    "读写当前存档的笔记和待办，按存档跨重启保留。notebook 与 todos 都给时全量覆盖两者，"
                    "只给一个时只覆盖那一个；两个都不给就是读取当前内容。"
                    "这是当前存档的笔记，换存档后是另一份。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "notebook": {
                            "type": ["string", "null"],
                            "description": "笔记本全文，全量覆盖；不给或为 null 表示不改",
                        },
                        "todos": {
                            "type": ["array", "null"],
                            "description": "待办全文，全量覆盖；不给或为 null 表示不改",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string", "description": "要做的事"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "done"],
                                        "description": "推进状态",
                                    },
                                },
                                "required": ["content", "status"],
                            },
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_skill",
                "description": "按名字读一篇 mod 发布的技能文档。可用技能的目录在每轮消息里。",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "技能名，见技能目录"}},
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall",
                "description": '按编号取回被收起的工具结果原文，编号形如 "r3"，写在收起时的占位说明里。',
                "parameters": {
                    "type": "object",
                    "properties": {"handle": {"type": "string", "description": '收起结果的编号，如 "r3"'}},
                    "required": ["handle"],
                },
            },
        },
    ]


class LocalTools:
    """执行三个本地工具调用，返回给模型看的文字。"""

    def __init__(self, notes: Notes, skills: SkillCatalog, store: ContextStore) -> None:
        self._notes = notes
        self._skills = skills
        self._store = store

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "notes":
            return self._notes_tool(arguments)
        if name == "read_skill":
            return await self._skills.read(str(arguments.get("name") or ""))
        if name == "recall":
            return self._recall_tool(str(arguments.get("handle") or ""))
        return f"没有名为 {name} 的本地工具。"

    def _notes_tool(self, arguments: dict[str, Any]) -> str:
        notebook = arguments.get("notebook")
        todos = arguments.get("todos")
        if isinstance(notebook, str):
            self._notes.set_notebook(notebook)
        if isinstance(todos, list):
            self._notes.set_todos(todos)
        return json_text(self._notes.document())

    def _recall_tool(self, handle: str) -> str:
        kept = self._store.get(handle)
        if kept is None:
            return f"没有编号为 {handle} 的收起结果。编号写在收起时的占位说明里，当前这一轮里收起了 {len(self._store)} 条。"
        return kept.text
