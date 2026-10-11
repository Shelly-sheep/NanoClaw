"""OpenAI 兼容 Provider（硅基流动 / OpenAI 官方 / 各类兼容服务通用）。

所有"OpenAI 风格"API 的请求结构一致，差异只在 base_url 和模型名，
换配置即可换厂商。本实现负责三件事：
- 组请求：动态构建 kwargs，tools 为空时绝不携带 tools/tool_choice；
- 发请求：AsyncOpenAI 异步客户端，异常统一转错误响应不炸主循环；
- 转格式：SDK 响应 -> 我们自己的 LLMResponse / ToolCallRequest。
"""

import json

from openai import AsyncOpenAI

from providers.base import LLMProvider, LLMResponse, ToolCallRequest


class OpenAICompatProvider(LLMProvider):
    """基于 openai SDK（AsyncOpenAI）的 LLM Provider 实现。

    - 构造时传入 api_key / base_url / model，base_url 指向硅基流动
      （https://api.siliconflow.cn/v1）即对接硅基流动；
    - chat() 按需构建请求参数：tools 非空才传 tools + tool_choice="auto"。
      若 tools 为 None（如生成摘要、润色文本等不需要工具的场景）仍传
      tool_choice，硅基流动会报
      "The tools parameter must be specified when tool_choice is utilized"，
      这是兼容 API 的通病，必须用 dict + **kwargs 动态组装来规避；
    - 思考型模型的推理内容（reasoning_content）可能挂在单个 tool_call
      上，也可能挂在 message 顶层，两处都做容错读取（getattr），
      tool_call 级别优先；
    - API 任何异常（网络 / 限流 / 鉴权）都转成
      LLMResponse(content=错误信息, finish_reason="error") 返回，
      由主循环决定如何呈现，绝不让异常打断 Agent 循环。
    """

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict]:
        """归一化 messages：全列表只保留一条 system 且必须在最开头。

        硅基流动要求 "System message must be at the beginning"——
        历史中混入第二条 system（长对话压缩产生的 "[历史摘要]" 是
        典型来源）会报 20015。这里把所有 system 消息的文本合并进
        开头唯一的 system 消息，其余消息顺序不动。返回新列表，
        不修改调用方的原始数据（主循环还要持久化 / 继续迭代）。
        """
        system_texts = [
            m.get("content", "")
            for m in messages
            if m.get("role") == "system" and m.get("content")
        ]
        non_system = [m for m in messages if m.get("role") != "system"]
        if not system_texts:
            return list(messages)
        merged_system = {"role": "system", "content": "\n\n".join(system_texts)}
        return [merged_system, *non_system]

    def _build_kwargs(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str | None,
    ) -> dict:
        """动态组装请求参数：tools 为空时不带 tools / tool_choice 字段。"""
        kwargs: dict = {
            "model": model or self.model,
            "messages": self._normalize_messages(messages),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        try:
            resp = await self.client.chat.completions.create(
                **self._build_kwargs(messages, tools, model)
            )

            message = resp.choices[0].message
            # message 顶层的思考内容（部分模型放在这里而非 tool_call 上）
            msg_reasoning = getattr(message, "reasoning_content", None)

            tool_calls = []
            for tc in message.tool_calls or []:
                tool_calls.append(
                    ToolCallRequest(
                        id=tc.id,
                        name=tc.function.name,
                        # SDK 给的是 JSON 字符串，这里解析成 dict，
                        # 主循环可直接 registry.execute(name, arguments)
                        arguments=json.loads(tc.function.arguments or "{}"),
                        # tool_call 级别优先，回落到 message 级别
                        reasoning_content=getattr(tc, "reasoning_content", None) or msg_reasoning,
                    )
                )

            return LLMResponse(
                content=message.content,
                tool_calls=tool_calls,
                finish_reason=resp.choices[0].finish_reason or "stop",
                usage=resp.usage.model_dump() if resp.usage else {},
            )
        except Exception as e:
            return LLMResponse(content=f"LLM API 调用失败：{e}", finish_reason="error")

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        on_delta=None,
    ) -> LLMResponse:
        """真流式实现：SSE 逐块接收，content 增量实时回调 on_delta。

        流式响应里 tool_calls 是分片到达的（首个分片带 id/name，后续
        只有 arguments 片段），按 index 归并拼装；reasoning_content
        同理累积（只透传回调展示，不进 content）。最后组装的
        LLMResponse 与 chat() 完全同构，上层无感。
        """
        try:
            kwargs = self._build_kwargs(messages, tools, model)
            kwargs["stream"] = True
            # 让硅基流动在流式末尾也返回 usage（部分兼容 API 不支持就忽略）
            kwargs["stream_options"] = {"include_usage": True}
            stream = await self.client.chat.completions.create(**kwargs)

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            # index -> 累积中的工具调用分片
            tc_acc: dict[int, dict] = {}
            finish_reason = "stop"
            usage: dict = {}

            async for chunk in stream:
                # 最后一个 chunk 可能只有 usage 没有 choices
                if not chunk.choices:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage.model_dump()
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                # 文本增量：实时回调给上层（渠道流式渲染）
                if delta.content:
                    content_parts.append(delta.content)
                    if on_delta is not None:
                        result = on_delta(delta.content)
                        if hasattr(result, "__await__"):
                            await result

                # 思考型模型的推理流（部分 API 放在 delta.reasoning_content）
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    reasoning_parts.append(rc)

                # 工具调用分片累积
                for tc_delta in delta.tool_calls or []:
                    slot = tc_acc.setdefault(
                        tc_delta.index, {"id": "", "name": "", "args": ""}
                    )
                    if tc_delta.id:
                        slot["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            slot["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            slot["args"] += tc_delta.function.arguments

            tool_calls = [
                ToolCallRequest(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=json.loads(slot["args"] or "{}"),
                    reasoning_content="".join(reasoning_parts) or None,
                )
                for _, slot in sorted(tc_acc.items())
            ]

            return LLMResponse(
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )
        except Exception as e:
            return LLMResponse(content=f"LLM API 调用失败：{e}", finish_reason="error")
