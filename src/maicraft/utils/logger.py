"""基于 loguru 的日志模块。

在应用启动时调用一次 configure_from_config() 完成配置；在那之前取用日志
只有控制台输出（级别 INFO），不建目录、不写文件。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger as loguru_logger

if TYPE_CHECKING:
    from loguru import Logger

# 控制台与 text 文件共用的行格式：时间 | 级别 | 模块名 - 消息
_TEXT_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[module]} - {message}"

# 模块级状态：已挂上的输出处理器 id，重新配置时先全部摘掉
_handler_ids: list[int] = []
# 是否已调用过 configure_from_config
_configured = False


def _jsonl_line(record: dict[str, Any]) -> str:
    """把一条 loguru 记录写成一行 JSON，键为 time、level、module、message，异常时再加 exception。"""
    entry: dict[str, str] = {
        "time": record["time"].isoformat(),
        "level": record["level"].name,
        "module": record["extra"].get("module", "unknown"),
        "message": record["message"],
    }
    exc = record["exception"]
    if exc is not None and exc[0] is not None:
        entry["exception"] = "".join(traceback.format_exception(*exc)).rstrip("\n")
    return json.dumps(entry, ensure_ascii=False)


def _jsonl_formatter(record: dict[str, Any]) -> str:
    """loguru 文件格式函数：序列化进 extra 后用占位符输出一行 JSON。"""
    record["extra"]["serialized"] = _jsonl_line(record)
    return "{extra[serialized]}\n"


def configure_from_config(config_dict: dict[str, Any] | None = None) -> None:
    """按配置字典重建全部输出；字典缺键时用默认值。

    支持的键与默认值：enabled=True、format="text"、directory="logs"、
    level="INFO"、console_level="INFO"、rotation="10 MB"、
    retention="7 days"、compression="zip"。
    重复调用安全：先摘掉之前挂的输出，不会重复写同一条日志。
    """
    global _configured
    _configured = True
    config = config_dict or {}

    for handler_id in _handler_ids:
        loguru_logger.remove(handler_id)
    _handler_ids.clear()
    # 同时摘掉 loguru 自带的默认 stderr 处理器，避免配置前取用留下的重复输出
    loguru_logger.remove()

    console_handler = loguru_logger.add(
        sys.stderr,
        level=config.get("console_level", "INFO"),
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{extra[module]}</cyan> - <level>{message}</level>",
    )
    _handler_ids.append(console_handler)

    if not config.get("enabled", True):
        return

    directory = Path(config.get("directory", "logs"))
    common = {
        "level": config.get("level", "INFO"),
        "rotation": config.get("rotation", "10 MB"),
        "retention": config.get("retention", "7 days"),
        "compression": config.get("compression", "zip"),
        "encoding": "utf-8",
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if config.get("format", "text") == "jsonl":
            file_handler = loguru_logger.add(
                directory / "maicraft_{time}.log",
                format=_jsonl_formatter,
                **common,
            )
        else:
            file_handler = loguru_logger.add(
                directory / "maicraft_{time}.log",
                format=_TEXT_FORMAT,
                **common,
            )
    except OSError as exc:
        # 常驻程序不因日志写不了而退出：降级为只有控制台输出，并说明原因
        loguru_logger.bind(module="logging").warning(
            "日志目录 {path} 建不了或文件输出挂不上（{reason}），本次只输出到控制台",
            path=str(directory),
            reason=exc,
        )
        return
    _handler_ids.append(file_handler)


class ModuleLogger:
    """绑定模块名的日志入口，屏蔽 loguru 细节；需要 loguru 原生能力时用 raw()。"""

    def __init__(self, module_name: str) -> None:
        self._logger: Logger = loguru_logger.bind(module=module_name)

    def _log(self, level: str, message: str, args: tuple[Any, ...], exc: bool | BaseException) -> None:
        logger = self._logger.opt(exception=exc) if exc else self._logger
        logger.log(level, message, *args)

    def debug(self, message: str, *args: Any, exc: bool | BaseException = False) -> None:
        """debug 级日志；args 做 % 风格延迟格式化，exc 为 True 记录当前 except 块的异常或传异常对象。"""
        self._log("DEBUG", message, args, exc)

    def info(self, message: str, *args: Any, exc: bool | BaseException = False) -> None:
        """info 级日志；args 与 exc 含义同 debug。"""
        self._log("INFO", message, args, exc)

    def warning(self, message: str, *args: Any, exc: bool | BaseException = False) -> None:
        """warning 级日志；args 与 exc 含义同 debug。"""
        self._log("WARNING", message, args, exc)

    def error(self, message: str, *args: Any, exc: bool | BaseException = False) -> None:
        """error 级日志；args 与 exc 含义同 debug。"""
        self._log("ERROR", message, args, exc)

    def exception(self, message: str) -> None:
        """error 级日志，附带当前 except 块中异常的完整堆栈。"""
        self._logger.opt(exception=True).error(message)

    def raw(self) -> Logger:
        """返回底层 loguru logger；某个用法在这里频繁出现就升级成门面方法。"""
        return self._logger


def get_logger(module_name: str) -> ModuleLogger:
    """取绑定模块名的日志入口；未配置过时保证有一个默认控制台输出。"""
    if not _configured and not _handler_ids:
        # 先摘掉 loguru 导入时自带的默认处理器，避免同一条日志输出两遍、两种格式
        loguru_logger.remove()
        _handler_ids.append(
            loguru_logger.add(
                sys.stderr,
                level="INFO",
                colorize=True,
                format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{extra[module]}</cyan> - <level>{message}</level>",
            )
        )
    return ModuleLogger(module_name)


__all__ = ["ModuleLogger", "configure_from_config", "get_logger"]
