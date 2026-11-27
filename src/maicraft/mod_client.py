"""连接 MaiCraftMod 内嵌的 MCP 端点。

MaiCraftMod 在游戏客户端进程里开一个只允许本机访问的 MCP 端点，提供 observe、lookup、
execute、goal、events 五个工具，以及一批只读的技能资源。本模块只管连接这一层：
握手、会话、超时、调用失败后在下一次调用前重连；回复的内容怎么读在 agent/mod_protocol.py。

调用工具用 fastmcp 的 call_tool_mcp 取协议原样的结果。mod 把业务上的拒绝（参数不对、
角色不在世界里）也标成工具报错返回，这些是给模型看的反馈，不是连接出了问题，
所以不能让它们在这里变成异常。

握手固定用 initialize 这一种。fastmcp 4 默认会先试探更新的握手方式，失败再退回
initialize；mod 目前只实现了 initialize，试探请求在它那里会被拒绝。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport
from mcp_types import (
    BlobResourceContents,
    CallToolResult,
    Implementation,
    Resource,
    TextResourceContents,
    Tool,
)

from maicraft.utils.logger import get_logger

logger = get_logger("ModClient")

DEFAULT_URL = "http://127.0.0.1:8766/mcp"


class ModUnavailable(Exception):
    """连不上端点，或者一次调用途中连接断了。稍后重试即可。"""


ClientFactory = Callable[[], Client]


def _installed_version() -> str:
    try:
        return version("maicraft")
    except PackageNotFoundError:
        return "unknown"


def _is_connected(client: Client) -> bool:
    # fastmcp 3 里 is_connected 是属性，4 里是方法。
    probe = client.is_connected
    return probe() if callable(probe) else bool(probe)


class ModClient:
    """MaiCraftMod 端点的连接。所有方法都要在事件循环里调用。"""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        request_timeout_s: float = 30.0,
        connect_timeout_s: float = 10.0,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.url = url
        self.request_timeout_s = request_timeout_s
        self.connect_timeout_s = connect_timeout_s
        self._client_factory = client_factory or self._build_client
        self._client: Client | None = None
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._last_error = ""

    @property
    def connected(self) -> bool:
        client = self._client
        return self._connected and client is not None and _is_connected(client)

    @property
    def last_error(self) -> str:
        return self._last_error

    def _build_client(self) -> Client:
        return Client(
            StreamableHttpTransport(self.url),
            name="maicraft",
            mode="legacy",
            timeout=self.request_timeout_s,
            init_timeout=self.connect_timeout_s,
            client_info=Implementation(name="maicraft", version=_installed_version()),
        )

    async def connect(self) -> None:
        """建立连接；已经连着时不做任何事。连不上时抛 ModUnavailable。"""
        async with self._connect_lock:
            if self.connected:
                return
            await self._close_client()
            try:
                client = self._client_factory()
                await client.__aenter__()
            except Exception as exc:
                self._client = None
                self._connected = False
                self._last_error = f"连不上 MaiCraftMod 的端点 {self.url}：{type(exc).__name__}: {exc}"
                raise ModUnavailable(self._last_error) from exc
            self._client = client
            self._connected = True
            self._last_error = ""
            server = client.server_info
            logger.info(
                f"已连接 MaiCraftMod：{server.name if server else '?'} "
                f"{server.version if server else '?'}（协议 {client.protocol_version}）"
            )

    async def close(self) -> None:
        async with self._connect_lock:
            await self._close_client()

    async def _close_client(self) -> None:
        client, self._client, self._connected = self._client, None, False
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:
            logger.warning(f"关闭与 MaiCraftMod 的连接时出错：{type(exc).__name__}: {exc}")

    async def ping(self) -> None:
        await self._request("ping", lambda client: client.ping())

    async def list_tools(self) -> list[Tool]:
        return await self._request("tools/list", lambda client: client.list_tools())

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> CallToolResult:
        """调用一个工具，返回协议原样的结果。工具报错不算异常，看结果的 is_error。"""

        def send(client: Client) -> Awaitable[CallToolResult]:
            if timeout_s is None:
                return client.call_tool_mcp(name, arguments)
            return client.call_tool_mcp(name, arguments, timeout=timeout_s)

        return await self._request(f"tools/call {name}", send)

    async def list_resources(self) -> list[Resource]:
        return await self._request("resources/list", lambda client: client.list_resources())

    async def read_resource(self, uri: str) -> list[TextResourceContents | BlobResourceContents]:
        result = await self._request(f"resources/read {uri}", lambda client: client.read_resource_mcp(uri))
        return list(result.contents)

    def describe_server(self) -> dict[str, Any]:
        """握手时对方报上的信息；没连上时是空字典。"""
        client = self._client
        if client is None or not self.connected:
            return {}
        server = client.server_info
        return {
            "name": server.name if server else "",
            "version": server.version if server else "",
            "protocol_version": client.protocol_version,
            "instructions": client.instructions or "",
        }

    async def _request(self, what: str, send: Callable[[Client], Awaitable[Any]]) -> Any:
        if not self.connected:
            await self.connect()
        client = self._client
        if client is None:
            raise ModUnavailable("还没有连上 MaiCraftMod")
        try:
            return await send(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 分不清是连接断了还是这一条请求本身出错，一律当作连接不可靠：
            # 下一次调用前重新握手，代价只是本机的一次往返。
            self._connected = False
            self._last_error = f"{what} 失败：{type(exc).__name__}: {exc}"
            raise ModUnavailable(self._last_error) from exc
