"""渠道抽象基类：Channel。

每个接入渠道（飞书 / QQ / Web / CLI 等）都实现这个基类：
- start()：启动渠道，持续把收到的消息发布到 MessageBus；
- send()：把 Agent 生成的 OutboundMessage 发回给用户；
- stop()：可选，关闭渠道、释放资源（如关闭 HTTP 服务器、注销
  WebSocket 连接等），默认空实现。

渠道只负责"收发消息 + 协议转换"，不直接依赖 Agent，通过 MessageBus
与主循环解耦。
"""

from abc import ABC, abstractmethod

from bus.queue import MessageBus, OutboundMessage


class Channel(ABC):
    """所有接入渠道的抽象基类。"""

    def __init__(self, name: str, bus: MessageBus) -> None:
        self.name = name
        self.bus = bus
        # 是否向用户展示"思考中 / 调用工具"等过程事件。
        # 只有终端（CLI）保留完整过程；Web / 飞书 / QQ 只收正文结果。
        self.show_process = False

    @abstractmethod
    async def start(self) -> None:
        """启动渠道：监听外部输入并发布到 MessageBus。"""

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """把一条出站消息发送回用户。"""

    async def send_stream_event(self, chat_id: str, event: dict) -> None:
        """接收 Agent 运行中的流式事件（思考 / 工具调用 / 文本增量）。

        默认忽略：不支持流式渲染的渠道（飞书 / QQ 等 IM 平台没有
        逐字更新能力）只收最终完整回复，行为不变。
        """

    async def stop(self) -> None:
        """停止渠道，释放资源。默认空实现，按需覆盖。"""
