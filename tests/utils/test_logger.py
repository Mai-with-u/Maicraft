"""日志模块的行为测试：文件全部写到 tmp_path，不 sleep、不联网。"""

import json
import re
import subprocess
import sys

import pytest

from maicraft.utils.logger import configure_from_config, get_logger


@pytest.fixture(autouse=True)
def _reset_logging_config():
    """每个测试结束后摘掉本测试挂的输出，避免影响其他测试进程内的处理器。"""
    yield
    configure_from_config({"enabled": False, "console_level": "WARNING"})


def _read_lines(directory):
    files = sorted(directory.glob("maicraft_*.log"))
    assert files, "日志目录下应出现 maicraft_ 开头的日志文件"
    return files[-1].read_text(encoding="utf-8").splitlines()


def test_unconfigured_use_creates_nothing(tmp_path):
    """从没配置过的进程里取用并记日志：运行目录不建 logs/，也不出现任何新文件。

    本测试进程在 conftest 里已经配置过日志，所以用子进程模拟真正的"未配置"状态。
    """
    result = subprocess.run(
        [sys.executable, "-c", "from maicraft.utils.logger import get_logger; get_logger('x').info('hi')"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "hi" in result.stderr
    assert not (tmp_path / "logs").exists()
    assert not list(tmp_path.iterdir()), "运行目录里不应出现任何新文件"


def test_unconfigured_use_outputs_each_message_once(tmp_path):
    """未配置进程里取用并记一条日志：标准错误里恰好一行，且是约定的行格式。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from maicraft.utils.logger import get_logger; get_logger('OnceModule').info('hello-once')",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    ansi_pattern = re.compile(r"\x1b\[[0-9;]*m")
    plain_lines = [ansi_pattern.sub("", line) for line in result.stderr.splitlines()]
    matched = [line for line in plain_lines if "hello-once" in line]
    assert len(matched) == 1, f"应只输出一行，实际：{result.stderr!r}"
    assert "| INFO     | OnceModule - hello-once" in matched[0]


def test_unwritable_directory_falls_back_to_console(tmp_path, capsys):
    """日志目录建不出来时不抛异常：退回只输出控制台，并记一条警告说明原因。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("占位文件", encoding="utf-8")
    configure_from_config({"enabled": True, "directory": str(blocker / "sub")})

    get_logger("FallbackModule").info("仍然要能输出")

    err = capsys.readouterr().err
    assert "仍然要能输出" in err
    assert "只输出到控制台" in err
    assert not (blocker / "sub").exists()


def test_exc_with_exception_object_records_traceback(tmp_path):
    """exc 直接传异常对象时，文本日志里带该异常的堆栈。"""
    log_dir = tmp_path / "logdir"
    configure_from_config({"enabled": True, "format": "text", "directory": str(log_dir)})
    error = KeyError("缺少键")

    get_logger("ExcObjectModule").error("参数不对", exc=error)

    text = "\n".join(_read_lines(log_dir))
    assert "参数不对" in text and "KeyError" in text and "缺少键" in text


def test_jsonl_message_with_newline_stays_one_line(tmp_path):
    """jsonl 格式下消息里的换行被转义，每条日志仍占一行。"""
    log_dir = tmp_path / "logdir"
    configure_from_config({"enabled": True, "format": "jsonl", "directory": str(log_dir)})

    get_logger("NewlineModule").info("第一行\n第二行")

    lines = _read_lines(log_dir)
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "第一行\n第二行"


def test_disabled_log_does_not_create_directory(tmp_path):
    """enabled=False 时即使显式给了 directory 也不建目录。"""
    log_dir = tmp_path / "never"
    configure_from_config({"enabled": False, "directory": str(log_dir)})

    get_logger("DisabledModule").info("不落盘")

    assert not log_dir.exists()


def test_enabled_writes_text_file_with_expected_format(tmp_path):
    """enabled 为真时在指定目录建出 maicraft_ 开头的文件，行格式为 时间 | 级别 | 模块 - 消息。"""
    log_dir = tmp_path / "logdir"
    configure_from_config({"enabled": True, "format": "text", "directory": str(log_dir)})
    get_logger("SomeModule").info("文本格式冒烟")

    line = _read_lines(log_dir)[0]
    assert " | INFO     | SomeModule - 文本格式冒烟" in line
    # 时间段形如 YYYY-MM-DD HH:mm:ss.SSS
    timestamp = line.split(" | ")[0]
    assert len(timestamp) == len("YYYY-MM-DD HH:mm:ss.SSS") and timestamp[4] == "-"


def test_jsonl_lines_are_parseable_and_carry_exception(tmp_path):
    """format 为 jsonl 时每行都是合法 JSON，键齐全；异常时带 exception 字段。"""
    log_dir = tmp_path / "logdir"
    configure_from_config({"enabled": True, "format": "jsonl", "directory": str(log_dir)})
    logger = get_logger("JsonModule")
    logger.info("正常一条")
    try:
        raise ValueError("出错了")
    except ValueError:
        logger.exception("记录异常")

    lines = _read_lines(log_dir)
    normal = json.loads(lines[0])
    assert set(normal) == {"time", "level", "module", "message"}
    assert normal["module"] == "JsonModule"
    assert normal["message"] == "正常一条"
    assert normal["level"] == "INFO"
    failure = json.loads(lines[1])
    assert failure["exception"] is not None and "ValueError" in failure["exception"]


def test_reconfigure_does_not_duplicate(tmp_path):
    """重复调用 configure_from_config 后同一条日志只写一次。"""
    log_dir = tmp_path / "logdir"
    configure_from_config({"enabled": True, "directory": str(log_dir)})
    configure_from_config({"enabled": True, "directory": str(log_dir)})
    get_logger("DupModule").info("只应出现一次")

    lines = _read_lines(log_dir)
    assert len(lines) == 1 and "只应出现一次" in lines[0]


def test_console_and_file_levels_are_independent(tmp_path, capsys):
    """console_level 只管控制台，level 只管文件。"""
    log_dir = tmp_path / "logdir"
    configure_from_config(
        {
            "enabled": True,
            "directory": str(log_dir),
            "level": "WARNING",
            "console_level": "ERROR",
        }
    )
    logger = get_logger("LevelModule")
    logger.warning("只进文件")
    logger.error("两边都有")

    text = _read_lines(log_dir)
    assert any("只进文件" in line for line in text)
    assert not any("只进控制台" in line for line in text)
    stderr = capsys.readouterr().err
    assert "两边都有" in stderr
    assert "只进文件" not in stderr


def test_exc_flag_records_exception_in_text(tmp_path):
    """exc=True 时文本日志里带异常信息。"""
    log_dir = tmp_path / "logdir"
    configure_from_config({"enabled": True, "format": "text", "directory": str(log_dir)})
    try:
        raise RuntimeError("原始错误")
    except RuntimeError:
        get_logger("ExcModule").error("失败了", exc=True)

    text = "\n".join(_read_lines(log_dir))
    assert "失败了" in text and "RuntimeError" in text and "原始错误" in text
