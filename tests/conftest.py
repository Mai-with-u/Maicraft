from maicraft.utils.logger import setup_advanced_logging

# 测试进程不往运行目录写日志文件。日志管理器第一次被取用时会按默认配置初始化，
# 默认配置会在当前目录建 logs 目录并写文件，所以这里抢在测试模块导入之前关掉文件输出。
setup_advanced_logging(level="WARNING", log_to_file=False)
