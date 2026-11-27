"""命令行入口：解析参数、组装各模块、提供自检与版本信息。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from maicraft.agent.agent import Agent
from maicraft.agent.memory_service import DisabledMemoryService, MemoryService
from maicraft.agent.mod_protocol import (
    EVENTS,
    GOAL,
    OBSERVE,
    McpCall,
    ModReply,
    no_reply,
    reply_of,
)
from maicraft.agent.notes import Notes
from maicraft.agent.skills import SkillCatalog
from maicraft.config import MaicraftConfig, load_config
from maicraft.mod_client import ModClient, ModUnavailable
from maicraft.openai_client.llm_request import LLMClient
from maicraft.openai_client.modelconfig import ModelConfig
from maicraft.utils.logger import get_logger, setup_advanced_logging

logger = get_logger("Main")

# Ctrl+C 退出的进程退出码：128 加上 SIGINT 的信号号 2，与常见 Unix 约定一致。
INTERRUPT_EXIT_CODE = 130


def build_call(mod: ModClient) -> McpCall:
    """把 ModClient 的工具调用包装成 Agent 用的统一调用。

    事件长轮询在 mod 侧要等 wait_ms 才空手返回，请求超时按它放大，否则会被客户端
    先掐断、再被当成断线去重连。连接不可用是给主循环重试的信号，包装成统一回复。
    """

    async def call(name: str, arguments: dict[str, Any]) -> ModReply:
        timeout_s: float | None = None
        if name == EVENTS:
            wait_ms = arguments.get("wait_ms")
            if isinstance(wait_ms, (int, float)) and not isinstance(wait_ms, bool):
                timeout_s = max(mod.request_timeout_s, wait_ms / 1000 + 10)
        try:
            result = await mod.call_tool(name, arguments, timeout_s=timeout_s)
        except ModUnavailable as exc:
            return no_reply(str(exc))
        return reply_of(result)

    return call


def build_memory(config: MaicraftConfig) -> MemoryService:
    """按配置给记忆服务；MaiGO 接入还没实现，开着也只给空实现。"""
    if config.maigo.enabled:
        logger.warning("MaiGO 对接尚未实现，这次按未启用运行")
    return DisabledMemoryService()


def build_agent(config: MaicraftConfig, mod: ModClient) -> Agent:
    """按配置组装常驻主循环；与 mod 的连接由调用方持有，退出时负责关掉。"""
    llm = LLMClient(
        ModelConfig(
            model_name=config.llm.model,
            api_key=config.llm.api_key,
            base_url=config.llm.base_url,
            max_tokens=config.llm.max_tokens,
            temperature=config.llm.temperature,
            request_timeout_s=config.llm.request_timeout_s,
        )
    )
    notes = Notes(Path("data") / "notes")
    return Agent(
        config,
        llm=llm,
        call=build_call(mod),
        mod=mod,
        memory=build_memory(config),
        notes=notes,
    )


async def check_mod(mod: ModClient) -> str:
    """连一次 mod，汇总握手信息与角色、目标、技能的概况。连不上时抛 ModUnavailable。"""
    await mod.connect()
    server = mod.describe_server()
    lines = [
        f"服务端：{server.get('name') or '?'} {server.get('version') or '?'}"
        f"（协议 {server.get('protocol_version') or '?'}）",
        self_line(reply_of(await mod.call_tool(OBSERVE, {"what": "self"}))),
        goals_line(reply_of(await mod.call_tool(GOAL, {"operation": "list"}))),
    ]
    skills = SkillCatalog(mod)
    await skills.refresh()
    lines.append(f"技能 {len(skills.all())} 项")
    return "\n".join(lines)


def self_line(reply: ModReply) -> str:
    """把 observe(what=self) 的回复写成一行摘要。"""
    if not reply.ok:
        return f"读不到自己：{reply.error_message}"
    position = reply.data.get("position") if isinstance(reply.data.get("position"), dict) else {}
    pos = f"({position.get('x')}, {position.get('y')}, {position.get('z')})" if "x" in position else "未知"
    goal = reply.data.get("goal") if isinstance(reply.data.get("goal"), dict) else {}
    return (
        f"角色 {reply.data.get('name') or '?'}：位置 {pos}，"
        f"生命 {reply.data.get('health') or '?'}，饱食度 {reply.data.get('food') or '?'}，"
        f"正在 {goal.get('doing') or '未知'}"
    )


def goals_line(reply: ModReply) -> str:
    """把 goal(operation=list) 的回复写成一行条数。"""
    if not reply.ok:
        return f"读不到目标：{reply.error_message}"
    goals = reply.data.get("goals") if isinstance(reply.data.get("goals"), list) else []
    return f"目标 {len(goals)} 条"


def print_version() -> None:
    """打印包版本号；包元数据读不到时打印 unknown。"""
    try:
        print(package_version("maicraft"))
    except PackageNotFoundError:
        print("unknown")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maicraft", description="基于 MaiCraftMod 的常驻 Minecraft agent")
    parser.add_argument("--config", help="配置文件路径，默认用运行目录下的 config.toml")
    parser.add_argument("--check", action="store_true", help="连一次 mod 打印握手与状态后退出")
    parser.add_argument("--version", action="store_true", help="打印版本号")
    args = parser.parse_args(argv)

    if args.version:
        print_version()
        return 0

    config = load_config(args.config)
    logging_config = config.logging
    setup_advanced_logging(
        level=logging_config.level,
        enable_json=logging_config.enable_json,
        log_to_file=logging_config.log_to_file,
        log_dir=logging_config.log_dir,
        rotation=logging_config.rotation,
        retention=logging_config.retention,
        enable_hierarchical_logging=logging_config.enable_hierarchical_logging,
        max_recent_logs=logging_config.max_recent_logs,
    )

    if args.check:
        return run_check(config)
    return run_resident(config)


def run_check(config: MaicraftConfig) -> int:
    """连一次 mod 打印概况后退出；连不上时把原因写进标准错误。"""

    async def check_once() -> str:
        mod = ModClient(config.mod.url)
        try:
            return await check_mod(mod)
        finally:
            await mod.close()

    try:
        print(asyncio.run(check_once()))
    except ModUnavailable as exc:
        print(f"连不上 MaiCraftMod：{exc}", file=sys.stderr)
        return 1
    return 0


def run_resident(config: MaicraftConfig) -> int:
    """常驻运行主循环；Ctrl+C 时关掉与 mod 的连接后安静退出。"""

    async def run_and_close() -> None:
        mod = ModClient(config.mod.url)
        agent = build_agent(config, mod)
        try:
            await agent.run()
        finally:
            await mod.close()

    try:
        asyncio.run(run_and_close())
    except KeyboardInterrupt:
        return INTERRUPT_EXIT_CODE
    return 0
