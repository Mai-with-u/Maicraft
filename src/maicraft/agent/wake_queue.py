"""唤醒队列：等模型处理的内容，分"立刻叫醒"和"下次顺带捎上"两种。

``wake`` 项让主循环马上叫醒模型；``carry`` 项不单独叫醒，只在下一次叫醒时捎给模型。
天色变化属于捎带，只留最新一条。
"""

from __future__ import annotations

__all__ = ["WakeQueue"]


class WakeQueue:
    """两种内容的先入先出队列：``wake`` 立刻叫醒，``carry`` 下次捎上。"""

    def __init__(self, *, max_carry: int = 12) -> None:
        self._wake: list[str] = []
        # 每项带一个标记，区分天色项（只留最新）与普通捎带项
        self._carry: list[tuple[str, bool]] = []
        self._max_carry = max_carry

    def push_wake(self, content: str) -> None:
        """加入一条要立刻叫醒模型的内容。"""
        self._wake.append(content)

    def push_carry(self, content: str) -> None:
        """加入一条下次叫醒时顺带捎上的内容。"""
        self._push_carry(content, is_sky=False)

    def replace_sky(self, content: str) -> None:
        """天色属于捎带且只留最新一条：丢掉旧的天色项，放入新的。"""
        self._carry = [(text, sky) for text, sky in self._carry if not sky]
        self._push_carry(content, is_sky=True)

    def _push_carry(self, content: str, *, is_sky: bool) -> None:
        self._carry.append((content, is_sky))
        while len(self._carry) > self._max_carry:
            self._carry.pop(0)

    def can_wake(self) -> bool:
        """有没有要立刻叫醒的 ``wake`` 项。"""
        return bool(self._wake)

    def take_all(self) -> list[str]:
        """取走全部内容并清空：``wake`` 项在前、``carry`` 项在后，各自按先后；完全相同的内容只留一条。"""
        combined = self._wake + [text for text, _ in self._carry]
        self._wake = []
        self._carry = []
        seen: set[str] = set()
        result: list[str] = []
        for text in combined:
            if text not in seen:
                seen.add(text)
                result.append(text)
        return result

    def clear(self) -> None:
        """丢弃全部内容。"""
        self._wake = []
        self._carry = []

    def __len__(self) -> int:
        return len(self._wake) + len(self._carry)

    def __bool__(self) -> bool:
        return bool(self._wake) or bool(self._carry)
