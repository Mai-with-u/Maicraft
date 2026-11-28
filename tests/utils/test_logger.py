from maicraft.utils.logger import LoggerManager, get_logger, get_recent_logs, setup_advanced_logging


def test_recent_logs_keep_the_bound_module_name():
    setup_advanced_logging(level="INFO", log_to_file=False)
    get_logger("SomeModule").info("日志模块迁移后的冒烟测试")

    entries = get_recent_logs(limit=20, module="SomeModule")

    assert any("冒烟测试" in entry["message"] for entry in entries)


def test_default_initialization_does_not_create_logs_dir(tmp_path, monkeypatch):
    """不调用任何 setup、直接取 logger 并记一条日志：运行目录里不建 logs/，也不写日志文件。"""
    monkeypatch.chdir(tmp_path)
    manager = LoggerManager()
    manager.get_logger("FreshModule").info("默认初始化不写文件")

    assert not (tmp_path / "logs").exists()
    assert not list(tmp_path.iterdir()), "运行目录里不应出现任何新文件"
