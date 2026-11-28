# CLAUDE.md

Maicraft：基于 MaiCraftMod 的独立常驻 Minecraft agent。程序挂着等 mod 的事件，有必要时才调用大模型，由模型给 mod 下达目标。

## 常用命令

```bash
uv run pytest -q             # 跑全部测试，提交前必须全绿
uv run ruff check            # 代码检查
uv run ruff format --check   # 格式检查
uv run maicraft --check      # 连一次 mod，打印握手与角色、目标、技能概况后退出
uv run maicraft              # 常驻运行
```

测试提交前三样都要通过。测试与 ruff 的约定见下面"约定"。

## 目录结构与模块职责

```
src/maicraft/
├── main.py                  # 命令行入口：解析参数（--config / --check / --version）、组装各模块、常驻主循环与自检
├── config.py                # 配置模型（pydantic）与配置文件加载：配置模板打在包里，不存在就从包内模板创建，模板更新时升级旧文件
├── mod_client.py            # 连接 MaiCraftMod 内嵌的 MCP 端点：握手、会话、超时、断线后重连
├── agent/                   # 与 mod 打交道的协议层、状态层与事件跟踪
│   ├── agent.py             # 主循环：读 mod 的事件，有必要时叫醒模型跑一轮，没事就挂着等
│   ├── mod_protocol.py      # mod 五个工具的注册名、统一回复格式与目标运行状态，只在这里解读一次
│   ├── wake_rules.py        # 叫醒规则：一种处境要不要叫醒模型、给模型看的那句话怎么写（纯函数）
│   ├── wake_queue.py        # 唤醒队列：立刻叫醒的内容与下次顺带捎上的内容
│   ├── chat_filter.py       # 公屏筛选与长话拆分：哪些公屏消息唤醒模型，超长发言怎么拆成几条发
│   ├── event_stream.py      # 事件流长轮询的读取与游标管理（角色事件流和公屏事件流共用）
│   ├── goal_watch.py        # 后台目标跟踪：盯本 agent 下达的目标何时需要处理
│   ├── goal_registry.py     # 目标登记表：下达给 mod 的目标在内存里的记录与通知文案
│   ├── context.py           # 一轮对话里工具结果的收起与取回：太长或太旧时换成占位，原文按编号留着
│   ├── local_tools.py       # 程序自己加给模型的三个本地工具：notes、read_skill、recall
│   ├── notes.py             # 模型写给自己的笔记和待办：每个 Minecraft 存档一份 JSON 文件
│   ├── skills.py            # mod 发布的技能：从 MCP 资源清单认出 skill:// 资源，按名字读正文
│   ├── memory_service.py    # 共享记忆服务（MaiGO）的接口与空实现；接入还没实现
│   ├── world_identity.py    # 从 mod 报的 world 认出当前 Minecraft 存档，判定"换了存档"
│   ├── mod_facts.py         # 解析目标结果里的连锁挖矿统计与取材留坑回填事实
│   └── prompts/system.md    # 给模型看的系统提示词
├── openai_client/
│   ├── llm_request.py       # 调用 OpenAI 兼容接口：单次请求、重试、图片处理
│   ├── modelconfig.py       # 模型连接参数的简单载体
│   ├── token_usage_manager.py  # token 用量记录：按模型落 JSON 文件，估算费用
│   └── model_price.toml     # 用量费用估算用的价格表
└── utils/
    └── logger.py            # 基于 loguru 的日志：configure_from_config 按配置挂控制台与文件输出（轮转、保留、压缩），get_logger 给各模块返回带模块名标注的日志接口
```

## 约定

这些约定在日常改代码时都要遵守：

**命名与注释**。名字直接说事物是什么，不了解这个项目历史的开发者第一眼要能看懂；不用比喻（不把东西叫成"账本""旗标"之类），不造新词，不自造分类词。代码标识符用英文：类名 PascalCase，函数和变量 snake_case，私有成员前导下划线。注释和文档字符串用中文，只写"这段代码是什么、为什么这样设计"，不写变更记录和步骤编号。给模型看的消息前缀只用四种：`[系统]`（程序的通知）、`[角色]`（游戏角色身上发生的事）、`[游戏公屏]`、`[计时]`。

**时间**。代码里的时刻和时长用毫秒整数，变量名以 `_ms` 结尾；时钟从构造函数注入，测试里用假时钟推进。配置文件里的字段可以用对使用者友好的单位（比如分钟）。

**与 mod 的接口**。调 mod 的工具走 fastmcp 的 `call_tool_mcp`，拿协议原样的结果。mod 的业务拒绝（`ok: false`）是给模型看的反馈，是数据，不能当成异常或连接故障。不引入 Amaidesu 的运行时依赖，不和 Amaidesu 通信。

**代码质量**。所有函数和方法写完整的类型注解。服务对象通过构造函数注入，测试里用假件替换，不去 patch 私有成员。不为"将来可能用"写分支，只写当前任务要求的东西。不新增依赖。

**测试**。测试放在 `tests/` 下与 `src/` 对应的目录；假件统一放在 `tests/fakes.py`，只追加、不改变已有假件的行为。测试里不 sleep 等真实时间，不联网，时间用注入的假时钟推进。提交前 `uv run pytest -q` 全绿，`uv run ruff check` 和 `uv run ruff format --check` 通过。

**提交信息**。遵循 Conventional Commits：`type(scope): subject`；subject 用中文、动词开头、句末不加句号、约 30 个字符以内；fix 类 subject 必须点名被修复的缺陷，用"修复 X 因 Y 而 Z"的形态。改动限于一个模块时 scope 写模块名，跨整个仓库时不带。提交信息要说人话：任何人不看 diff 也能看懂，不用比喻和黑话，不写本地编号、过程标记和机器本地信息。版本号完全由用户决定，不自作主张推进版本号。

## 发布流程

dev 是开发主线，平时不动版本号；发布时的步骤与版本号规则见 [docs/release.md](docs/release.md)。

请用中文与我交流，并尽可能使用中文注释。
