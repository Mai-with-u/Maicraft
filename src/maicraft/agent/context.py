"""一轮对话里工具结果的收起与取回：结果太长或太旧时换成占位，原文按编号留着。

编号是 ``"r1"``、``"r2"``……，模型在提示词里看到占位后可以用 ``recall`` 取回原文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ContextStore", "KeptResult", "shorten_tool_result", "trim_tool_messages"]


@dataclass(frozen=True, slots=True)
class KeptResult:
    """一条收起的工具结果原文。"""

    handle: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    text: str


class ContextStore:
    """一轮对话里收起的工具结果原文，按编号存放与取回。"""

    def __init__(self) -> None:
        self._results: dict[str, KeptResult] = {}
        self._handle_by_call_id: dict[str, str] = {}
        self._next_number = 1

    def keep(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        text: str,
        *,
        call_id: str,
    ) -> str:
        """保存一条原文，返回编号 ``"r1"``、``"r2"``……。"""
        handle = f"r{self._next_number}"
        self._next_number += 1
        self._results[handle] = KeptResult(
            handle=handle,
            call_id=call_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            text=text,
        )
        self._handle_by_call_id[call_id] = handle
        return handle

    def handle_for(self, call_id: str) -> str | None:
        """按工具调用的 ``call_id`` 查编号，查不到返回 None。"""
        return self._handle_by_call_id.get(call_id)

    def get(self, handle: str) -> KeptResult | None:
        """按编号取回原文，查不到返回 None。"""
        return self._results.get(handle)

    def clear(self) -> None:
        """丢弃全部收起的内容，编号重新从 ``"r1"`` 起算。"""
        self._results = {}
        self._handle_by_call_id = {}
        self._next_number = 1

    def __len__(self) -> int:
        return len(self._results)


def shorten_tool_result(
    text: str,
    *,
    max_result_chars: int,
    store: ContextStore,
    tool_name: str,
    arguments: dict[str, Any],
    call_id: str,
) -> str:
    """一条工具结果超过 ``max_result_chars`` 时收起原文、返回截断占位；不长的原样返回。"""
    if len(text) <= max_result_chars:
        return text
    handle = store.keep(tool_name, arguments, text, call_id=call_id)
    kept = store.get(handle)
    assert kept is not None
    head = text[:max_result_chars]
    return f'{head}\n……（全文 {len(kept.text)} 字已收起，用 recall("{handle}") 取回）'


def trim_tool_messages(
    messages: list[dict[str, Any]],
    store: ContextStore,
    *,
    keep_recent: int,
) -> list[dict[str, Any]]:
    """把较早的工具结果换成占位，返回新列表，不改动传入的列表。

    ``role == "tool"`` 的消息里最近 ``keep_recent`` 条保持原样；更早的按 ``tool_call_id``
    从 ``store`` 里查编号和工具名，查不到就用不带名字的占位。其他角色原样保留。
    """
    tool_indexes = [i for i, msg in enumerate(messages) if msg.get("role") == "tool"]
    stale_count = len(tool_indexes) - keep_recent
    stale_indexes = set(tool_indexes[:stale_count])
    result: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i not in stale_indexes:
            result.append(dict(msg))
            continue
        call_id = str(msg.get("tool_call_id", ""))
        handle = store.handle_for(call_id) if call_id else None
        kept = store.get(handle) if handle else None
        if handle and kept:
            content = f'[较早的结果已收起：{kept.tool_name}，要原文用 recall("{handle}") 取回]'
        else:
            content = "[较早的结果已收起]"
        result.append(dict(msg, content=content))
    return result
