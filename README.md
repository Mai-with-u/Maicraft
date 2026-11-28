# Maicraft

Maicraft 是一个独立运行的程序，通过 [MaiCraftMod](https://github.com/Mai-with-u/MaiCraftMod) 操作 Minecraft 里的一个角色。程序常驻运行：没事时挂着等 mod 的事件，有必要时才调用大模型，由模型决定给 mod 下达什么目标。mod 像正常玩家那样用游戏的正常交互把目标做成，并如实报告结果。

仓库地址：<https://github.com/Mai-with-u/Maicraft>

> 当前实现还没有做过实机验证，请先在备份过的测试世界里试用。

## 前提

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- 装了 MaiCraftMod 的 Minecraft 客户端
- agent 必须和游戏在同一台机器上：mod 的 MCP 端点只允许本机访问

## 安装与运行

Windows 下双击 `start.cmd`，它先安装依赖（`uv sync`）再启动程序。参数会原样传给程序，所以也可以 `start.cmd --check`。

或者手动执行：

```bash
uv sync
uv run maicraft            # 常驻运行，Ctrl+C 退出
uv run maicraft --check    # 连一次 mod，打印握手信息与角色、目标、技能概况后退出
uv run maicraft --version  # 打印版本号
uv run maicraft --config 其他路径.toml  # 指定配置文件，默认用运行目录下的 config.toml
```

第一次运行时，程序会从 `config-template.toml` 复制生成 `config.toml`。运行前必须先填 `[llm]` 里的模型和 API key。之后模板更新时，程序会升级旧配置文件（先备份，保留你的设置和注释）。

## 配置

配置文件是 TOML 格式，逐段说明（字段含义也写在模板的注释里）：

- `[llm]`：调用的模型。`model`（模型名称）、`api_key`（API 密钥，必须自己填）、`base_url`（API 地址，留空用服务方默认）、`temperature`、`max_tokens`、`supports_vision`（模型能不能看图；不能时画面只取旁边的文字）、`request_timeout_s`。
- `[mod]`：与 mod 的连接。`url`（mod 的 MCP 端点地址，默认 `http://127.0.0.1:8766/mcp`）、`events_wait_ms`（事件长轮询的单次等待毫秒数）。
- `[agent]`：agent 行为。`max_steps_per_turn`（一轮对话里最多执行的动作步数）、`idle_replan_minutes`（空闲多少分钟后主动叫醒模型，0 表示不叫醒）、`max_empty_turns`（连续多少轮没有实际动作后停下）、`long_term_goal`（长期目标，原样放进提示词）、`keep_recent_results`（提示词里保留最近几条动作结果）。
- `[chat]`：公屏聊天。`mention_names`（除角色名之外还接受哪些称呼）、`batch_window_ms`（把相邻消息合并处理的等待毫秒数）。
- `[maigo]`：MaiGO 共享记忆服务的对接。目前只定义了接口，还没有接入；`enabled = true` 时程序会打一条警告并按未启用运行。

另有 `[logging]`（日志级别、日志文件等）和 `[inner]`（配置格式版本，不要手动改）两段，一般不用动。

## 运行时会生成的文件

都在运行目录下：

- `config.toml`：你的配置文件（第一次运行时从模板生成）。
- `logs/`：日志文件。
- `data/notes/`：模型写给自己的笔记和待办，按 Minecraft 存档分开，一个存档一个 JSON 文件。
- `usage/`：模型用量记录，一个模型一个 JSON 文件，含 token 数和按内置价格表估算的费用。

## 它怎么工作

程序通过 mod 内嵌的 MCP 端点连到游戏，用长轮询读两条事件流：角色事件流和游戏公屏事件流。以下情况会叫醒模型跑一轮对话：

- 角色身上发生了它自己处理不了的事（身体需求没人管、死亡），或计时到点；
- 模型下达的目标提问、被暂停、结束或长时间停滞；
- 公屏上有私聊或点名（原话里出现角色名或配置的称呼）的消息；其余公屏消息只进最近的聊天记录，不叫醒模型；
- 空闲超过 `idle_replan_minutes` 分钟时主动叫醒，让模型重新想想做什么。

一轮对话里，模型可以调用 mod 的四个工具：`observe`（看自己和周围）、`lookup`（查能力与资料）、`execute`（下达目标）、`goal`（查看与管理目标），以及程序自己提供的三个本地工具：`notes`（读写笔记和待办）、`read_skill`（读 mod 发布的技能正文）、`recall`（取回之前被收起的工具结果原文）。工具结果太长时在提示词里换成占位，原文按编号留着，模型可以用 `recall` 取回。

给模型看的完整提示词在 `src/maicraft/agent/prompts/system.md`。

## 旧版本

基于 mineflayer 的旧实现固定在 tag `mineflayer-legacy`，不再维护。

## 许可证

[MIT](LICENSE)
