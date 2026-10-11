"""QQ 渠道：QQChannel。

基于 qq-botpy 官方 SDK，同时支持 QQ 群 @ 消息 和 QQ 单聊（C2C）消息。

与飞书渠道的关键区别：
- botpy 的事件回调本身就是 async，且运行在主事件循环中，
  不需要 run_coroutine_threadsafe，直接 await 即可。
- 不能用 bot.run()（内部调 loop.run_until_complete，会与主事件循环冲突），
  必须用 async with bot + await bot.start(appid=..., secret=...)。

注意：
- on_group_at_message_create：QQ 群消息
- on_c2c_message_create：QQ 单聊消息（C2CMessage，不是频道私信 DirectMessage）
- 两者都由 Intents(public_messages=True) 覆盖，不需要 public_guild_messages（那是频道消息）
"""

import re
from collections import deque

import botpy
from botpy.message import C2CMessage, GroupMessage

from bus.queue import InboundMessage, OutboundMessage
from channels.base import Channel

# 入站去重窗口：只保留最近 500 条消息 ID。
# botpy 重连 / 平台重推时同一事件可能投递多次，不拦截就会让 Agent
# 对一句话跑两遍，表现为"机器人连着回复两条语义重复的话"
SEEN_ID_WINDOW = 500


class QQBotClient(botpy.Client):
    """botpy 事件回调类，持有 QQChannel 引用以访问 bus。"""

    def __init__(self, channel: "QQChannel", intents) -> None:
        super().__init__(intents=intents)
        self._channel = channel

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        """QQ 群 @ 消息回调。"""
        # 去重：重复投递的事件直接丢弃，不再进入 Agent
        if not self._channel.mark_seen(message.id):
            return
        sender_id = message.author.member_openid
        chat_id = message.group_openid
        # 去掉 @Bot 前缀
        content = re.sub(r"<@!\d+>", "", message.content or "").strip()

        inbound = InboundMessage(
            channel="qq",
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            raw={"msg_type": "group", "message_id": message.id},
        )
        # botpy 回调运行在主事件循环，直接 await
        await self._channel.bus.publish_inbound(inbound)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        """QQ 单聊消息回调。

        注意：这里用的是 C2CMessage（QQ 单聊），不是 DirectMessage（频道私信）。
        DirectMessage + on_direct_message_create 需要 direct_message intent，
        和 QQ 单聊完全不同。
        """
        if not self._channel.mark_seen(message.id):
            return
        sender_id = message.author.user_openid  # 不是 author.openid
        chat_id = sender_id  # 单聊的 chat_id 就是对方的 openid

        inbound = InboundMessage(
            channel="qq",
            sender_id=sender_id,
            chat_id=chat_id,
            content=message.content or "",
            raw={"msg_type": "c2c", "message_id": message.id},
        )
        await self._channel.bus.publish_inbound(inbound)


class QQChannel(Channel):
    """QQ 渠道：群 @ 消息 + 单聊消息。"""

    def __init__(self, bus, app_id: str, app_secret: str) -> None:
        super().__init__(name="qq", bus=bus)
        self.app_id = app_id
        self.app_secret = app_secret
        self._bot = None
        # 已处理消息 ID（set 判重，deque 限制内存）
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=SEEN_ID_WINDOW)

    def mark_seen(self, message_id: str) -> bool:
        """登记消息 ID；已存在返回 False（重复消息），新消息返回 True。"""
        if not message_id or message_id in self._seen_ids:
            return False
        # deque 满了会自动弹出最早的 ID，同步把它从 set 里删掉
        if len(self._seen_order) == SEEN_ID_WINDOW:
            self._seen_ids.discard(self._seen_order[0])
        self._seen_order.append(message_id)
        self._seen_ids.add(message_id)
        return True

    async def start(self) -> None:
        """启动 QQ 机器人（长连接）。"""
        # public_messages 同时覆盖群消息和 C2C 单聊
        # 不需要 public_guild_messages（那是频道消息）
        intents = botpy.Intents(public_messages=True)
        self._bot = QQBotClient(channel=self, intents=intents)

        # 不能用 bot.run()（内部 loop.run_until_complete 会与主事件循环冲突）
        # 直接 await start()，不需要 asyncio.to_thread
        async with self._bot:
            await self._bot.start(appid=self.app_id, secret=self.app_secret)

    async def send(self, message: OutboundMessage) -> None:
        """发送回复消息。

        根据入站时透传的 extra["msg_type"] 精确选择群接口或单聊接口，
        只发一次。旧实现"先试单聊、失败再试群"有重复发送风险：
        某些错误响应不抛异常，两条消息都会发出去。

        extra["message_id"] 是被回复消息的 ID：QQ 群/单聊的被动消息
        必须携带它（且需在 5 分钟内回复），不带会被平台拒绝。
        """
        msg_type = message.extra.get("msg_type")
        msg_id = message.extra.get("message_id") or None

        if msg_type == "group":
            await self._bot.api.post_group_message(
                group_openid=message.chat_id,
                msg_type=0,
                msg_id=msg_id,
                content=message.content,
            )
        else:
            # msg_type == "c2c"，以及缺失元数据时的兜底（默认单聊）
            await self._bot.api.post_c2c_message(
                openid=message.chat_id,
                msg_type=0,
                msg_id=msg_id,
                content=message.content,
            )

    async def stop(self) -> None:
        """清理 QQ 客户端。"""
        self._bot = None
