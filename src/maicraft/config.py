"""配置模型与配置文件加载：不存在就从模板创建，模板更新时升级旧配置文件。"""

import os
import shutil
import tomllib as tomli
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Dict, Optional

from packaging import version
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from maicraft.utils.logger import get_logger


class ConfigError(Exception):
    """配置文件无法使用：找不到模板、校验不通过等，异常消息是给用户看的中文说明。"""


class MetaConfig(BaseModel):
    """配置文件格式版本，只用来和模板比较"""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="0.4.0", description="配置格式的版本，只用来和模板比较")


class LoggingConfig(BaseModel):
    """Logging配置模型"""

    model_config = ConfigDict(extra="forbid")

    level: str = Field(default="INFO", description="日志级别")
    enable_json: bool = Field(default=False, description="是否启用JSON Lines格式文件输出（控制台始终为彩色格式）")
    log_to_file: bool = Field(default=True, description="是否输出日志到文件")
    log_dir: str = Field(default="logs", description="日志目录")
    rotation: str = Field(default="1 day", description="日志轮转策略（如'1 day', '100 MB'）")
    retention: str = Field(default="7 days", description="日志保留策略（如'7 days', '10 files'）")
    enable_hierarchical_logging: bool = Field(default=True, description="是否启用层次化日志记录")
    max_recent_logs: int = Field(default=1000, ge=100, le=10000, description="内存中保留的最近日志条数")


class LLMConfig(BaseModel):
    """LLM配置模型"""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="gpt-4o-mini", description="LLM模型名称")
    api_key: str = Field(default="", description="API密钥")
    base_url: str = Field(default="", description="API基础URL")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0, description="温度参数")
    max_tokens: int = Field(default=4096, ge=100, le=32000, description="最大token数")
    supports_vision: bool = Field(
        default=False,
        description="模型能不能看图；false 时 mod 发来的画面只把旁边的文字给模型",
    )
    request_timeout_s: float = Field(default=120.0, gt=0.0, description="单次模型请求的超时秒数")


class ModConfig(BaseModel):
    """MaiCraftMod 连接配置模型"""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(default="http://127.0.0.1:8766/mcp", description="mod 的 MCP 端点地址")
    events_wait_ms: int = Field(default=25000, ge=1000, le=55000, description="事件长轮询的单次等待毫秒数")


class AgentConfig(BaseModel):
    """agent 行为配置模型"""

    model_config = ConfigDict(extra="forbid")

    max_steps_per_turn: int = Field(default=30, ge=1, le=200, description="一轮对话里最多执行的动作步数")
    idle_replan_minutes: int = Field(
        default=10, ge=0, le=1440, description="空闲多少分钟后主动叫醒模型；0 表示不主动叫醒"
    )
    max_empty_turns: int = Field(default=3, ge=1, le=20, description="连续多少轮没有实际动作后停下")
    long_term_goal: str = Field(default="", description="长期目标，原样放进提示词")
    keep_recent_results: int = Field(default=6, ge=1, le=30, description="提示词里保留最近几条动作结果")


class ChatConfig(BaseModel):
    """聊天相关配置模型"""

    model_config = ConfigDict(extra="forbid")

    mention_names: list[str] = Field(default_factory=list, description="除角色名之外还接受哪些称呼")
    batch_window_ms: int = Field(default=2000, ge=200, le=10000, description="把相邻消息合并处理的等待毫秒数")


