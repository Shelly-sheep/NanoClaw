"""网关：Gateway。

Gateway 是"多渠道接入"和"Agent 处理"之间的调度中枢，职责：
- 启动所有渠道；
- 消费入站消息，按 session_key 路由到对应的 Agent；
- 把 Agent 的回复分发到目标渠道。

并发模型：
- N 个入站 worker（默认 4）同时从总线取消息，四种渠道可以同时对话；
- 通道隔离靠 session_key = "渠道:发送者"，不同渠道 / 不同用户
  拿到的是不同 Agent 实例，历史互不串；
- 同一个会话内用 per-session 锁串行化：同一用户连发两条消息时，
  第二条必须等第一条处理完，否则两个协程交叉写同一份历史会乱，
  不同会话之间则完全并行；
- 出站消息各自建任务并发发送，某个渠道（如飞书 HTTP）慢，
  不会拖累其他渠道的回复。

去重：
- 入站消息若带 raw["message_id"]（飞书 / QQ 都有），按
  "渠道:message_id" 在滑动窗口内去重。SDK 断线重连 / 平台
  重推同一事件时，只处理第一次，避免"一句话被答两遍"。
  CLI / Web 输入源本身不产生重复 ID，不受影响。
"""

import asyncio
from collections import deque
from typing import Callable

from bus.queue import MessageBus, InboundMessage, OutboundMessage
from channels.base import Channel

# 事件去重窗口：只记最近 1000 条入站事件 ID
SEEN_EVENT_WINDOW = 1000


