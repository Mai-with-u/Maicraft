"""LLMClient 的 chat_messages 与错误分类测试，用假的 OpenAI 客户端，不联网。"""

import json
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

import maicraft.openai_client.token_usage_manager as token_usage_manager_module
from maicraft.openai_client.llm_request import LLMClient
from maicraft.openai_client.modelconfig import ModelConfig
from maicraft.openai_client.token_usage_manager import TokenUsageManager


@pytest.fixture(autouse=True)
def _redirect_usage_dir(tmp_path_factory):
    """把 LLMClient 内部建的全局用量管理器指到临时目录，避免在运行目录留下 usage 文件。"""
    usage_dir = tmp_path_factory.mktemp("usage")
    token_usage_manager_module.global_token_manager = TokenUsageManager(usage_dir=str(usage_dir), use_global=False)


def make_model_config(**kwargs) -> ModelConfig:
    defaults = dict(
        model_name="test-model",
        api_key="key",
        base_url="http://localhost/v1",
        max_tokens=512,
        temperature=0.5,
    )
    defaults.update(kwargs)
    return ModelConfig(**defaults)


class FakeCompletions:
    """替身：记录请求参数，返回预设的回复或抛出预设的异常。"""

    def __init__(self, response=None, error: Optional[Exception] = None):
        self.response = response
        self.error = error
        self.kwargs: Optional[Dict[str, Any]] = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    """替身：只实现 LLMClient 用到的 chat.completions.create。"""

    def __init__(self, response=None, error: Optional[Exception] = None):
        self.chat = SimpleNamespace(completions=FakeCompletions(response, error))


def make_response(tool_calls=None, content: str = "你好") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[choice], usage=usage, model="test-model")


def make_tool_call() -> SimpleNamespace:
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="observe", arguments='{"radius": 8}'),
    )


def make_status_error(exc_cls, status_code: int, message: str) -> APIStatusError:
    """手工拼出 openai 状态错误需要的 response/request 属性，不依赖 httpx。"""
    request = SimpleNamespace(headers={})
    response = SimpleNamespace(
        status_code=status_code,
        request=request,
        headers={},
        history=None,
        next_request=None,
        text=message,
    )
    return exc_cls(message, response=response, body=None)


def make_connection_error(exc_cls, message: str) -> Exception:
    request = SimpleNamespace(headers={})
    if exc_cls is APITimeoutError:
        return APITimeoutError(request=request)
    return APIConnectionError(message=message, request=request)


async def call_chat_messages(client) -> Dict[str, Any]:
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "看看周围"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    return await client.chat_messages(messages)


async def call_chat_messages_with_multimodal(client) -> Dict[str, Any]:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这是什么"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
            ],
        }
    ]
    return await client.chat_messages(messages)


async def test_chat_messages_sends_conversation_as_is_and_records_usage(tmp_path):
    fake = FakeClient(make_response())
    config = make_model_config()
    llm = LLMClient(config, client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["success"] is True
    assert result["content"] == "你好"
    assert result["model"] == "test-model"
    assert result["finish_reason"] == "stop"
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    sent = fake.chat.completions.kwargs
    assert sent["model"] == "test-model"
    # 整段对话原样发出，四种角色都在
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "tool"]
    assert sent["messages"] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "看看周围"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    assert "tools" not in sent
    assert sent["temperature"] == 0.5
    assert sent["max_tokens"] == 512

    usage_file = tmp_path / "usage" / "test-model_usage.json"
    saved = json.loads(usage_file.read_text(encoding="utf-8"))
    assert saved["total_tokens"] == 15
    assert saved["total_calls"] == 1


async def test_chat_messages_sends_multimodal_content_list(tmp_path):
    fake = FakeClient(make_response())
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    await call_chat_messages_with_multimodal(llm)

    sent = fake.chat.completions.kwargs
    content = sent["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "这是什么"}
    assert content[1]["type"] == "image_url"


async def test_chat_messages_parses_tool_calls(tmp_path):
    fake = FakeClient(make_response(tool_calls=[make_tool_call()]))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "observe", "arguments": '{"radius": 8}'},
        }
    ]


