from maicraft.utils.logger import configure_from_config

# 测试进程不往运行目录写日志文件；控制台只输出 WARNING 及以上，减少测试输出噪音。
configure_from_config({"enabled": False, "console_level": "WARNING"})
