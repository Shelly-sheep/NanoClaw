"""飞书渠道：FeishuChannel。

通过飞书开放平台的长连接（WebSocket）接收消息，通过 OpenAPI 发送回复。
飞书 SDK（lark-oapi）的两个客户端是分开的：
- lark.ws.Client：WebSocket 长连接，接收事件回调
- lark.Client：HTTP API 客户端，发消息、查用户等

注意：_on_message 回调在 SDK 的 WebSocket 子线程中执行，
不能直接 await，必须用 run_coroutine_threadsafe 投递回主事件循环。
"""

import asyncio
import json
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from bus.queue import InboundMessage, OutboundMessage
from channels.base import Channel


class FeishuChannel(Channel):
    """飞书渠道：长连接收消息 + OpenAPI 发消息。"""

    def __init__(self, bus, app_id: str, app_secret: str) -> None:
        super().__init__(name="feishu", bus=bus)
        self.app_id = app_id
        self.app_secret = app_secret
        # WebSocket 客户端（收消息）
        self._client = None
        # HTTP API 客户端（发消息）
        self._lark_api_client = None
        # 主事件循环引用，供子线程回调投递协程
        self._loop = None
        # 渠道是否已关闭：SDK 的接收线程无法强制停止，网关关停后
        # 它仍可能收到消息，回调必须识别闸门并静默丢弃，否则会对
        # 已置空的 _loop 调 run_coroutine_threadsafe 报错
        self._closed = False
        # start() 协程永久等待的闸门，stop() 时唤醒以正常结束
        self._stop_guard = None

    async def start(self) -> None:
        """启动飞书长连接，持续接收消息。"""
        # 保存主事件循环，子线程回调要用它投递协程
        self._loop = asyncio.get_running_loop()
        self._closed = False  # （重新）启动时复位关闭闸门

        # 发送消息用的 HTTP API 客户端（和 WebSocket 客户端分开）
        self._lark_api_client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        # 注册 P2 协议的消息接收回调
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )

        # WebSocket 客户端
        self._client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # client.start() 内部自建事件循环并永久阻塞（含断线重连）。
        # 不用 asyncio.to_thread：默认线程池的工作线程是非守护语义，
        # 解释器退出时 atexit 会 join 它，而它永远不会结束——
        # 一旦网关因其他渠道故障进入关停，整个进程会卡死在退出阶段。
        # 用 daemon 线程承载，进程退出时直接回收；start() 协程本体
        # 用一个可被取消的 Event 永久等待，网关关停时能正常结束。
        thread = threading.Thread(
            target=self._run_client, daemon=True, name="feishu-ws"
        )
        thread.start()
        self._stop_guard = asyncio.Event()
        await self._stop_guard.wait()

    def _run_client(self) -> None:
        """守护线程入口：运行 lark ws 客户端，异常只记日志不炸进程。"""
        try:
            self._client.start()
        except Exception as e:
            print(f"[feishu] 长连接异常：{type(e).__name__}: {e}")

    def _on_message(self, data) -> None:
        """飞书消息接收回调（在 WebSocket 子线程中执行）。"""
        # 网关已关停：SDK 线程可能还活着，静默丢弃迟到的消息，
        # 避免对 None / 已关闭的事件循环投递协程
        loop = self._loop
        if self._closed or loop is None or loop.is_closed():
            return
        # P2 协议中事件数据在 data.event 里
        event = data.event

        sender_id = event.sender.sender_id.open_id
        chat_id = event.message.chat_id
        message_type = event.message.message_type
        sender_type = event.sender.sender_type  # 不是 sender_id.type

        # 过滤 Bot 自己发的消息
        if sender_type == "app":
            return
        # 只处理文本消息
        if message_type != "text":
            return

        content = json.loads(event.message.content)
        text = content.get("text", "")

        message = InboundMessage(
            channel="feishu",
            sender_id=sender_id,
            chat_id=chat_id,
            content=text,
            raw={"message_id": event.message.message_id},
        )

        # 回调在子线程，不能直接 await，投递回主事件循环。
        # 竞态兜底：检查通过后主循环恰好关闭时，
        # run_coroutine_threadsafe 会抛 RuntimeError，忽略即可
        try:
            asyncio.run_coroutine_threadsafe(
                self.bus.publish_inbound(message),
                loop,
            )
        except RuntimeError:
            pass

    async def send(self, message: OutboundMessage) -> None:
        """通过飞书 OpenAPI 发送回复消息。"""
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(message.chat_id)
                .msg_type("text")
                .content(json.dumps({"text": message.content}))
                .build()
            )
            .build()
        )

        # 发消息是同步 HTTP 调用，放线程里
        await asyncio.to_thread(self._lark_api_client.im.v1.message.create, request)

    async def stop(self) -> None:
        """清理飞书客户端。

        先落下关闭闸门（让 SDK 线程的迟到回调失效），再清引用。
        lark ws 客户端没有公开的断开接口、接收线程也无法强制结束，
        它是守护线程，进程退出时自动回收；网关存活期间若调用 stop，
        闸门保证不会再有消息入站。
        """
        self._closed = True
        self._client = None
        self._lark_api_client = None
        self._loop = None
        if self._stop_guard is not None:
            self._stop_guard.set()