async def test_chat_messages_forwards_tools_and_overrides(tmp_path):
    fake = FakeClient(make_response())
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    tools = [{"type": "function", "function": {"name": "observe"}}]
    result = await llm.chat_messages(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        temperature=0.9,
        max_tokens=128,
    )

    assert result["success"] is True
    sent = fake.chat.completions.kwargs
    assert sent["tools"] == tools
    assert sent["tool_choice"] == "auto"
    assert sent["temperature"] == 0.9
    assert sent["max_tokens"] == 128


async def test_chat_messages_passes_per_call_timeout(tmp_path):
    fake = FakeClient(make_response())
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    await llm.chat_messages([{"role": "user", "content": "hi"}], timeout_s=7.5)

    assert fake.chat.completions.kwargs["timeout"] == 7.5


async def test_chat_messages_timeout_error_is_transient(tmp_path):
    fake = FakeClient(error=make_connection_error(APITimeoutError, "timeout"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["success"] is False
    assert result["content"] is None
    assert result["error"]
    assert result["error_kind"] == "transient"


async def test_chat_messages_connection_error_is_transient(tmp_path):
    fake = FakeClient(error=make_connection_error(APIConnectionError, "connection refused"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "transient"


async def test_chat_messages_rate_limit_error_is_transient(tmp_path):
    fake = FakeClient(error=make_status_error(RateLimitError, 429, "rate limit"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "transient"


async def test_chat_messages_server_error_is_transient(tmp_path):
    fake = FakeClient(error=make_status_error(APIStatusError, 503, "service unavailable"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "transient"


async def test_chat_messages_authentication_error_is_fatal(tmp_path):
    fake = FakeClient(error=make_status_error(AuthenticationError, 401, "invalid api key"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "fatal"


async def test_chat_messages_permission_error_is_fatal(tmp_path):
    fake = FakeClient(error=make_status_error(PermissionDeniedError, 403, "forbidden"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "fatal"


async def test_chat_messages_insufficient_balance_as_bad_request_is_fatal(tmp_path):
    fake = FakeClient(error=make_status_error(BadRequestError, 400, "余额不足，请充值"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "fatal"


async def test_chat_messages_plain_bad_request_is_invalid(tmp_path):
    fake = FakeClient(error=make_status_error(BadRequestError, 400, "invalid message format"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "invalid"


async def test_chat_messages_other_error_is_unknown(tmp_path):
    fake = FakeClient(error=ValueError("bad thing"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await call_chat_messages(llm)

    assert result["error_kind"] == "unknown"


async def test_client_created_by_llmclient_gets_configured_timeout():
    llm = LLMClient(make_model_config(request_timeout_s=60.0))

    assert llm.client.timeout == 60.0


async def test_default_timeout_is_120_seconds():
    llm = LLMClient(make_model_config())

    assert llm.client.timeout == 120.0


async def test_given_client_is_used_as_is():
    fake = FakeClient(make_response())
    llm = LLMClient(make_model_config(), client=fake)

    assert llm.client is fake


async def test_chat_completion_behavior_unchanged(tmp_path):
    fake = FakeClient(make_response(content="旧接口"))
    llm = LLMClient(make_model_config(), client=fake)
    llm.token_manager = TokenUsageManager(usage_dir=str(tmp_path / "usage"), use_global=False)

    result = await llm.chat_completion("你好", system_message="你是助手", tools=[{"type": "function"}])

    assert result["success"] is True
    assert result["content"] == "旧接口"
    sent = fake.chat.completions.kwargs
    assert sent["messages"] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
    ]
    assert sent["tool_choice"] == "auto"
    # 旧接口失败时没有 error_kind 字段
    failing = FakeClient(error=ValueError("x"))
    llm2 = LLMClient(make_model_config(), client=failing)
    llm2.token_manager = llm.token_manager
    failed = await llm2.chat_completion("你好")
    assert failed == {"success": False, "error": failed["error"], "content": None}
    assert "error_kind" not in failed


def test_timeout_reaches_self_created_client():
    """自己创建客户端时，配置里的超时会传给 AsyncOpenAI（float 会展开成 httpx.Timeout 的各阶段）。"""
    llm = LLMClient(make_model_config(request_timeout_s=45.0))

    assert llm.client.timeout == 45.0