class Gateway:
    """多渠道接入网关。"""

    def __init__(
        self,
        bus: MessageBus,
        channels: list[Channel],
        agent_factory: Callable[[str], object],
        concurrency: int = 4,
    ) -> None:
        self.bus = bus
        self.channels = channels
        self.agent_factory = agent_factory
        # 入站并发数：四种渠道可同时对话
        self.concurrency = max(1, concurrency)
        # 按 session_key 缓存 Agent 实例（同一渠道+同一用户复用）
        self._agents: dict[str, object] = {}
        # 按渠道名快速索引，出站分发时用
        self._channel_map: dict[str, Channel] = {c.name: c for c in channels}
        # 每会话一把锁：同会话串行、跨会话并行
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 入站事件去重（set 判重，deque 限内存）
        self._seen_events: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=SEEN_EVENT_WINDOW)
        # 持有出站发送任务的强引用，避免被 GC 提前回收
        self._send_tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        """并发启动所有渠道 + N 个入站 worker + 出站分发。"""
        tasks = [
            asyncio.create_task(self._start_channel(c)) for c in self.channels
        ] + [
            asyncio.create_task(self._inbound_worker(i))
            for i in range(self.concurrency)
        ] + [
            asyncio.create_task(self._dispatch_outbound()),
        ]
        try:
            # 等待所有任务（通常会阻塞直到某个渠道退出）
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            # 取消所有未完成的任务
            for task in tasks:
                if not task.done():
                    task.cancel()
            # 等待任务取消完成（忽略 CancelledError）
            await asyncio.gather(*tasks, return_exceptions=True)
            # 关闭所有渠道
            await self.shutdown()

    async def _start_channel(self, channel: Channel) -> None:
        """启动单个渠道；失败只记日志，不拖垮整个网关。

        渠道启动依赖外部网络（如 QQ 的 api.sgroup.qq.com），网络抖动
        时 SDK 可能直接抛异常。若不隔离，gather 会把异常传播出去，
        导致其他正常渠道（CLI / Web / 飞书）一起被关闭。
        """
        try:
            await channel.start()
        except asyncio.CancelledError:
            raise  # 正常关闭流程，必须传播
        except (Exception, SystemExit) as e:
            # SystemExit：uvicorn 端口绑定失败时调 sys.exit() 抛出，
            # 它是 BaseException 不是 Exception，必须单独列出，否则会
            # 穿透 gather 触发整个网关关停（飞书等正常渠道也被拖死）
            print(
                f"[gateway] 渠道 '{channel.name}' 启动失败："
                f"{type(e).__name__}: {e}（其他渠道不受影响）"
            )

    def _mark_event_seen(self, dedup_key: str) -> bool:
        """登记入站事件 ID；已存在返回 False（重复事件），新事件返回 True。

        事件循环单线程，worker 在 await 之前执行本方法，无需加锁。
        """
        if dedup_key in self._seen_events:
            return False
        # deque 满了自动弹出最早的 key，同步从 set 删除
        if len(self._seen_order) == SEEN_EVENT_WINDOW:
            self._seen_events.discard(self._seen_order[0])
        self._seen_order.append(dedup_key)
        self._seen_events.add(dedup_key)
        return True

    def _get_agent(self, session_key: str) -> object:
        """按 session_key 获取或创建 Agent。"""
        agent = self._agents.get(session_key)
        if agent is None:
            agent = self.agent_factory(session_key)
            self._agents[session_key] = agent
        return agent

    async def _inbound_worker(self, worker_id: int) -> None:
        """入站消费循环：取消息 -> 去重 -> 找/建 Agent -> 加会话锁跑 -> 发回复。

        多个 worker 并发运行；同一 session_key 的消息靠锁排队，
        不同 session_key 的消息在不同 worker 上真正并行。
        """
        while True:
            msg: InboundMessage = await self.bus.consume_inbound()

            # 统一事件去重：飞书 / QQ 的 raw 里都带 message_id
            event_id = (msg.raw or {}).get("message_id")
            if event_id and not self._mark_event_seen(f"{msg.channel}:{event_id}"):
                print(f"[gateway] 忽略重复事件：{msg.channel}:{event_id}")
                continue

            session_key = f"{msg.channel}:{msg.sender_id}"
            agent = self._get_agent(session_key)
            lock = self._session_locks.setdefault(session_key, asyncio.Lock())
            channel = self._channel_map.get(msg.channel)

            # 流式事件桥：Agent 的 on_event -> 渠道的 send_stream_event。
            # 用标记记录本轮是否真的流式输出了正文（delta 事件），
            # 已流式渲染的渠道在收到最终消息时不再重复打印全文。
            streamed = {"sent": False}

            async def on_event(event: dict) -> None:
                if channel is None:
                    return
                etype = event.get("type")
                # 过程事件（思考状态 / 工具调用）只发给终端渠道；
                # Web / 飞书 / QQ 只接收正文增量与最终结果
                if etype in ("status", "tool_call") and not channel.show_process:
                    return
                if etype == "delta" and event.get("text"):
                    streamed["sent"] = True
                try:
                    await channel.send_stream_event(msg.chat_id, event)
                except Exception:
                    pass  # 流式渲染失败不影响主流程

            # 同一会话串行执行，防止并发消息交叉污染对话历史
            async with lock:
                try:
                    reply = await agent.run(msg.content, on_event=on_event)
                except Exception as e:
                    # Agent 出错时回一条友好提示，不让 worker 挂掉
                    reply = f"处理消息时出错：{e}"

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=reply,
                    # 透传渠道专有字段（如 QQ 的 msg_type / message_id），
                    # 让回复渠道能精确投递而不是猜测私聊/群聊
                    extra={
                        **(dict(msg.raw) if msg.raw else {}),
                        "streamed": streamed["sent"],
                    },
                )
            )

    async def _dispatch_outbound(self) -> None:
        """出站分发：取回复 -> 找渠道 -> 建任务并发发送。

        发送本身并发执行：某个渠道 API 慢或失败不影响其他渠道。
        """
        while True:
            msg: OutboundMessage = await self.bus.consume_outbound()
            channel = self._channel_map.get(msg.channel)
            if channel is None:
                # 没有对应渠道，丢弃并打印，避免阻塞出站队列
                print(f"警告：未知渠道 '{msg.channel}'，出站消息被丢弃")
                continue
            task = asyncio.create_task(self._safe_send(channel, msg))
            self._send_tasks.add(task)
            task.add_done_callback(self._send_tasks.discard)

    async def _safe_send(self, channel: Channel, msg: OutboundMessage) -> None:
        """单条出站发送，异常就地捕获只记日志，不波及分发循环。"""
        try:
            await channel.send(msg)
        except Exception as e:
            print(f"[gateway] 渠道 '{channel.name}' 发送失败：{type(e).__name__}: {e}")

    async def shutdown(self) -> None:
        """关闭所有渠道并清空 Agent 缓存。"""
        # 等待在途的发送任务收尾（给 3 秒，避免无限等待）
        if self._send_tasks:
            await asyncio.wait(self._send_tasks, timeout=3)
        for c in self.channels:
            await c.stop()
        self._agents.clear()
        self._session_locks.clear()
        # 关闭 MCP 子进程连接（main.py 装配时挂上来的）
        mcp_manager = getattr(self, "mcp_manager", None)
        if mcp_manager is not None:
            await mcp_manager.shutdown()
