"""游戏公屏筛选与长话拆分：决定哪些公屏消息唤醒模型，以及超长发言怎么拆成几条发。

筛选部分的规则：
- 只有私聊和点名（原话里出现角色名字或配置的称呼）的消息进待唤醒，其余只进最近的聊天记录，
  防止全服刷屏把模型叫个不停；
- 落进同一个合批窗口（默认 2 秒）的待唤醒消息算一批，到点后整批取出，私聊在前、点名在后；
- 一批最多 ``max_batch_items`` 条，超出的丢弃；同一发言人同一批里最多 ``max_distinct_per_sender``
  条不同内容；同一人重复同一句话折叠为一条并记下次数（``ChatMessage.repeat``）；
- ``wake_window_ms`` 内唤醒次数达到 ``wake_cooldown_after`` 次后，进入 ``wake_cooldown_ms`` 的冷却，
  冷却期间待唤醒消息继续攒批，到点时刻至少推到上次唤醒加冷却时长之后。

拆分部分是纯函数：以 ``/`` 开头的是游戏命令、或长度不超过阈值时原样返回一条；否则按句末标点
和换行切，过长再按次级标点切，贪心合并成每条尽量不超过目标宽度的少量短句。
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "ChatFilter",
    "ChatMessage",
    "PUBLIC_CHAT_CHUNK_TARGET_CHARS",
    "PUBLIC_CHAT_MAX_CHUNKS",
    "PUBLIC_CHAT_MIN_CHUNK_CHARS",
    "PUBLIC_CHAT_SOFT_LENGTH_LIMIT",
    "PUBLIC_CHAT_SPLIT_THRESHOLD",
    "split_public_chat_message",
]

# 游戏公屏单条消息温和提醒的字数阈值（非阻断）：超过该字数时提醒模型拆开发
PUBLIC_CHAT_SOFT_LENGTH_LIMIT = 45
# 自动按句界拆发的阈值：不超过这个长度的消息整条发
PUBLIC_CHAT_SPLIT_THRESHOLD = 55
# 拆分时单条的期望宽度
PUBLIC_CHAT_CHUNK_TARGET_CHARS = 42
# 一条长话最多拆成几条
PUBLIC_CHAT_MAX_CHUNKS = 3
# 拆出的任何一条不短于这个字数
PUBLIC_CHAT_MIN_CHUNK_CHARS = 8

# 强句末标点与换行：拆超长公屏的第一层切分
_STRONG_SENTENCE_SPLIT_RE = re.compile(r"[^\n。！？!?…]+(?:[。！？!?…]+[”’」』）)\]]*)?|\n+")
# 次级停顿标点：仍超宽的原子句按它进一步细拆
_SECONDARY_CLAUSE_SPLIT_RE = re.compile(r"[^；;，,、：:]+(?:[；;，,、：:]+[”’」』）)\]]*)?")


def _sender_key(sender: str) -> str:
    """发言人归一化键（去空白、大小写不敏感）。"""
    return (sender or "").strip().casefold()


def _content_key(content: str) -> str:
    """消息正文去重键（折叠连续空白、大小写不敏感）。"""
    return " ".join((content or "").split()).casefold()


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """公屏事件流里的一条消息（字段含义见 MaiCraftMod 接口文档 2.5 节 ``topic=chat``）。"""

    cursor: int
    kind: str
    sender: str = ""
    sender_id: str = ""
    sender_online: bool | None = None
    sender_distance: float | None = None
    private: bool = False
    text: str = ""
    mentions: tuple[str, ...] = ()
    repeat: int = 1

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> ChatMessage:
        """从公屏事件流的一条事件读出消息；``mentions`` 只取每项的 ``name``。"""
        raw_distance = event.get("sender_distance")
        if isinstance(raw_distance, bool) or not isinstance(raw_distance, (int, float)):
            distance: float | None = None
        else:
            distance = float(raw_distance)
        online = event.get("sender_online")
        mentions = event.get("mentions") or ()
        return cls(
            cursor=int(event["cursor"]),
            kind=str(event["kind"]),
            sender=str(event.get("sender") or ""),
            sender_id=str(event.get("sender_id") or ""),
            sender_online=None if online is None else bool(online),
            sender_distance=None if distance is None or distance < 0 else distance,
            private=bool(event.get("private") or False),
            text=str(event.get("text") or ""),
            mentions=tuple(str(item.get("name") or "") for item in mentions if isinstance(item, dict)),
        )


@dataclass
class _PendingMessage:
    """合批窗口内的一条待唤醒消息（``repeat`` 随同人同文消息累加）。"""

    seq: int
    message: ChatMessage
    content_key: str
    repeat: int = 1


class ChatFilter:
    """游戏公屏筛选：合批待唤醒消息、防止刷屏频繁唤醒、维护最近的聊天记录。"""

    def __init__(
        self,
        *,
        names: Sequence[str],
        clock_ms: Callable[[], int],
        batch_window_ms: int = 2000,
        max_batch_items: int = 8,
        max_distinct_per_sender: int = 3,
        wake_window_ms: int = 30000,
        wake_cooldown_after: int = 3,
        wake_cooldown_ms: int = 6000,
        max_recent: int = 16,
    ) -> None:
        self._names = tuple(sorted({_content_key(name) for name in names if name and name.strip()}))
        self._clock_ms = clock_ms
        self._batch_window_ms = max(0, int(batch_window_ms))
        self._max_batch_items = max(1, int(max_batch_items))
        self._max_distinct_per_sender = max(1, int(max_distinct_per_sender))
        self._wake_window_ms = max(0, int(wake_window_ms))
        self._wake_cooldown_after = max(1, int(wake_cooldown_after))
        self._wake_cooldown_ms = max(0, int(wake_cooldown_ms))
        self._max_recent = max(1, int(max_recent))

        self._seq = 0
        # 当前合批窗口及其到点时刻（batch 为空表示没有打开的窗口）
        self._batch: list[_PendingMessage] = []
        self._batch_due_ms: int | None = None
        # 近期唤醒时刻与冷却状态：防止有人刷屏把模型叫个不停
        self._wake_times: deque[int] = deque()
        self._last_wake_ms: int | None = None
        self._cooldown_active = False
        # 最近的聊天记录（点名与没点名的都算，旧的在前）
        self._recent: deque[ChatMessage] = deque(maxlen=self._max_recent)

    def add(self, message: ChatMessage) -> None:
        """收到一条公屏消息：私聊和点名的进待唤醒合批；其余只进最近的聊天记录。"""
        now = int(self._clock_ms())
        self._remember_recent(message, now)

        if message.kind != "player" or not self._should_wake(message):
            return
        self._seq += 1
        incoming = _PendingMessage(seq=self._seq, message=message, content_key=_content_key(message.text))
        self._merge_into_batch(incoming)
        if self._batch_due_ms is None:
            self._batch_due_ms = self._compute_batch_due_ms(now)

    def next_wake_delay_ms(self) -> int | None:
        """最早一批待唤醒的消息还有多少毫秒到点；没有待唤醒的返回 ``None``，已到点返回 ``0``。"""
        if self._batch_due_ms is None or not self._batch:
            return None
        return max(0, self._batch_due_ms - int(self._clock_ms()))

    def take_due(self) -> list[ChatMessage]:
        """取走到点的一批：私聊在前、点名在后，各自按到达先后；没到点的和冷却期内的都留着。"""
        if self._batch_due_ms is None or not self._batch:
            return []
        now = int(self._clock_ms())
        if now < self._batch_due_ms:
            return []

        items = self._batch
        self._batch = []
        self._batch_due_ms = None
        private_first = sorted(items, key=lambda item: (0 if item.message.private else 1, item.seq))
        self._record_wake_event(now)
        return [replace(item.message, repeat=item.repeat) for item in private_first]

    def recent(self) -> list[ChatMessage]:
        """最近的聊天记录，旧的在前，最多 ``max_recent`` 条，点名与没点名的都算。"""
        return list(self._recent)

    def clear(self) -> None:
        """清空待唤醒合批、唤醒频次记录与最近的聊天记录。"""
        self._batch.clear()
        self._batch_due_ms = None
        self._wake_times.clear()
        self._last_wake_ms = None
        self._cooldown_active = False
        self._recent.clear()

    def _should_wake(self, message: ChatMessage) -> bool:
        """私聊或原话里出现角色名字 / 配置称呼（大小写不敏感）算点名。"""
        if message.private:
            return True
        text = _content_key(message.text)
        return bool(text) and any(name in text for name in self._names)

    def _compute_batch_due_ms(self, now: int) -> int:
        """计算合批窗口的到点时刻：常态为窗口宽，冷却期内至少推到冷却结束。"""
        due = now + self._batch_window_ms
        if self._last_wake_ms is None:
            return due
        # 长时间静默（超过两倍冷却期）自动解除冷却
        if now - self._last_wake_ms >= self._wake_cooldown_ms * 2:
            self._cooldown_active = False
            return due
        if self._cooldown_active:
            return max(due, self._last_wake_ms + self._wake_cooldown_ms)
        return due

    def _record_wake_event(self, now: int) -> None:
        """记录一次唤醒：``wake_window_ms`` 内达到 ``wake_cooldown_after`` 次后开启冷却。"""
        self._last_wake_ms = now
        self._wake_times.append(now)
        cutoff = now - self._wake_window_ms
        while self._wake_times and self._wake_times[0] < cutoff:
            self._wake_times.popleft()
        if len(self._wake_times) >= self._wake_cooldown_after:
            self._cooldown_active = True

    def _merge_into_batch(self, incoming: _PendingMessage) -> None:
        """把一条待唤醒消息并进当前合批窗口：同人同文折叠计数，超出各上限的丢弃。"""
        if len(self._batch) >= self._max_batch_items:
            return
        sender_norm = _sender_key(incoming.message.sender)
        same_sender = [item for item in self._batch if _sender_key(item.message.sender) == sender_norm]
        for existing in same_sender:
            # 私聊与公开消息不互相折叠，避免丢掉"这是私聊"的信息
            if existing.content_key == incoming.content_key and existing.message.private == incoming.message.private:
                existing.repeat += 1
                return
        if sender_norm and len(same_sender) >= self._max_distinct_per_sender:
            return
        self._batch.append(incoming)

    def _remember_recent(self, message: ChatMessage, now_ms: int) -> None:
        """写入最近的聊天记录；相邻同人同文只刷新次数与时刻，不重复占坑。"""
        content = _content_key(message.text)
        if not content:
            return
        if self._recent:
            last = self._recent[-1]
            if (
                _sender_key(last.sender) == _sender_key(message.sender)
                and _content_key(last.text) == content
                and last.kind == message.kind
                and last.private == message.private
            ):
                self._recent[-1] = replace(last, repeat=last.repeat + 1)
                return
        self._recent.append(message)


def _split_long_clause_safely(clause: str, *, max_len: int) -> list[str]:
    """对没有任何标点断口的极长片段按字宽分段，尽量避免在连续 ASCII 词或数字中间切断。"""
    text = clause.strip()
    if len(text) <= max_len:
        return [text] if text else []
    parts: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        remaining = total - start
        if remaining <= max_len:
            piece = text[start:].strip()
            if piece:
                parts.append(piece)
            break
        cut = start + max_len
        # 优先在切点前找空白符；若无空白且切点落在 ASCII 字母数字中间，向前回退避开断词
        space_pos = text.rfind(" ", start + max(1, max_len // 2), cut + 1)
        if space_pos > start:
            cut = space_pos
        elif (
            cut < total
            and text[cut - 1].isascii()
            and text[cut - 1].isalnum()
            and text[cut].isascii()
            and text[cut].isalnum()
        ):
            back = cut - 1
            while back > start + max(1, max_len // 2) and text[back - 1].isascii() and text[back - 1].isalnum():
                back -= 1
            if back > start + max(1, max_len // 2):
                cut = back
        piece = text[start:cut].strip()
        if piece:
            parts.append(piece)
        start = cut
        while start < total and text[start].isspace():
            start += 1
    return parts


def _pack_chat_units(units: Sequence[str], *, target_len: int, max_chunks: int) -> list[str]:
    """按目标宽度贪心合并相邻短句，控制在 ``max_chunks`` 条以内且避免末尾碎句。"""
    cleaned_units = [unit.strip() for unit in units if unit and unit.strip()]
    if not cleaned_units:
        return []
    chunks: list[str] = []
    current = ""
    for unit in cleaned_units:
        if not current:
            current = unit
            continue
        if len(current) + len(unit) <= target_len or len(chunks) >= max_chunks - 1:
            current = f"{current}{unit}"
        else:
            chunks.append(current)
            current = unit
    if current:
        chunks.append(current)
    # 末条过短时并回前一条，保证观感自然
    if len(chunks) >= 2 and len(chunks[-1]) < PUBLIC_CHAT_MIN_CHUNK_CHARS:
        chunks[-2] = f"{chunks[-2]}{chunks[-1]}"
        chunks.pop()
    return chunks[:max_chunks]


def split_public_chat_message(
    message: str,
    *,
    threshold: int = PUBLIC_CHAT_SPLIT_THRESHOLD,
    max_chunks: int = PUBLIC_CHAT_MAX_CHUNKS,
) -> list[str]:
    """把超长的公屏发言按中文标点句界拆成至多 ``max_chunks`` 条自然短句。

    - 全是空白返回空列表；
    - 以 ``/`` 开头（游戏命令）或长度不超过 ``threshold`` 时原样返回一条；
    - 先按换行与强句末标点（。！？!?…）切，仍超宽的原子句按逗号、分号等次级标点细拆，
      再贪心合并成每条尽量不超过目标宽度的短句；拆出的任何一条不短于
      ``PUBLIC_CHAT_MIN_CHUNK_CHARS``；完整不丢字、不改写原意。
    """
    raw = str(message or "").strip()
    if not raw:
        return []
    if raw.startswith("/") or len(raw) <= threshold or max_chunks <= 1:
        return [raw]

    # 先按换行与强句末标点切出自然原子句
    strong_units = [m.group(0).strip() for m in _STRONG_SENTENCE_SPLIT_RE.finditer(raw) if m.group(0).strip()]
    if not strong_units:
        strong_units = [raw]

    # 仍超宽的原子句按次级停顿标点进一步细拆
    desired_chunks = min(
        max_chunks, max(2, (len(raw) + PUBLIC_CHAT_CHUNK_TARGET_CHARS - 1) // PUBLIC_CHAT_CHUNK_TARGET_CHARS)
    )
    target_len = max(PUBLIC_CHAT_CHUNK_TARGET_CHARS, (len(raw) + desired_chunks - 1) // desired_chunks)
    refined_units: list[str] = []
    for unit in strong_units:
        if len(unit) <= target_len:
            refined_units.append(unit)
            continue
        sub_units = [m.group(0).strip() for m in _SECONDARY_CLAUSE_SPLIT_RE.finditer(unit) if m.group(0).strip()]
        if len(sub_units) >= 2:
            for sub in sub_units:
                if len(sub) > threshold:
                    refined_units.extend(_split_long_clause_safely(sub, max_len=target_len))
                else:
                    refined_units.append(sub)
        else:
            refined_units.extend(_split_long_clause_safely(unit, max_len=target_len))

    chunks = _pack_chat_units(refined_units, target_len=target_len, max_chunks=max_chunks)
    if len(chunks) <= 1 and len(raw) > threshold:
        # 无标点的极长单段合并后仍未拆开时，按安全字宽强制分段
        forced = _split_long_clause_safely(raw, max_len=target_len)
        chunks = _pack_chat_units(forced, target_len=target_len, max_chunks=max_chunks)
    return chunks or [raw]
