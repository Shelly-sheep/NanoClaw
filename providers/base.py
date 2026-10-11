"""LLM Provider 基础设施：统一的数据结构和抽象基类。

Provider 层的职责是把各家 LLM API（硅基流动 / OpenAI 官方 / 其他
兼容服务）的协议差异封装起来，对上只暴露一个 async chat() 方法。
本文件定义三样东西：
- ToolCallRequest：模型发起的一次"函数调用"请求；
- LLMResponse：一次对话回复的归一化结果；
- LLMProvider：抽象基类，主循环（agent/loop.py）只认这个接口。

流式输出：chat_stream() 是 chat() 的流式变体，通过 on_delta 回调
逐 token 推送文本。默认实现退化为一次性返回（子类不实现也能用），
子类实现了真流式则逐块回调。
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

# token 增量回调：每收到一小段新文本就调用一次（可以是 async）
DeltaCallback = Callable[[str], Awaitable[None] | None]


@dataclass
class ToolCallRequest:
    """模型请求调用某个工具的完整信息。

    对应 OpenAI 响应里 assistant 消息的 tool_calls 项：
    - id: 本次调用 ID；回传工具结果时必须作为 tool_call_id
      原样带回，API 靠它把结果和请求配对；
    - name: 要调用的工具名，对应 Tool.name；
    - arguments: 工具入参。API 原始返回是 JSON 字符串，由
      Provider 层解析成 dict 后再填入，主循环直接
      registry.execute(name, arguments) 即可；
    - reasoning_content: 思考型模型（Qwen3、DeepSeek-R1 等）
      返回 tool_calls 时附带的推理过程。OpenAI 标准协议没有此
      字段，但多轮对话时不少兼容 API 要求随 assistant 消息原样
      回传，否则下一轮请求报 400，所以必须单独存下来。
    """

    id: str
    name: str
    arguments: dict
    reasoning_content: str | None = None


@dataclass
class LLMResponse:
    """一次 LLM 调用的归一化结果。

    一次回复要么是文本（content），要么是函数调用（tool_calls），
    也可能两者都有（先说一句话再调工具）。主循环用 has_tool_calls
    分流：
    - True：执行工具，结果以 tool 消息追加，继续循环；
    - False：content 就是最终答案，循环结束。
    finish_reason（stop/tool_calls/length...）和 usage（token 用量）
    主要供调试和日志。
    """

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMProvider(ABC):
    """LLM Provider 抽象基类（所有 Provider 的"模具"）。

    子类（如 openai_compat.py）实现 chat()，负责三件事：
    - 把 messages / tools 组装成目标 API 的请求体；
    - 发请求，处理超时、限流等网络层问题；
    - 把响应解析回 LLMResponse（含 arguments JSON 字符串 -> dict、
      提取 reasoning_content）。

    参数约定：
    - messages: OpenAI 格式消息列表 [{"role": ..., "content": ...}]；
    - tools: OpenAI 格式工具定义列表（registry.get_definitions()
      的返回值），None 表示本轮不让模型调工具；
    - model: 模型名，None 时用 Provider 自己配置的默认模型——
      保留该参数是为了支持"轻活用小模型，难活用大模型"。
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """发送一轮对话请求，返回归一化的 LLMResponse。"""

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        """流式对话：每收到一小段新文本就回调 on_delta，最后返回完整结果。

        返回值与非流式 chat() 完全一致（content 是拼接好的全文），
        on_delta 只是"顺带通知"，调用方即使不消费也不影响结果。

        默认实现：退化为一次性调用 chat()，把完整 content 一次性
        回调给 on_delta——不支持流式的 Provider 也能跑通上层逻辑。
        """
        response = await self.chat(messages, tools=tools, model=model)
        if on_delta is not None and response.content:
            result = on_delta(response.content)
            # 兼容同步 / 异步回调
            if isinstance(result, Awaitable):
                await result
        return response
