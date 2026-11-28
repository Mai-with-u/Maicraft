"""配置模型与配置文件加载测试，全部在临时目录里进行。"""

import shutil
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from maicraft.config import ConfigError, MaicraftConfig, load_config

REPO_TEMPLATE = Path(__file__).resolve().parents[1] / "config-template.toml"


@pytest.fixture
def template_dir(tmp_path, monkeypatch):
    """临时运行目录，里面放了新版模板，并把工作目录切过去。"""
    shutil.copy2(REPO_TEMPLATE, tmp_path / "config-template.toml")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_defaults_from_template(template_dir):
    config = load_config()

    assert config.inner.version == "0.3.0"
    assert config.llm.model == "gpt-4o-mini"
    assert config.llm.api_key == ""
    assert config.llm.temperature == 0.2
    assert config.llm.max_tokens == 4096
    assert config.llm.supports_vision is False
    assert config.llm.request_timeout_s == 120.0
    assert config.mod.url == "http://127.0.0.1:8766/mcp"
    assert config.mod.events_wait_ms == 25000
    assert config.agent.max_steps_per_turn == 30
    assert config.agent.idle_replan_minutes == 10
    assert config.agent.max_empty_turns == 3
    assert config.agent.long_term_goal == ""
    assert config.agent.keep_recent_results == 6
    assert config.chat.mention_names == []
    assert config.chat.batch_window_ms == 2000
    assert config.maigo.enabled is False
    assert config.maigo.url == "http://127.0.0.1:8768"
    assert config.maigo.branch_id == "maicraft"
    # 没有配置文件时会从模板创建
    assert (template_dir / "config.toml").exists()
    assert not (template_dir / "config.toml.backup").exists()


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        MaicraftConfig.model_validate({"llm": {"no_such_field": 1}})
    with pytest.raises(ValidationError):
        MaicraftConfig.model_validate({"no_such_section": {}})


def test_upgrade_from_old_format_keeps_user_values(template_dir):
    old_config = """\
[inner]
version = "0.2.0"

[bot]
player_name = "Mai"
bot_name = "麦麦"

[game]
goal = "挖到钻石"

[llm]
model = "gpt-4o"
api_key = "sk-user-key"
base_url = "https://api.example.com/v1"
temperature = 0.7
max_tokens = 1024

[api]
port = 20914

[vlm]
model = "gpt-4o"

[logging]
level = "DEBUG"
"""
    (template_dir / "config.toml").write_text(old_config, encoding="utf-8")

    config = load_config()

    # 返回值校验通过，旧段消失
    assert isinstance(config, MaicraftConfig)
    text = (template_dir / "config.toml").read_text(encoding="utf-8")
    for old_section in ("[bot]", "[game]", "[api]", "[vlm]"):
        assert old_section not in text
    # 生成了备份
    assert (template_dir / "config.toml.backup").exists()
    # 新段是模板默认值
    with open(template_dir / "config.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["chat"] == {"mention_names": [], "batch_window_ms": 2000}
    assert data["maigo"] == {"enabled": False, "url": "http://127.0.0.1:8768", "branch_id": "maicraft"}
    assert data["llm"]["supports_vision"] is False
    assert data["llm"]["request_timeout_s"] == 120.0
    # 用户值被保留，版本号跟随模板
    assert config.llm.api_key == "sk-user-key"
    assert config.llm.model == "gpt-4o"
    assert config.llm.temperature == 0.7
    assert config.llm.max_tokens == 1024
    assert config.logging.level == "DEBUG"
    assert config.inner.version == "0.3.0"
    assert data["inner"]["version"] == "0.3.0"


def test_no_upgrade_when_template_version_not_higher(template_dir):
    config_text = (REPO_TEMPLATE).read_text(encoding="utf-8")
    (template_dir / "config.toml").write_text(config_text, encoding="utf-8")
    before = (template_dir / "config.toml").stat().st_mtime_ns

    load_config()

    assert (template_dir / "config.toml").stat().st_mtime_ns == before
    assert not (template_dir / "config.toml.backup").exists()


def test_explicit_path_is_used(tmp_path):
    template_dir = tmp_path / "run"
    template_dir.mkdir()
    shutil.copy2(REPO_TEMPLATE, template_dir / "config-template.toml")
    config_path = tmp_path / "elsewhere" / "my-config.toml"

    config = load_config(config_path)

    assert config_path.exists()
    assert config.inner.version == "0.3.0"
    assert template_dir.exists()


def test_missing_template_reports_a_readable_error(tmp_path, monkeypatch):
    """运行目录里没有模板又要新建配置：给出中文说明（找过哪里、该怎么办），不抛原始回溯。"""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "config-template.toml" in message
    assert str(tmp_path) in message, "写明在哪个目录找过模板"
    assert "仓库目录" in message
    assert "config.toml" in message


def test_upgrade_runs_when_user_version_is_unreadable(template_dir):
    """用户配置缺 [inner] 或版本号不合法而模板在位：按现有升级路径处理一次。"""
    for version_line in ('[inner]\nversion = "not-a-version"\n', ""):
        old_config = (
            version_line
            + """\
[bot]
player_name = "Mai"

[llm]
model = "gpt-4o"
api_key = "sk-user-key"
temperature = 0.7

[logging]
level = "DEBUG"
"""
        )
        (template_dir / "config.toml").write_text(old_config, encoding="utf-8")

        config = load_config()

        assert isinstance(config, MaicraftConfig)
        assert (template_dir / "config.toml.backup").exists(), "升级前留备份"
        text = (template_dir / "config.toml").read_text(encoding="utf-8")
        assert "[bot]" not in text, "模板里没有的段被去掉"
        assert config.inner.version == "0.3.0"
        assert config.llm.model == "gpt-4o"
        assert config.llm.api_key == "sk-user-key"
        assert config.llm.temperature == 0.7
        assert config.logging.level == "DEBUG"
        assert (template_dir / "config.toml.backup").exists()
        (template_dir / "config.toml.backup").unlink()


def test_validation_failure_lists_each_bad_field(template_dir):
    """最终校验失败：报中文错误，写明配置文件路径和每一个出问题的字段，每条一行。"""
    text = (REPO_TEMPLATE).read_text(encoding="utf-8")
    bad_config = text.replace("temperature = 0.2", "temperature = 5.0").replace("max_tokens = 4096", "max_tokens = 1")
    (template_dir / "config.toml").write_text(bad_config, encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert str(template_dir / "config.toml") in message
    assert "llm.temperature" in message
    assert "llm.max_tokens" in message
    lines = [line for line in message.splitlines() if line.startswith("- ")]
    assert len(lines) == 2, "每个出问题的字段各占一行"


def test_main_prints_chinese_config_error_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    """main 层面：配置无法使用时把中文说明打到标准错误，非零退出码。"""
    monkeypatch.chdir(tmp_path)

    from maicraft.main import main

    assert main([]) == 1

    err = capsys.readouterr().err
    assert "config-template.toml" in err
    assert "仓库目录" in err
