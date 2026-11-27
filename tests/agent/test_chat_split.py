"""长话拆分直测：阈值内不拆、命令不拆、按句末标点切、次级标点细拆、最多条数与最短字数。"""

from __future__ import annotations

from maicraft.agent.chat_filter import split_public_chat_message


def test_short_message_returned_as_is() -> None:
    assert split_public_chat_message("大家好，今天天气不错。") == ["大家好，今天天气不错。"]


def test_message_at_threshold_returned_as_is() -> None:
    text = "字" * 55
    assert split_public_chat_message(text) == [text]


def test_command_starting_with_slash_returned_as_is() -> None:
    text = "/give Mai diamond 64 " + "很长的一段话" * 20
    assert split_public_chat_message(text) == [text.strip()]


def test_whitespace_only_returns_empty_list() -> None:
    assert split_public_chat_message("   \n\t ") == []


def test_long_message_split_at_sentence_punctuation() -> None:
    text = (
        "今天我们先去挖矿。"
        + "然后去下界找堡垒。"
        + "最后回家合成装备。"
        + "路上小心苦力怕。"
        + "洞口记得先插上火把。"
        + "记得带够食物。"
        + "都听明白了吗？"
    )
    chunks = split_public_chat_message(text)
    assert len(chunks) >= 2
    assert "".join(chunks) == text
    assert all(len(chunk) >= 8 for chunk in chunks)


def test_long_sentence_without_strong_punctuation_uses_secondary_punctuation() -> None:
    text = (
        "我们先往东走一小段路，然后过了桥再沿着河一直往南，"
        + "最后在山脚下的大树旁边集合，一起把今天的营地建起来，路上都别掉队啊"
    )
    chunks = split_public_chat_message(text)
    assert len(chunks) >= 2
    assert "".join(chunks) == text


def test_unpunctuated_long_text_is_forced_into_chunks() -> None:
    text = "字" * 120
    chunks = split_public_chat_message(text)
    assert len(chunks) >= 2
    assert "".join(chunks) == text


def test_at_most_three_chunks() -> None:
    text = "第一句。第二句。第三句。第四句。第五句。第六句。第七句。第八句。" * 3
    assert len(split_public_chat_message(text)) <= 3


def test_trailing_short_chunk_is_merged_back() -> None:
    # 末句只有 2 个字，短于最短字数时应并回前一条而不是单独成条
    text = "这里是一段足够长的话需要拆开发送，短尾。" + "然后是很长很长的第二句话，也拆开。" + "尾"
    chunks = split_public_chat_message(text)
    assert all(len(chunk) >= 8 for chunk in chunks)
