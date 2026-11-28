import json

from maicraft.openai_client.token_usage_manager import TokenUsageManager


def test_usage_files_go_to_the_given_directory_and_accumulate(tmp_path):
    usage_dir = tmp_path / "usage"
    manager = TokenUsageManager(usage_dir=str(usage_dir), use_global=False)

    manager.record_usage("some-model", 10, 5, 15)
    manager.record_usage("some-model", 7, 3, 10)

    saved = json.loads((usage_dir / "some-model_usage.json").read_text(encoding="utf-8"))
    assert saved["total_prompt_tokens"] == 17
    assert saved["total_completion_tokens"] == 8
    assert saved["total_tokens"] == 25
    assert saved["total_calls"] == 2


def test_usage_directory_is_created_lazily(tmp_path):
    """用量目录等到第一次要写文件时才创建：构造管理器不在运行目录建出空目录。"""
    usage_dir = tmp_path / "usage"
    manager = TokenUsageManager(usage_dir=str(usage_dir), use_global=False)

    assert not usage_dir.exists()

    manager.record_usage("some-model", 1, 1, 2)

    assert usage_dir.exists()
    assert (usage_dir / "some-model_usage.json").exists()
