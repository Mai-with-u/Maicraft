from maicraft.utils.logger import get_logger, get_recent_logs, setup_advanced_logging


def test_recent_logs_keep_the_bound_module_name():
    setup_advanced_logging(level="INFO", log_to_file=False)
    get_logger("SomeModule").info("日志模块迁移后的冒烟测试")

    entries = get_recent_logs(limit=20, module="SomeModule")

    assert any("冒烟测试" in entry["message"] for entry in entries)
