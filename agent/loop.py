"""Agent 主循环：一条用户消息进来，自动完成"思考 -> 调工具 -> 再思考"的循环。

核心是有限次迭代 + 两道防线：
- 次数防线：max_iterations 上限，防止模型无限调工具烧 token；
- 重复防线（熔断机制）：滑动窗口统计"工具名:参数"签名，同一签名
  重复过多先警告（拦截本次执行）、再熔断（终止整个任务），
  防止模型陷在同一个错误里反复重试。

对话历史跨轮次保存在 _session_history：多轮工具往返的中间消息
（assistant.tool_calls + tool 结果）也一并保存，重放时 API 才能
把结果和调用配对。system prompt 不入历史——它每轮由
ContextBuilder 重新构建（时间、记忆都会变）。
"""

import json
from collections.abc import Awaitable, Callable

from providers.base import LLMProvider
from agent.tools.registry import ToolRegistry
from agent.context import ContextBuilder
from agent.memory import MemoryConsolidator
from session.manager import SessionManager

# 流式事件回调：Agent 运行中向渠道实时推送状态/文本增量。
# 事件类型：
#   {"type": "status", "text": ...}     阶段提示（思考中 / 工具结果摘要）
#   {"type": "delta",  "text": ...}     模型输出的文本增量
#   {"type": "tool_call", "name": ..., "arguments": ...}  即将执行某工具
EventCallback = Callable[[dict], Awaitable[None]]

# 重复调用的两级处置：>=10 次警告（拦截本次），>=20 次熔断（终止任务）
WARN_THRESHOLD = 10
BREAK_THRESHOLD = 20
# 滑动窗口大小：只看最近 30 次调用，旧签名自动淡出
WINDOW_SIZE = 30

# 触发上下文压缩的 Token 预算
MEMORY_TOKEN_BUDGET = 6000

# 警告时回给模型的 tool 消息内容（让模型知道这次调用没执行）
SYSTEM_ERROR = (
    "系统提示：检测到完全相同的重复调用，本次调用已被拦截、未执行。"
    "请更换参数、使用其他工具，或换一种思路完成任务。"
)


