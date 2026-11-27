import base64
import mimetypes
import os
from io import BytesIO
from typing import Any, Dict, List, Optional, Union

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from PIL import Image

from maicraft.openai_client.modelconfig import ModelConfig
from maicraft.openai_client.token_usage_manager import TokenUsageManager
from maicraft.utils.logger import get_logger


class LLMClient:
    """LLM调用客户端"""

    def __init__(self, model_config: Optional[ModelConfig], client: Optional[AsyncOpenAI] = None):
        """初始化LLM客户端

        Args:
            model_config: 模型配置
            client: 已有的 OpenAI 异步客户端；不给时按配置自己创建
        """
        self.model_config = model_config
        self.logger = get_logger("LLMClient")

        if client is not None:
            self.client = client
        else:
            # 初始化OpenAI客户端
            self.client = AsyncOpenAI(
                api_key=self.model_config.api_key,
                base_url=self.model_config.base_url,
                timeout=self.model_config.request_timeout_s,
            )

        self.logger.info(f"LLM客户端初始化完成，模型: {self.model_config.model_name}")

        # 初始化token使用量管理器
        self.token_manager = TokenUsageManager()

    def _infer_mime_from_bytes(self, data: bytes) -> str:
        """根据图片字节推断 MIME 类型，默认 image/png"""
        try:
            with Image.open(BytesIO(data)) as img:
                format_to_mime = {
                    "PNG": "image/png",
                    "JPEG": "image/jpeg",
                    "JPG": "image/jpeg",
                    "WEBP": "image/webp",
                    "GIF": "image/gif",
                }
                return format_to_mime.get(img.format, "image/png")
        except Exception:
            return "image/png"

    def _path_or_url_to_data_url(self, image: Union[str, bytes]) -> str:
        """将 URL/本地路径/字节 转为 URL 或 data URL"""
        if isinstance(image, str):
            if image.startswith("http://") or image.startswith("https://"):
                return image
            if os.path.exists(image) and os.path.isfile(image):
                with open(image, "rb") as f:
                    data = f.read()
                mime, _ = mimetypes.guess_type(image)
                if mime is None:
                    mime = self._infer_mime_from_bytes(data)
                b64 = base64.b64encode(data).decode("ascii")
                return f"data:{mime};base64,{b64}"
            return image
        elif isinstance(image, (bytes, bytearray)):
            data = bytes(image)
            mime = self._infer_mime_from_bytes(data)
            b64 = base64.b64encode(data).decode("ascii")
            return f"data:{mime};base64,{b64}"
        else:
            raise TypeError("image 参数必须为 str(路径或URL) 或 bytes")

    def _build_vision_user_content(
        self, prompt: str, images: Union[str, bytes, List[Union[str, bytes]]]
    ) -> List[Dict[str, Any]]:
        """构建多模态 user content 列表，兼容 OpenAI Chat Completions 识图格式"""
        contents: List[Dict[str, Any]] = []
        contents.append({"type": "text", "text": prompt})
        image_list: List[Union[str, bytes]] = images if isinstance(images, list) else [images]
        for img in image_list:
            url_or_data = self._path_or_url_to_data_url(img)
            contents.append({"type": "image_url", "image_url": {"url": url_or_data}})
        return contents

    async def chat_completion(
        self,
        prompt: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """异步聊天完成调用

        Args:
            prompt: 用户输入的提示
            tools: 工具列表，格式为OpenAI工具格式
            system_message: 系统消息
            temperature: 温度参数，覆盖配置中的默认值
            max_tokens: 最大token数，覆盖配置中的默认值

        Returns:
            包含响应结果的字典
        """
        try:
            # 构建消息列表
            messages = []

            if system_message:
                messages.append({"role": "system", "content": system_message})

            messages.append({"role": "user", "content": prompt})

            # 构建请求参数
            request_params = {
                "model": self.model_config.model_name,
                "messages": messages,
                "temperature": temperature or self.model_config.temperature,
            }

            if tools:
                request_params["tools"] = tools
                request_params["tool_choice"] = "auto"

            if max_tokens:
                request_params["max_tokens"] = max_tokens
            elif self.model_config.max_tokens:
                request_params["max_tokens"] = self.model_config.max_tokens

            return await self._create_and_parse(request_params)

        except Exception as e:
            error_msg = f"LLM请求失败: {str(e)}"
            self.logger.error(error_msg)
            return {"success": False, "error": error_msg, "content": None}

    async def chat_messages(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """发送 OpenAI 格式的整段对话

        Args:
            messages: 完整的对话消息列表，可含 assistant 的 tool_calls、tool 角色的结果、
                多模态内容列表，原样发送
            tools: 工具列表，格式为OpenAI工具格式
            temperature: 温度参数，覆盖配置中的默认值
            max_tokens: 最大token数，覆盖配置中的默认值
            timeout_s: 本次调用的超时秒数，不给时用客户端的默认超时

        Returns:
            成功时包含 success、content、model、usage、finish_reason，有工具调用时还有 tool_calls；
            失败时 success 为 False，带 error、content 和 error_kind（transient/fatal/invalid/unknown）
        """
        request_params: Dict[str, Any] = {
            "model": self.model_config.model_name,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.model_config.temperature,
        }

        if tools:
            request_params["tools"] = tools
            request_params["tool_choice"] = "auto"

        if max_tokens:
            request_params["max_tokens"] = max_tokens
        elif self.model_config.max_tokens:
            request_params["max_tokens"] = self.model_config.max_tokens

        if timeout_s is not None:
            request_params["timeout"] = timeout_s

        try:
            return await self._create_and_parse(request_params)
        except Exception as e:
            error_msg = f"LLM请求失败: {str(e)}"
            self.logger.error(error_msg)
            return {
                "success": False,
                "error": error_msg,
                "content": None,
                "error_kind": self._classify_error(e),
            }

    async def _create_and_parse(self, request_params: Dict[str, Any]) -> Dict[str, Any]:
        """发送请求、解析回复并记录token使用量，chat_completion 和 chat_messages 共用"""
        self.logger.debug(f"发送LLM请求: {request_params}")

        # 异步调用
        response = await self.client.chat.completions.create(**request_params)

        # 解析响应
        result = {
            "success": True,
            "content": response.choices[0].message.content,
            "model": response.model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "finish_reason": response.choices[0].finish_reason,
        }

        # 处理工具调用
        if response.choices[0].message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
                }
                for tool_call in response.choices[0].message.tool_calls
            ]

        # 记录token使用量
        self.token_manager.record_usage(
            model_name=response.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
        )

        return result

    def _classify_error(self, error: Exception) -> str:
        """把模型调用异常归类，决定重试策略

        Returns:
            transient（可重试）、fatal（重试没用）、invalid（请求内容被拒）或 unknown
        """
        if isinstance(error, (APITimeoutError, APIConnectionError, RateLimitError)):
            return "transient"
        if isinstance(error, APIStatusError) and error.status_code is not None and error.status_code >= 500:
            return "transient"
        if isinstance(error, (AuthenticationError, PermissionDeniedError)):
            return "fatal"
        if isinstance(error, BadRequestError):
            error_text = str(error)
            if any(keyword in error_text for keyword in ("balance", "quota", "insufficient", "余额", "欠费")):
                return "fatal"
            return "invalid"
        return "unknown"

    async def simple_chat(self, prompt: str, system_message: Optional[str] = None) -> str:
        """简化的聊天接口，只返回文本内容

        Args:
            prompt: 用户输入的提示
            system_message: 系统消息

        Returns:
            响应文本内容，失败时返回错误信息
        """
        result = await self.chat_completion(prompt, system_message=system_message)

        if result["success"]:
            return result["content"]
        else:
            return f"错误: {result['error']}"

    async def call_tool(
        self, prompt: str, tools: List[Dict[str, Any]], system_message: Optional[str] = None
    ) -> Dict[str, Any]:
        """调用工具的函数

        Args:
            prompt: 用户输入的提示
            tools: 工具列表
            system_message: 系统消息

        Returns:
            包含工具调用结果的字典
        """
        response = await self.chat_completion(prompt=prompt, tools=tools, system_message=system_message)

        if not response.get("success"):
            self.logger.error(f"[MaiAgent] LLM调用失败: {response.get('error')}")
            return None

        tool_calls = response.get("tool_calls", [])

        return tool_calls

    async def vision_completion(
        self,
        prompt: str,
        images: Union[str, bytes, List[Union[str, bytes]]],
        system_message: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """异步识图(多模态)调用，支持 URL/本地文件/字节 多图"""
        try:
            messages: List[Dict[str, Any]] = []
            if system_message:
                messages.append({"role": "system", "content": system_message})
            user_content = self._build_vision_user_content(prompt, images)
            messages.append({"role": "user", "content": user_content})

            request_params: Dict[str, Any] = {
                "model": self.model_config.model_name,
                "messages": messages,
                "temperature": temperature or self.model_config.temperature,
            }
            if max_tokens:
                request_params["max_tokens"] = max_tokens
            elif self.model_config.max_tokens:
                request_params["max_tokens"] = self.model_config.max_tokens

            # self.logger.debug(f"发送VLM请求: {request_params}")
            response = await self.client.chat.completions.create(**request_params)

            result = {
                "success": True,
                "content": response.choices[0].message.content,
                "model": response.model,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
                "finish_reason": response.choices[0].finish_reason,
                # 添加推理链内容解析
                "reasoning_content": getattr(response.choices[0].message, "reasoning_content", None),
            }
            return result
        except Exception as e:
            error_msg = f"VLM请求失败: {str(e)}"
            self.logger.error(error_msg)
            return {"success": False, "error": error_msg, "content": None}

    async def simple_vision(
        self, prompt: str, images: Union[str, bytes, List[Union[str, bytes]]], system_message: Optional[str] = None
    ) -> str:
        """简化的识图接口，只返回文本内容"""
        result = await self.vision_completion(prompt=prompt, images=images, system_message=system_message)
        if result["success"]:
            return result["content"]
        else:
            return f"错误: {result['error']}"

    def get_config_info(self) -> Dict[str, Any]:
        """获取配置信息

        Returns:
            配置信息字典
        """
        return {
            "model": self.model_config.model_name,
            "base_url": self.model_config.base_url,
            "temperature": self.model_config.temperature,
            "max_tokens": self.model_config.max_tokens,
            "api_key_set": bool(self.model_config.api_key),
        }

    def get_token_usage_summary(self, model_name: Optional[str] = None) -> str:
        """获取token使用量摘要

        Args:
            model_name: 模型名称，如果为None则显示所有模型

        Returns:
            token使用量摘要字符串
        """
        if model_name:
            return self.token_manager.format_usage_summary(model_name)
        else:
            all_usage = self.token_manager.get_all_models_usage()
            if not all_usage:
                return "暂无任何模型的使用记录"

            summaries = []
            for model in all_usage:
                summaries.append(self.token_manager.format_usage_summary(model))

            return "\n\n".join(summaries)

    def get_token_usage_data(self, model_name: Optional[str] = None) -> Dict[str, Any]:
        """获取token使用量原始数据

        Args:
            model_name: 模型名称，如果为None则返回所有模型数据

        Returns:
            token使用量数据字典
        """
        if model_name:
            return self.token_manager.get_usage_summary(model_name)
        else:
            return self.token_manager.get_all_models_usage()

    def get_total_cost_summary(self) -> str:
        """获取所有模型的总费用摘要

        Returns:
            总费用摘要字符串
        """
        return self.token_manager.format_total_cost_summary()

    def get_total_cost_data(self) -> Dict[str, Any]:
        """获取所有模型的总费用数据

        Returns:
            总费用数据字典
        """
        return self.token_manager.get_total_cost_summary()
