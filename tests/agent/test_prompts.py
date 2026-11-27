"""系统提示词的静态检查：能读出来、工具名只用七个、不带来源里宿主场景的内容。"""

from __future__ import annotations

import importlib.resources

# 给模型的全部工具：mod 的四个 + 本地的三个
TOOLS_FOR_MODEL = ("observe", "lookup", "execute", "goal", "notes", "read_skill", "recall")

# 来源提示词里的宿主特有工具，一律不出现
EXCLUDED_HOST_TOOLS = (
    "minecraft_report",
    "minecraft_todo",
    "minecraft_get_work_log",
    "minecraft_glance",
    "minecraft_chat_send",
    "minecraft_drawing",
    "minecraft_skill",
    "minecraft_context_fold",
    "minecraft_context_recall",
    "minecraft_rules",
)

# 来源提示词为主播直播场景写的词，这里不该出现
EXCLUDED_WORDS = ("主播", "直播", "观众", "上报")


def _system_prompt() -> str:
    text = importlib.resources.files("maicraft.agent.prompts").joinpath("system.md").read_text(encoding="utf-8")
    return text


def test_system_prompt_readable() -> None:
    text = _system_prompt()
    assert text.strip()
    for section in (
        "工具",
        "每一轮怎么用",
        "遇到困难时",
        "读结果",
        "行为准则",
        "记住地点",
        "在游戏公屏说话",
        "消息前缀",
    ):
        assert section in text


def test_tool_names_exactly_the_seven() -> None:
    text = _system_prompt()
    for name in TOOLS_FOR_MODEL:
        assert name in text
    for prefix in ("maicraft_", "minecraft_"):
        assert prefix not in text
    for tool in EXCLUDED_HOST_TOOLS:
        assert tool not in text


def test_no_streamer_words() -> None:
    text = _system_prompt()
    for word in EXCLUDED_WORDS:
        assert word not in text
