"""配置模型与配置文件加载测试，全部在临时目录里进行。"""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

import maicraft.config
from maicraft.config import ConfigValidationError, MaicraftConfig, load_config

PACKAGE_TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "maicraft" / "config-template.toml"

# 0.3.0 格式的完整配置样本（改动前的模板内容），用来验证旧配置的升级
TEMPLATE_030 = """\
# Maicraft配置模板
[inner]
# 配置格式的版本，只用来和模板比较，不要手动改
version = "0.3.0"

[logging]
# 日志级别，可选: TRACE, DEBUG, INFO, SUCCESS, WARNING, ERROR, CRITICAL
level = "INFO"
# 是否启用JSON Lines格式文件输出（控制台始终为彩色格式）
enable_json = false
# 是否输出日志到文件
log_to_file = true
# 日志目录
log_dir = "logs"
# 日志轮转策略（如"1 day", "100 MB"）
rotation = "1 day"
# 日志保留策略（如"7 days", "10 files"）
retention = "7 days"
# 是否启用层次化日志记录（不同级别输出到不同文件）
enable_hierarchical_logging = true
# 内存中保留的最近日志条数
max_recent_logs = 1000

[llm]
# 模型名称
model = "gpt-4o-mini"
# API密钥
api_key = ""
# API基础URL，留空使用服务方默认地址
base_url = ""
# 温度参数，越高越发散
temperature = 0.2
# 单次回复的最大token数
max_tokens = 4096
# 模型能不能看图；false 时 mod 发来的画面只把旁边的文字给模型
supports_vision = false
# 单次模型请求的超时秒数
request_timeout_s = 120.0

[mod]
# mod 的 MCP 端点地址
url = "http://127.0.0.1:8766/mcp"
# 事件长轮询的单次等待毫秒数
events_wait_ms = 25000

[agent]
# 一轮对话里最多执行的动作步数
max_steps_per_turn = 30
# 空闲多少分钟后主动叫醒模型；0 表示不主动叫醒
idle_replan_minutes = 10
# 连续多少轮没有实际动作后停下
max_empty_turns = 3
# 长期目标，原样放进提示词
long_term_goal = ""
# 提示词里保留最近几条动作结果
keep_recent_results = 6

[chat]
# 除角色名之外还接受哪些称呼
mention_names = []
# 把相邻消息合并处理的等待毫秒数
batch_window_ms = 2000

[maigo]
# 是否启用 Maigo 对接
enabled = false
# Maigo 服务地址
url = "http://127.0.0.1:8768"
# 在 Maigo 里使用的分支标识
branch_id = "maicraft"
"""


@pytest.fixture
def template_dir(tmp_path, monkeypatch):
    """临时运行目录：模板从包里取，工作目录切过去。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_defaults_from_template(template_dir):
    config = load_config()

    assert config.meta.version == "0.4.0"
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
    assert config.meta.version == "0.4.0"
    assert data["meta"]["version"] == "0.4.0"


def test_upgrade_from_030_template_keeps_user_values(template_dir):
    """0.3.0 格式的完整旧配置升级到 0.4.0：[inner] 和旧的 [logging] 字段被去掉，
    新的 [meta] 和 [logging] 字段补上，同名键上用户已有的值保留，原文件留备份。"""
    old_config = (
        TEMPLATE_030.replace('level = "INFO"', 'level = "DEBUG"')
        .replace('rotation = "1 day"', 'rotation = "5 MB"')
        .replace('retention = "7 days"', 'retention = "3 days"')
        .replace('api_key = ""', 'api_key = "sk-user-key"')
    )
    (template_dir / "config.toml").write_text(old_config, encoding="utf-8")

    config = load_config()

    assert isinstance(config, MaicraftConfig)
    assert (template_dir / "config.toml.backup").exists(), "升级前留备份"
    text = (template_dir / "config.toml").read_text(encoding="utf-8")
    assert "[inner]" not in text, "旧版本段被去掉"
    for old_field in ("enable_json", "log_to_file", "log_dir", "enable_hierarchical_logging", "max_recent_logs"):
        assert old_field not in text, f"旧的日志字段 {old_field} 被去掉"
    with open(template_dir / "config.toml", "rb") as f:
        data = tomllib.load(f)
    assert data["meta"] == {"version": "0.4.0"}
    assert data["logging"] == {
        "enabled": True,
        "format": "text",
        "directory": "logs",
        "level": "DEBUG",
        "console_level": "INFO",
        "rotation": "5 MB",
        "retention": "3 days",
        "compression": "zip",
    }
    assert config.logging.level == "DEBUG"
    assert config.logging.rotation == "5 MB"
    assert config.logging.retention == "3 days"
    assert config.llm.api_key == "sk-user-key"
    assert config.meta.version == "0.4.0"


def test_no_upgrade_when_template_version_not_higher(template_dir):
    config_text = PACKAGE_TEMPLATE.read_text(encoding="utf-8")
    (template_dir / "config.toml").write_text(config_text, encoding="utf-8")
    before = (template_dir / "config.toml").stat().st_mtime_ns

    load_config()

    assert (template_dir / "config.toml").stat().st_mtime_ns == before
    assert not (template_dir / "config.toml.backup").exists()


def test_explicit_path_is_used(tmp_path):
    config_path = tmp_path / "elsewhere" / "my-config.toml"

    config = load_config(config_path)

    assert config_path.exists()
    assert config.meta.version == "0.4.0"


def test_missing_template_reports_a_readable_error(tmp_path, monkeypatch):
    """模板不在位又要新建配置：给出中文说明（该在哪、该怎么办），不抛原始回溯。"""
    fake_template = tmp_path / "no-such" / "config-template.toml"
    monkeypatch.setattr(maicraft.config, "template_path", lambda: fake_template)

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config()

    message = str(excinfo.value)
    assert "config-template.toml" in message
    assert str(fake_template) in message, "写明模板应该在哪个位置"
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
        assert config.meta.version == "0.4.0"
        assert config.llm.model == "gpt-4o"
        assert config.llm.api_key == "sk-user-key"
        assert config.llm.temperature == 0.7
        assert config.logging.level == "DEBUG"
        assert (template_dir / "config.toml.backup").exists()
        (template_dir / "config.toml.backup").unlink()


def test_validation_failure_lists_each_bad_field(template_dir):
    """最终校验失败：报中文错误，写明配置文件路径和每一个出问题的字段，每条一行。"""
    text = PACKAGE_TEMPLATE.read_text(encoding="utf-8")
    bad_config = text.replace("temperature = 0.2", "temperature = 5.0").replace("max_tokens = 4096", "max_tokens = 1")
    (template_dir / "config.toml").write_text(bad_config, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
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
    monkeypatch.setattr(maicraft.config, "template_path", lambda: tmp_path / "no-such" / "config-template.toml")

    from maicraft.main import main

    assert main([]) == 1

    err = capsys.readouterr().err
    assert "config-template.toml" in err
