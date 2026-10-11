"""CLI 渠道：CLIChannel。

命令行是最基本的接入方式：
- 从 stdin 读用户输入，包成 InboundMessage 发布到总线；
- Agent 运行中的流式事件（思考中 / 调用工具 / 文本增量）实时渲染；
- 收到最终 OutboundMessage 时：若本轮已流式输出正文，只换行收尾，
  不再重复打印全文。

为了让"输入 -> 等待回复 -> 再输入"的节奏串行化，用一个 asyncio.Event
做同步：发布入站消息后 wait()，send() 被调用时 set() 唤醒下一轮 input。
"""

import asyncio
import json

from bus.queue import InboundMessage, OutboundMessage
from channels.base import Channel

# 工具调用参数在终端最多展示这么长，避免刷屏
_ARGS_PREVIEW_LEN = 60


class CLIChannel(Channel):
    """命令行渠道（支持流式渲染）。"""

    def __init__(self, bus) -> None:
        super().__init__(name="cli", bus=bus)
        # 终端保留完整思考过程与工具调用提示（其他渠道只收结果）
        self.show_process = True
        # 同步"输入 -> 等回复 -> 再输入"节奏
        self._response_event = asyncio.Event()
        # 以下两个属性由 main.py 在创建后注入，CLIChannel 自身不依赖 Agent
        self.tools = None  # ToolRegistry，用于 /tools 展示描述
        self._clear_callback: callable | None = None
        # 流式渲染状态：当前是否停在"状态提示行"上（可被 \r 覆盖）
        self._status_open = False
        # 本轮是否已经开始打印正文（决定何时输出 "🦞 NanoClaw> " 前缀）
        self._answer_open = False

    async def start(self) -> None:
        """循环读取 stdin，发布入站消息并等待回复。"""
        while True:
            # input() 阻塞主线程，用 to_thread 包起来避免卡死事件循环
            try:
                user_input = await asyncio.to_thread(input, "\n你> ")
            except (EOFError, KeyboardInterrupt):
                print("\n[NanoClaw] 再见！")
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input == "/exit":
                print("[NanoClaw] 再见！")
                break
            if user_input == "/clear":
                if self._clear_callback is not None:
                    self._clear_callback()
                print("[NanoClaw] 会话历史已清空。")
                continue
            if user_input == "/tools":
                self._print_tools()
                continue

            # 正常输入：发布入站消息，然后等 Agent 回复
            self._status_open = False
            self._answer_open = False
            self._response_event.clear()
            await self.bus.publish_inbound(
                InboundMessage(
                    channel="cli",
                    sender_id="local",
                    chat_id="direct",
                    content=user_input,
                )
            )
            await self._response_event.wait()

    def _print_tools(self) -> None:
        """/tools 命令：带编号、描述展示，MCP 工具单独标注。"""
        if self.tools is None:
            print("（工具表未注入）")
            return
        names = self.tools.list_tools()
        print(f"已注册工具（{len(names)} 个）：")
        for i, name in enumerate(names, 1):
            tool = self.tools._tools.get(name)
            desc = getattr(tool, "description", "") or ""
            # 取描述第一行，避免多行 docstring 刷屏
            desc = desc.split("\n")[0].strip()
            tag = "  ← MCP 工具！" if "__" in name else ""
            print(f"  {i}. {name} — {desc}{tag}")

    def _close_status(self) -> None:
        """结束状态提示行（换行），后续输出另起一行。"""
        if self._status_open:
            print()
            self._status_open = False

    async def send_stream_event(self, chat_id: str, event: dict) -> None:
        """渲染流式事件：状态提示可覆盖，正文增量逐字输出。"""
        etype = event.get("type")
        if etype == "status":
            # 正文已开始后不再插状态行（避免打断流式文本）
            if self._answer_open:
                return
            text = event.get("text", "")
            print(f"\r\033[K  [... {text} ...]", end="", flush=True)
            self._status_open = True
        elif etype == "tool_call":
            self._close_status()
            args = json.dumps(
                event.get("arguments", {}), ensure_ascii=False
            )[:_ARGS_PREVIEW_LEN]
            print(f"  [调用工具] {event.get('name')}({args})")
            # 工具调用后的新一轮正文重新打印 "NanoClaw> " 前缀，
            # 让每轮回复（调工具前 / 后）边界清晰
            self._answer_open = False
        elif etype == "delta":
            text = event.get("text", "")
            if not self._answer_open:
                # 前缀未打印前，丢弃模型吐出的前导空白增量，
                # 避免 "NanoClaw> " 后面紧跟一串空行
                text = text.lstrip()
            if not text:
                return
            self._close_status()
            if not self._answer_open:
                print("  NanoClaw> ", end="", flush=True)
                self._answer_open = True
            print(text, end="", flush=True)

    async def send(self, message: OutboundMessage) -> None:
        """收尾：已流式输出则只换行，否则一次性打印全文。"""
        if message.extra.get("streamed") and self._answer_open:
            print()  # 流式正文已打印，补换行收尾
        else:
            self._close_status()
            print(f"  NanoClaw> {message.content}")
        self._status_open = False
        self._answer_open = False
        self._response_event.set()
