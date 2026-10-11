"""Web 渠道：WebChannel。

基于 FastAPI + uvicorn 的 WebSocket 聊天界面，配套 channels/web_ui/index.html。

与飞书/QQ 渠道的关键区别：
- 没有第三方 SDK 事件回调，WebSocket 连接由我们自己管理
- 每个 WebSocket 连接分配一个 client_id（uuid4），同时作为 sender_id 和 chat_id
- send() 靠 chat_id 找回对应的 WebSocket 连接并发送文本

启动方式：
- 不能用 uvicorn.run(app)（同步阻塞，卡住事件循环）
- 必须用 uvicorn.Server(config) + await server.serve()，和主事件循环共存
"""

import json
import logging
import socket
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import HTMLResponse

from bus.queue import InboundMessage, OutboundMessage
from channels.base import Channel

# index.html 放在 channels/web_ui/ 下
INDEX_HTML_PATH = Path(__file__).parent / "web_ui" / "index.html"


class WebChannel(Channel):
    """Web 渠道：WebSocket 聊天界面。"""

    def __init__(self, bus, host: str = "0.0.0.0", port: int = 8080) -> None:
        super().__init__(name="web", bus=bus)
        self.host = host
        self.port = port
        # client_id -> WebSocket 连接
        self._connections: dict[str, WebSocket] = {}

    def _check_port_available(self) -> None:
        """预检测端口是否可绑定；被占用时抛清晰错误（不依赖 uvicorn 报错）。

        典型场景：已有一个 NanoClaw 实例在跑（单实例锁文件若被误删
        时无法兜底），uvicorn 启动中途 sys.exit 会抛出 SystemExit，
        这里提前发现并以普通 RuntimeError 告知调用方。
        """
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((self.host, self.port))
        except OSError as e:
            raise RuntimeError(
                f"Web 端口 {self.host}:{self.port} 无法绑定（{e}）。"
                f"通常是已有另一个 NanoClaw 实例在运行，请先停止它。"
            ) from e
        finally:
            probe.close()

    async def start(self) -> None:
        """启动 Web 服务（FastAPI + uvicorn）。"""
        self._check_port_available()
        app = FastAPI()

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return INDEX_HTML_PATH.read_text(encoding="utf-8")

        # 浏览器自动请求网站图标；没有图标时返回 204，避免日志里刷 404
        @app.get("/favicon.ico")
        async def favicon():
            return Response(status_code=204)

        # 非本应用请求：Vite 浏览器扩展 / 开发代理会注入 /@vite/client，
        # 我们的页面并不使用 Vite，返回 204 静默掉，同样避免 404 噪音
        @app.get("/@vite/client")
        async def vite_client():
            return Response(
                status_code=204,
                media_type="application/javascript",
                content=b"",
            )

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            client_id = str(uuid4())
            self._connections[client_id] = websocket
            try:
                while True:
                    data = await websocket.receive_text()
                    # chat_id 就是 client_id，send() 靠它找回连接
                    inbound = InboundMessage(
                        channel="web",
                        sender_id=client_id,
                        chat_id=client_id,
                        content=data,
                    )
                    await self.bus.publish_inbound(inbound)
            except WebSocketDisconnect:
                pass
            finally:
                self._connections.pop(client_id, None)

        # 用 uvicorn.Server 异步启动，不阻塞事件循环
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="info"
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def send_stream_event(self, chat_id: str, event: dict) -> None:
        """向 WebSocket 推送流式事件（增量文本 / 状态 / 工具调用）。"""
        ws = self._connections.get(chat_id)
        if ws is not None:
            await ws.send_text("[EVENT]" + json.dumps(event, ensure_ascii=False))

    async def send(self, message: OutboundMessage) -> None:
        """向指定 WebSocket 连接发送最终完整回复。"""
        ws = self._connections.get(message.chat_id)
        if ws is not None:
            if message.extra.get("streamed"):
                await ws.send_text("[DONE]")
            else:
                await ws.send_text(message.content)
        else:
            logging.warning(
                f"[web] 连接 {message.chat_id} 不存在（用户已断开），跳过发送"
            )

    async def stop(self) -> None:
        """清理连接。"""
        self._connections.clear()
