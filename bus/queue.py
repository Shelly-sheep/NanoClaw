"""消息总线：统一收发多渠道消息。

把不同接入渠道（飞书 / QQ / Web / CLI 等）的消息抽象成统一结构，
通过两个 asyncio.Queue 解耦：
- inbound_queue：外部渠道 -> Agent 主循环
- outbound_queue：Agent 回复 -> 外部渠道分发

各渠道适配器只关心怎么把原始消息转成 InboundMessage、
怎么把 OutboundMessage 发回去，主循环只认这两个 dataclass。
"""

import asyncio
from dataclasses import dataclass, field


@dataclass
class InboundMessage:
    """入站消息：来自某个渠道的一条用户消息。

    - channel: 来源渠道名（"feishu" / "qq" / "web" / "cli" 等）
    - sender_id: 发送者标识，用于区分用户
    - chat_id: 会话标识，群聊 or 私聊
    - content: 消息正文
    - raw: 原始消息体，保留给调试 / 渠道特判用
    """

    channel: str
    sender_id: str
    chat_id: str
    content: str
    raw: dict | None = None


@dataclass
class OutboundMessage:
    """出站消息：Agent 要发往某个渠道的一条回复。

    - channel: 目标渠道
    - chat_id: 目标会话
    - content: 回复正文
    - reply_to: 引用的消息 ID（可选，实现"回复某人"时用）
    - extra: 渠道专有信息（如 QQ 的 msg_type=group/c2c、被动回复用
      的 message_id），由入站消息的 raw 透传，渠道适配器据此精确投递，
      避免"先试私聊再试群聊"导致的重复发送
    """

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    extra: dict = field(default_factory=dict)


class MessageBus:
    """基于 asyncio.Queue 的双队列消息总线。

    队列无容量上限（默认 maxsize=0），消费者用 await queue.get()
    会自动阻塞到有消息为止，天然支持"来一条处理一条"。
    """

    def __init__(self) -> None:
        self.inbound_queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound_queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """渠道适配器收到消息后调用，推入入站队列。"""
        await self.inbound_queue.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """主循环从入站队列取一条消息，无消息时自动等待。"""
        return await self.inbound_queue.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Agent 生成回复后调用，推入出站队列。"""
        await self.outbound_queue.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """渠道适配器从出站队列取一条回复，无消息时自动等待。"""
        return await self.outbound_queue.get()
