"""入口模块的测试：自检文字、事件长轮询的超时、包装与版本信息。

测试只组装 ModClient 与假件，不启动常驻循环、不联网。
"""

import asyncio
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError

import pytest

import maicraft.main as main_module
from maicraft.agent.memory_service import DisabledMemoryService
from maicraft.config import MaicraftConfig
from maicraft.main import build_agent, build_call, build_memory, check_mod, main
from maicraft.mod_client import ModClient, ModUnavailable
from tests.fakes import SKILL_TEXT, SKILL_URI, FakeModClient, ok_envelope


def make_mod(prepare: Callable[[FakeModClient], None] | None = None) -> tuple[ModClient, FakeModClient]:
    """造一个以假客户端为后端的 ModClient；prepare 给客户端预设剧本，同时返回假件供断言。"""

    def factory() -> FakeModClient:
        client = FakeModClient()
        if prepare is not None:
            prepare(client)
        return client

    fake = factory()
    return ModClient(client_factory=lambda: fake), fake


def prepare_self_and_goals(client: FakeModClient) -> None:
    client.reply(
        "observe",
        ok_envelope(
            {
                "name": "小明",
                "position": {"x": 3.6, "y": 64.0, "z": -7.2, "dimension": "minecraft:overworld"},
                "health": 18,
                "food": 15,
                "goal": {"doing": "空闲，没有主任务"},
            }
        ),
    )
    client.reply("goal", ok_envelope({"goals": [{"goal_id": 1}, {"goal_id": 2}]}))
    client.resource_texts[SKILL_URI] = SKILL_TEXT


async def test_check_mod_reports_handshake_self_goals_and_skills():
    mod, _fake = make_mod(prepare_self_and_goals)

    text = await check_mod(mod)

    assert "服务端：fake-maicraft 0（协议 2025-11-25）" in text
    assert "角色 小明：位置 (3.6, 64.0, -7.2)，生命 18，饱食度 15，正在 空闲，没有主任务" in text
    assert "目标 2 条" in text
    assert "技能 1 项" in text


async def test_check_mod_reports_tool_errors_instead_of_dying():
    def prepare(client: FakeModClient) -> None:
        client.reply("observe", {"ok": False, "error": {"code": "not_in_world", "message": "角色不在世界里"}})
        client.reply("goal", ok_envelope({"goals": []}))

    mod, _fake = make_mod(prepare)

    text = await check_mod(mod)

    assert "读不到自己：角色不在世界里" in text


async def test_check_mod_raises_when_mod_is_unreachable():
    client = FakeModClient()
    client.enter_error = ConnectionError("拒绝连接")
    mod = ModClient(client_factory=lambda: client)

    with pytest.raises(ModUnavailable):
        await check_mod(mod)


async def test_call_enlarges_timeout_for_events_only():
    def prepare(client: FakeModClient) -> None:
        client.reply("observe", ok_envelope({"what": "self"}))
        client.reply("events", ok_envelope({"stream_id": "s", "cursor": 0, "events": []}))

    mod, fake = make_mod(prepare)
    call = build_call(mod)

    await call("events", {"wait_ms": 55000})
    await call("observe", {"what": "self"})

    timeouts = {name: timeout for name, _arguments, timeout in fake.calls}
    assert timeouts["events"] == 65.0
    assert timeouts["observe"] is None


async def test_call_wraps_unavailability_into_no_reply():
    def prepare(client: FakeModClient) -> None:
        client.call_error = ConnectionError("连接断了")

    mod, _fake = make_mod(prepare)
    call = build_call(mod)

    reply = await call("observe", {"what": "self"})

    assert reply.ok is False
    assert reply.error_code == "no_reply"


def test_build_memory_returns_disabled_service_even_when_enabled() -> None:
    config = MaicraftConfig.model_validate({"maigo": {"enabled": True}})

    memory = build_memory(config)

    # MaiGO 接入尚未实现：开着配置也只能给空实现，main 会另打警告日志
    assert isinstance(memory, DisabledMemoryService)


def test_build_agent_assembles_a_resident_agent() -> None:
    config = MaicraftConfig.model_validate({"llm": {"api_key": "test-key"}})
    mod, _fake = make_mod()

    # Agent 默认时钟取事件循环时刻，构造要在事件循环里做（与 main 的常驻路径一致）
    async def build() -> object:
        return build_agent(config, mod)

    agent = asyncio.run(build())

    assert agent is not None


def test_main_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0

    out = capsys.readouterr().out.strip()
    assert out and out != "unknown"


def test_main_version_prints_unknown_without_package_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raise_missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(main_module, "package_version", raise_missing)

    assert main(["--version"]) == 0

    assert capsys.readouterr().out.strip() == "unknown"


def test_main_version_and_help_create_no_files(tmp_path, monkeypatch):
    """--version 与 --help 在加载配置、初始化日志之前就处理完：运行目录不出现新文件、不读 config.toml。"""
    monkeypatch.chdir(tmp_path)

    assert main(["--version"]) == 0

    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0

    assert not list(tmp_path.iterdir()), "运行目录里不应出现任何新文件"