class AgentLoop:
    """驱动"LLM <-> 工具"循环的引擎。

    一轮 run() 的生命周期：
    用户消息 -> build_messages -> [LLM 调用 <-> 工具执行] 循环 -> 最终文本。

    每次迭代：
    - 模型要调工具：先过防爆检测，通过的执行工具；assistant 消息
      （含 tool_calls）与每个 tool 结果消息都追加进 messages，continue
      进入下一轮，让模型看到结果后继续思考；
    - 模型直接回答：本轮新增消息并入 _session_history，返回文本。

    格式红线：assistant 消息里 tool_calls 元素只允许 id / type /
    function 三个字段，reasoning_content 必须放在消息顶层——
    硅基流动等 API 重放历史消息时，tool_calls 里出现未知字段会报
    error code 20015。
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ContextBuilder,
        session_manager: SessionManager,
        model: str | None = None,
        max_iterations: int = 32,
        session_key: str = "cli:direct",
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.context = context
        self.model = model
        self.max_iterations = max_iterations
        # 滑动窗口：最近的工具调用签名（"工具名:参数JSON"）
        self._tool_call_history: list[str] = []
        # 持久化层 + 会话标识
        self.session_manager = session_manager
        self.session_key = session_key
        # 跨轮次对话历史：启动时从 JSONL 恢复（不含 system，system 每轮重建）
        self._session_history: list[dict] = session_manager.get_history(session_key)
        # 长对话自动压缩：内部先建默认实例，main.py 可用
        # agent.consolidator = ... 覆盖为外部装配的压缩器
        self.consolidator = MemoryConsolidator(
            provider=provider,
            workspace=context.workspace,
            token_budget=MEMORY_TOKEN_BUDGET,
        )
        # 每轮 run 的状态：本轮新增消息对象 id 集合 / 是否已压缩
        self._turn_ids: set[int] = set()
        self._consolidated = False

    async def run(
        self,
        user_message: str,
        on_event: EventCallback | None = None,
    ) -> str:
        """处理一条用户消息，返回模型的最终文本回复。

        on_event 不为 None 时，运行全程推送流式事件（思考 / 工具调用 /
        文本增量），供渠道实时渲染；为 None 时行为与原来完全一致
        （子 Agent、测试等场景不用传）。
        """
        # 重置本轮状态
        self._turn_ids = set()
        self._consolidated = False

        messages = self.context.build_messages(
            history=self._session_history, current_message=user_message
        )
        # 本轮新增消息的起点：system(1) + 旧历史之后（含本次用户消息）
        baseline = 1 + len(self._session_history)
        # 用户消息随本轮开始立即持久化（取 messages 中的同一对象，
        # 便于压缩时按对象身份识别本轮消息）
        user_msg = messages[-1]
        self._turn_ids.add(id(user_msg))
        self._persist(user_msg)

        # 文本增量转发：Provider 的 on_delta -> 渠道事件的桥。
        # 模型（尤其工具调用后第二轮）常以一串 "\n\n" 开头输出，
        # 直接转发会让各渠道回复前出现大段空白，因此每次 LLM 调用
        # 开始前重置闸门，正文首个非空白字符之前的空白一律丢弃。
        delta_gate = {"started": False}

        async def emit_delta(text: str) -> None:
            if on_event is None or not text:
                return
            if not delta_gate["started"]:
                text = text.lstrip()
                if text:
                    delta_gate["started"] = True
                else:
                    return
            await on_event({"type": "delta", "text": text})

        for _ in range(self.max_iterations):
            # 每轮 LLM 调用是一条独立的 assistant 消息，各自去前导空白
            delta_gate["started"] = False
            # 调用 LLM 前自动压缩超预算的上下文
            messages, baseline = await self._maybe_consolidate(messages, baseline)

            if on_event is not None:
                await on_event({"type": "status", "text": "思考中"})
                response = await self.provider.chat_stream(
                    messages,
                    self.tools.get_definitions(),
                    self.model,
                    on_delta=emit_delta,
                )
            else:
                response = await self.provider.chat(
                    messages, self.tools.get_definitions(), self.model
                )

            # Provider 层出错：错误信息直接作为本轮回复，不入历史
            if response.finish_reason == "error":
                return response.content or "未知错误"

            if response.has_tool_calls:
                # assistant 消息：tool_calls 元素严格三字段，reasoning 放顶层
                assistant_msg: dict = {"role": "assistant", "content": response.content}
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
                if response.tool_calls[0].reasoning_content:
                    assistant_msg["reasoning_content"] = response.tool_calls[0].reasoning_content
                self._append(messages, assistant_msg)

                breaker: str | None = None
                for tc in response.tool_calls:
                    args_json = json.dumps(tc.arguments, ensure_ascii=False)
                    verdict = self._check_tool_loop(tc.name, args_json)
                    if breaker is not None:
                        # 已熔断：后续调用不再执行，只补占位结果
                        self._append(
                            messages,
                            {"role": "tool", "tool_call_id": tc.id, "content": SYSTEM_ERROR},
                        )
                    elif verdict is None:
                        # 防爆放行：真正执行工具，结果以 tool 消息回填
                        if on_event is not None:
                            await on_event({
                                "type": "tool_call",
                                "name": tc.name,
                                "arguments": tc.arguments,
                            })
                        result = await self.tools.execute(tc.name, tc.arguments)
                        if on_event is not None:
                            preview = result.replace("\n", " ")[:80]
                            await on_event({
                                "type": "status",
                                "text": f"工具 {tc.name} 完成：{preview}",
                            })
                        self._append(
                            messages,
                            {"role": "tool", "tool_call_id": tc.id, "content": result},
                        )
                    elif verdict.startswith("熔断"):
                        # 记录熔断，本次及后续调用都以占位结果回填
                        breaker = verdict
                        self._append(
                            messages,
                            {"role": "tool", "tool_call_id": tc.id, "content": SYSTEM_ERROR},
                        )
                    else:
                        # 警告：拦截本次执行，但 tool 结果消息必须补上，
                        # 否则下一轮请求里 tool_call 没有对应结果会报错
                        self._append(
                            messages,
                            {"role": "tool", "tool_call_id": tc.id, "content": SYSTEM_ERROR},
                        )

                if breaker is not None:
                    # 已持久化的消息同步进内存历史，保证磁盘与内存一致
                    self._save_to_history(messages[baseline:])
                    return f"任务已熔断终止：{breaker}"
                continue

            # 模型没有调工具 -> 最终回答（先入 messages 再存历史，
            # 否则历史里缺最后一轮 assistant 回复，下轮模型看不到）。
            # strip 掉首尾空白：模型常以 "\n\n" 起头，飞书等渠道会
            # 原样显示成一大段缩进空格。
            final = (response.content or "").strip() or "（模型未返回内容）"
            self._append(messages, {"role": "assistant", "content": final})
            self._save_to_history(messages[baseline:])
            return final
        else:
            # 持久化过的中间消息同步进内存历史，避免重启后重复/丢失
            self._save_to_history(messages[baseline:])
            return f"已达最大循环次数（{self.max_iterations} 轮），任务未完成，已强制停止。"

    def _check_tool_loop(self, tool_name: str, tool_args_json: str) -> str | None:
        """防爆检测：同一签名重复过多则警告/熔断。

        签名 = "工具名:参数JSON"，完全相同的调用意味着模型大概率
        卡死在同一个错误上（如反复读不存在的文件）。
        - 出现 >= 20 次：返回熔断消息（终止任务）；
        - 出现 >= 10 次：返回警告消息（拦截本次执行）；
        - 否则返回 None（放行）。

        注意：警告的调用也会记入窗口。若不记，计数永远停在 10，
        熔断（>=20）永远无法触发，防线二形同虚设。
        """
        signature = f"{tool_name}:{tool_args_json}"
        count = self._tool_call_history.count(signature)
        if count >= BREAK_THRESHOLD:
            return (
                f"熔断：工具 '{tool_name}' 相同参数已重复调用 {count} 次，"
                f"疑似陷入死循环，本轮任务强制终止。"
            )
        self._tool_call_history.append(signature)
        if len(self._tool_call_history) > WINDOW_SIZE:
            self._tool_call_history.pop(0)
        if count >= WARN_THRESHOLD:
            return f"警告：工具 '{tool_name}' 相同参数已重复调用 {count} 次，本次调用被拒绝。"
        return None

    def _save_to_history(self, messages_snapshot: list[dict]) -> None:
        """把本轮新增消息（用户消息 + 工具往返 + 最终回答）并入会话历史。"""
        self._session_history.extend(messages_snapshot)

    def _append(self, messages: list[dict], message: dict) -> None:
        """消息加入本轮 messages 的同时持久化，保证内存与文件不分裂。"""
        messages.append(message)
        self._turn_ids.add(id(message))
        self._persist(message)

    async def _maybe_consolidate(
        self, messages: list[dict], baseline: int
    ) -> tuple[list[dict], int]:
        """调用 LLM 前检查并压缩上下文，同步内存历史与会话文件。

        未触发压缩或本轮已压缩过时原样返回。触发时：
        - 按对象身份把压缩结果拆成"旧历史（含摘要）"与"本轮消息"，
          更新 _session_history 并重算 baseline；
        - 重写 JSONL（clear 后按新顺序逐条写入），避免旧消息和摘要
          并存导致重启恢复时上下文重复。
        """
        if self._consolidated:
            return messages, baseline

        consolidated = await self.consolidator.maybe_consolidate(messages)
        if consolidated is messages:
            return messages, baseline

        self._consolidated = True
        new_history = [
            m for m in consolidated[1:] if id(m) not in self._turn_ids
        ]
        self._session_history = new_history

        self.session_manager.clear(self.session_key)
        for m in consolidated[1:]:
            self._persist(m)

        baseline = 1 + len(new_history)
        return consolidated, baseline

    def _persist(self, message: dict) -> None:
        """持久化单条消息；磁盘失败只警告，不打断主循环。"""
        try:
            self.session_manager.save_message(self.session_key, message)
        except OSError as e:
            print(f"警告：会话消息持久化失败: {e}")

    def clear_history(self) -> None:
        """清空会话历史、工具调用窗口与持久化文件，开始全新会话。"""
        self._session_history.clear()
        self._tool_call_history.clear()
        self.session_manager.clear(self.session_key)
