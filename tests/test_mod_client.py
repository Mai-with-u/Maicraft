import asyncio
from collections.abc import Callable

import mcp_types
import pytest
from fastmcp import Client

from maicraft.mod_client import ModClient, ModUnavailable
from tests import fakes
from tests.fakes import SKILL_TEXT, SKILL_URI, TOOL_NAMES, FakeModClient, error_envelope, ok_envelope


class RecordingFactory:
    """每造一个假客户端就记下来，用来断言重连了几次。prepare 用来给每个新客户端预设剧本。"""

    def __init__(self, prepare: Callable[[FakeModClient], None] | None = None) -> None:
        self.clients: list[FakeModClient] = []
        self.prepare = prepare

    def __call__(self) -> FakeModClient:
        client = FakeModClient()
        if self.prepare is not None:
            self.prepare(client)
        self.clients.append(client)
        return client

    @property
    def last(self) -> FakeModClient:
        return self.clients[-1]


async def test_connect_reports_what_the_server_said_at_handshake():
    factory = RecordingFactory()
    mod = ModClient(client_factory=factory)

    assert mod.connected is False
    await mod.connect()

    assert mod.connected is True
    assert mod.describe_server() == {
        "name": "fake-maicraft",
        "version": "0",
        "protocol_version": "2025-11-25",
        "instructions": "假 mod 的说明",
    }


async def test_connecting_twice_keeps_the_same_connection():
    factory = RecordingFactory()
    mod = ModClient(client_factory=factory)

    await mod.connect()
    await mod.connect()

    assert len(factory.clients) == 1


async def test_connect_failure_names_the_cause_and_leaves_us_disconnected():
    factory = RecordingFactory()

    def failing() -> FakeModClient:
        client = factory()
        client.enter_error = RuntimeError("Client failed to connect: 连接被拒绝")
        return client

    mod = ModClient(client_factory=failing)

    with pytest.raises(ModUnavailable, match="连接被拒绝"):
        await mod.connect()

    assert mod.connected is False
    assert mod.describe_server() == {}
    assert "连接被拒绝" in mod.last_error


async def test_call_tool_returns_the_raw_result_including_business_refusals():
    factory = RecordingFactory()
    mod = ModClient(client_factory=factory)
    factory_reply = ok_envelope({"goal_id": 7, "state": "running"})
    refusal = error_envelope("invalid_parameter", "参数有 2 处问题")

    await mod.connect()
    factory.last.reply("execute", factory_reply)
    factory.last.reply("goal", refusal)

    accepted = await mod.call_tool("execute", {"goal": {"ability": "maicraft:sleep"}})
    refused = await mod.call_tool("goal", {"operation": "answer", "goal_id": 7, "answer": "x"})

    assert accepted.is_error is False
    assert accepted.content[0].text == '{"ok": true, "data": {"goal_id": 7, "state": "running"}}'
    assert refused.is_error is True
    assert '"code": "invalid_parameter"' in refused.content[0].text
    # 业务拒绝是正常的工具回复，不该让连接被当成断了。
    assert mod.connected is True


async def test_call_tool_passes_a_per_call_timeout_and_skips_it_when_not_given():
    factory = RecordingFactory()
    mod = ModClient(client_factory=factory)
    await mod.connect()
    factory.last.reply("events", ok_envelope())
    factory.last.reply("observe", ok_envelope())

    await mod.call_tool("events", {"wait_ms": 25000}, timeout_s=40.0)
    await mod.call_tool("observe", {"what": "self"})

    assert factory.last.calls == [("events", {"wait_ms": 25000}, 40.0), ("observe", {"what": "self"}, None)]


def prepare_observe(client: FakeModClient) -> None:
    client.reply("observe", ok_envelope({"after": "reconnect"}))


async def test_a_failed_call_marks_us_disconnected_and_the_next_call_reconnects():
    factory = RecordingFactory(prepare=prepare_observe)
    mod = ModClient(client_factory=factory)
    await mod.connect()
    factory.last.call_error = RuntimeError("Connection closed")

    with pytest.raises(ModUnavailable, match="Connection closed"):
        await mod.call_tool("observe", {"what": "self"})

    assert mod.connected is False

    # 重连会换一个新的客户端，它没有继承旧客户端的故障。
    result = await mod.call_tool("observe", {"what": "self"})

    assert len(factory.clients) == 2
    assert "reconnect" in result.content[0].text
    assert factory.clients[0].closed is True


async def test_callers_that_lost_the_connection_at_once_reconnect_only_once():
    factory = RecordingFactory(prepare=prepare_observe)
    mod = ModClient(client_factory=factory)
    await mod.connect()
    factory.last.call_error = RuntimeError("Connection closed")
    await asyncio.gather(
        mod.call_tool("observe", {"what": "self"}),
        mod.call_tool("observe", {"what": "scene"}),
        return_exceptions=True,
    )

    await asyncio.gather(mod.call_tool("observe", {"what": "self"}), mod.call_tool("observe", {"what": "scene"}))

    assert len(factory.clients) == 2


async def test_cancelling_a_call_does_not_count_as_a_connection_failure():
    factory = RecordingFactory()
    mod = ModClient(client_factory=factory)
    await mod.connect()
    factory.last.reply("observe", ok_envelope())
    factory.last.call_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await mod.call_tool("observe", {"what": "self"})

    assert mod.connected is True


async def test_list_tools_and_read_resources():
    factory = RecordingFactory()
    mod = ModClient(client_factory=factory)
    await mod.connect()
    factory.last.resource_texts[SKILL_URI] = SKILL_TEXT

    tools = await mod.list_tools()
    resources = await mod.list_resources()
    contents = await mod.read_resource(SKILL_URI)

    assert [tool.name for tool in tools] == list(TOOL_NAMES)
    assert [str(resource.uri) for resource in resources] == [SKILL_URI]
    assert [content.text for content in contents] == [SKILL_TEXT]


async def test_close_is_idempotent_and_leaves_us_disconnected():
    factory = RecordingFactory()
    mod = ModClient(client_factory=factory)
    await mod.connect()

    await mod.close()
    await mod.close()

    assert mod.connected is False
    assert factory.last.closed is True


async def test_real_fastmcp_client_talks_to_an_in_process_server():
    """用真正的 fastmcp 客户端走一遍，确认我们对它接口的用法是对的。"""
    server = fakes.fake_mod_server()
    mod = ModClient(client_factory=lambda: Client(server, mode="legacy"))
    await mod.connect()

    tools = await mod.list_tools()
    picture = await mod.call_tool("observe", {"what": "picture"}, timeout_s=5.0)
    refused = await mod.call_tool("lookup", {"id": "maicraft:nope"})
    accepted = await mod.call_tool("execute", {"goal": {"ability": "maicraft:sleep"}}, timeout_s=5.0)
    resources = await mod.list_resources()
    contents = await mod.read_resource(SKILL_URI)
    described = mod.describe_server()
    await mod.close()

    assert [tool.name for tool in tools] == list(TOOL_NAMES)
    assert tools[0].input_schema["type"] == "object"
    assert [type(block).__name__ for block in picture.content] == ["TextContent", "ImageContent"]
    assert picture.content[1].mime_type == "image/jpeg"
    assert refused.is_error is True
    assert '"code": "unknown_id"' in refused.content[0].text
    assert accepted.is_error is False
    assert isinstance(accepted, mcp_types.CallToolResult)
    assert [str(resource.uri) for resource in resources] == [SKILL_URI]
    assert [content.text for content in contents] == [SKILL_TEXT]
    assert described["protocol_version"] == "2025-11-25"
    assert described["name"] == "fake-maicraft"