class MaigoConfig(BaseModel):
    """Maigo 对接配置模型"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="是否启用 Maigo 对接")
    url: str = Field(default="http://127.0.0.1:8768", description="Maigo 服务地址")
    branch_id: str = Field(default="maicraft", description="在 Maigo 里使用的分支标识")


class MaicraftConfig(BaseModel):
    """Maicraft 配置模型"""

    model_config = ConfigDict(extra="forbid")

    meta: MetaConfig = Field(default_factory=MetaConfig, description="配置格式版本")
    logging: LoggingConfig = Field(default_factory=LoggingConfig, description="Logging配置")
    llm: LLMConfig = Field(default_factory=LLMConfig, description="LLM配置")
    mod: ModConfig = Field(default_factory=ModConfig, description="mod连接配置")
    agent: AgentConfig = Field(default_factory=AgentConfig, description="agent行为配置")
    chat: ChatConfig = Field(default_factory=ChatConfig, description="聊天配置")
    maigo: MaigoConfig = Field(default_factory=MaigoConfig, description="Maigo对接配置")


def template_path() -> Path:
    """包内自带的配置模板；装成 wheel 之后也在包里"""
    traversable = files("maicraft").joinpath("config-template.toml")
    with as_file(traversable) as path:
        return path


def _get_version_from_toml(toml_path: str) -> Optional[str]:
    """从TOML文件中获取版本号"""
    try:
        with open(toml_path, "rb") as f:
            data = tomli.load(f)
        return data.get("meta", {}).get("version")
    except Exception:
        return None


def _compare_versions(template_version: str, config_version: str) -> bool:
    """比较版本号，返回True表示模板版本更高"""
    try:
        return version.parse(template_version) > version.parse(config_version)
    except Exception:
        # 如果版本号格式不正确，认为需要更新
        return True


def _parse_toml_with_comments(file_path: str) -> tuple[Dict[str, Any], str]:
    """解析TOML文件并保留注释"""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 使用tomli解析数据
    with open(file_path, "rb") as f:
        data = tomli.load(f)

    return data, content


def _update_config_from_template(config_path: str, template_path_str: str) -> None:
    """从模板更新配置文件，保留用户的自定义设置和注释"""
    logger = get_logger("ConfigLoader")

    try:
        # 读取模板和现有配置
        template_data, template_content = _parse_toml_with_comments(template_path_str)

        # 如果配置文件存在，读取现有配置
        existing_data = {}
        existing_content = ""
        if os.path.exists(config_path):
            existing_data, existing_content = _parse_toml_with_comments(config_path)

        # 创建备份
        if os.path.exists(config_path):
            backup_path = config_path + ".backup"
            shutil.copy2(config_path, backup_path)
            logger.info(f"[配置更新] 已创建备份文件: {backup_path}")

        # 合并配置：以模板为基础，保留用户的自定义值
        merged_data = {}

        # 首先添加模板中的所有配置项
        for section, values in template_data.items():
            if isinstance(values, dict):
                merged_data[section] = {}
                for key, template_value in values.items():
                    # 如果现有配置中有相同的键，且与模板值不同，则保留用户设置；
                    # api_key 的占位值不保留，版本号始终跟随模板
                    if (
                        section in existing_data
                        and key in existing_data[section]
                        and existing_data[section][key] != template_value
                        and not (key == "api_key" and existing_data[section][key] == "your-api-key")
                        and not (section == "meta" and key == "version")
                    ):
                        merged_data[section][key] = existing_data[section][key]
                        logger.info(f"[配置更新] 保留用户设置: {section}.{key} = {existing_data[section][key]}")
                    else:
                        merged_data[section][key] = template_value
            else:
                merged_data[section] = values

        # 生成新的配置文件内容，保留注释
        new_content = _generate_config_content_with_comments(merged_data, template_content, existing_content)

        # 写入更新后的配置
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        logger.info(f"[配置更新] 配置文件已更新: {config_path}")

    except Exception as e:
        logger.error(f"[配置更新] 更新配置文件失败: {e}")
        raise


def _generate_config_content_with_comments(
    merged_data: Dict[str, Any], template_content: str, existing_content: str
) -> str:
    """生成包含注释的配置文件内容"""
    lines = []

    # 添加文件头注释
    lines.append("# Maicraft配置")
    lines.append("")

    def _format_toml_value(value: Any) -> str:
        """将Python值转换为TOML字面量字符串，确保布尔为小写true/false。"""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            escaped = value.replace('"', '\\"')
            return f'"{escaped}"'
        if isinstance(value, (int, float)):
            return str(value)
        if value is None:
            return '""'
        if isinstance(value, list):
            items = ", ".join(_format_toml_value(v) for v in value)
            return f"[ {items} ]"
        return f'"{str(value)}"'

    # 为每个section生成内容
    for section, values in merged_data.items():
        # 添加section注释（从模板中提取）
        section_comment = _extract_section_comment(template_content, section)
        if section_comment:
            lines.append(section_comment)

        lines.append(f"[{section}]")

        if isinstance(values, dict):
            for key, value in values.items():
                # 添加key的注释（从模板中提取）
                key_comment = _extract_key_comment(template_content, section, key)
                if key_comment:
                    lines.append(key_comment)

                # 添加配置项
                lines.append(f"{key} = {_format_toml_value(value)}")
        else:
            lines.append(f"value = {_format_toml_value(values)}")

        lines.append("")

    return "\n".join(lines)


def _extract_section_comment(content: str, section: str) -> str:
    """从内容中提取section的注释"""
    lines = content.split("\n")
    section_line = f"[{section}]"

    for i, line in enumerate(lines):
        if line.strip() == section_line:
            # 查找section前的注释
            for j in range(i - 1, -1, -1):
                comment_line = lines[j].strip()
                if comment_line.startswith("#"):
                    return comment_line
                elif comment_line == "":
                    continue
                else:
                    break
            break

    return ""


def _extract_key_comment(content: str, section: str, key: str) -> str:
    """从内容中提取key的注释"""
    lines = content.split("\n")
    in_section = False

    for i, line in enumerate(lines):
        line_stripped = line.strip()

        # 检查是否进入目标section
        if line_stripped == f"[{section}]":
            in_section = True
            continue

        # 如果遇到新的section，退出当前section
        if in_section and line_stripped.startswith("[") and line_stripped.endswith("]"):
            break

        # 在目标section中查找key
        if in_section and line_stripped.startswith(f"{key} ="):
            # 查找key前的注释
            for j in range(i - 1, -1, -1):
                comment_line = lines[j].strip()
                if comment_line.startswith("#"):
                    return comment_line
                elif comment_line == "":
                    continue
                else:
                    break
            break

    return ""


def _create_config_from_template(config_path: Path, template: Path) -> None:
    """把模板复制为配置文件"""
    logger = get_logger("ConfigLoader")

    if not template.exists():
        logger.error("[配置创建] 未找到config-template.toml文件")
        raise FileNotFoundError(f"config-template.toml文件不存在: {template}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, config_path)
    logger.info(f"[配置创建] 已从模板创建配置文件: {config_path}")


def load_config(path: str | os.PathLike[str] | None = None) -> MaicraftConfig:
    """加载配置并校验成 MaicraftConfig

    文件不存在时从模板复制；存在时模板版本更高、或用户配置的版本号读不出来，就按模板
    升级一次（先备份，保留用户设置和注释）。找不到模板、校验不通过时抛
    :class:`ConfigError`，消息是给用户看的中文说明。

    Args:
        path: 配置文件路径；不给时用运行目录下的 config.toml

    Returns:
        校验后的配置对象
    """
    logger = get_logger("ConfigLoader")

    config_path = Path(path) if path is not None else Path.cwd() / "config.toml"
    template = template_path()

    if not config_path.exists():
        if not template.exists():
            raise ConfigError(
                f"没有找到配置模板：应该在 {template}。模板随程序一起打包，找不到通常说明安装不完整；"
                f"请重新安装本程序，或先把一份 config.toml 放到 {config_path}，再用 --config 指定它的路径。"
            )
        logger.info("[配置加载] 未找到config.toml文件，正在创建默认配置...")
        _create_config_from_template(config_path, template)
    else:
        template_version = _get_version_from_toml(str(template))
        config_version = _get_version_from_toml(str(config_path))
        # 用户配置的版本号读不出来（缺 [inner]、缺 version、不是合法版本号）而模板在位时，
        # 也按模板升级路径处理一次：备份、去掉模板里没有的段和键、补上缺的、保留用户已有的值
        if template_version and (config_version is None or _compare_versions(template_version, config_version)):
            logger.info(f"[配置更新] 检测到模板版本更新: {config_version} -> {template_version}")
            _update_config_from_template(str(config_path), str(template))

    with open(config_path, "rb") as f:
        data = tomli.load(f)

    try:
        return MaicraftConfig.model_validate(data)
    except ValidationError as exc:
        lines = [f"配置文件校验失败：{config_path}"]
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "（顶层）"
            lines.append(f"- {field}：{error['msg']}")
        raise ConfigError("\n".join(lines)) from exc
