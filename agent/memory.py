"""记忆压缩器：MemoryConsolidator。

长对话时消息列表会超出模型上下文窗口，本类在调用 LLM 前做检查：
Token 估算超预算时，把"中间的旧消息"交给 LLM 压成一段摘要，
用一条 system 摘要消息替换它们，保留第一条 system prompt 和
最近 6 条消息不动。

摘要双通道保留：
- 对话中：一条 "[历史摘要]" system 消息，模型继续对话时看得到；
- 磁盘上：追加写入 workspace/memory/HISTORY.md，带时间戳与
  压缩条数，便于审计与回溯。
"""

import json
import os
from datetime import datetime

from providers.base import LLMProvider

# 压缩后保留在摘要两侧的消息数：第一条 system + 最近 6 条
RECENT_MESSAGES = 6

SUMMARY_INSTRUCTION = (
    "请用 3-5 句话概括以下对话的关键信息，保留重要的事实和结论，"
    "省略过程细节和寒暄。只输出摘要，不要其他内容。"
)


class MemoryConsolidator:
    """按 Token 预算压缩对话消息，并把摘要归档到 HISTORY.md。

    - provider: 用于生成摘要的 LLM；
    - workspace: 工作区，摘要落盘到其下 workspace/memory/HISTORY.md；
    - token_budget: 超过该 Token 估算值才触发压缩。
    """

    def __init__(
        self,
        provider: LLMProvider,
        workspace: str,
        token_budget: int = 6000,
    ) -> None:
        self.provider = provider
        self.workspace = workspace
        self.token_budget = token_budget

    @staticmethod
    def estimate_tokens(messages: list[dict]) -> int:
        """粗估消息列表的 Token 数。

        按 JSON 序列化后的字符数 // 2 逐条累加。这是经验近似
        （中文约 1 字 1 token，英文约 4 字符 1 token，取折中），
        不依赖 tiktoken，对"是否需要压缩"的二值判断足够。
        """
        total = 0
        for msg in messages:
            total += len(json.dumps(msg, ensure_ascii=False)) // 2
        return total

    async def maybe_consolidate(self, messages: list[dict]) -> list[dict]:
        """超预算则压缩，否则原样返回。"""
        if self.estimate_tokens(messages) <= self.token_budget:
            return messages

        # 消息数不足以切出"中间部分"时无可压缩，原样返回
        if len(messages) <= 1 + RECENT_MESSAGES:
            return messages

        # tail 边界不能落在 tool 消息上：tool 消息必须紧跟在携带
        # tool_calls 的 assistant 消息之后，而该 assistant 可能已被
        # 划入待压缩区，造成 orphan tool 消息触发 API 400。
        tail_start = len(messages) - RECENT_MESSAGES
        while tail_start > 1 and messages[tail_start].get("role") == "tool":
            tail_start -= 1
        if tail_start <= 1:
            return messages

        head = messages[0]
        tail = messages[tail_start:]
        old_messages = messages[1:tail_start]

        summary = await self._summarize(old_messages)
        summary_msg = {"role": "system", "content": f"[历史摘要]: {summary}"}

        self._save_to_history(summary, len(old_messages))
        return [head, summary_msg, *tail]

    async def _summarize(self, messages: list[dict]) -> str:
        """把旧消息拼成文本，调用 LLM 生成 3-5 句摘要。

        失败（网络错误、模型返回错误）时返回兜底文案，保证
        压缩流程不中断。
        """
        transcript = self._flatten_messages(messages)
        request_messages = [
            {"role": "system", "content": SUMMARY_INSTRUCTION},
            {"role": "user", "content": transcript},
        ]

        try:
            response = await self.provider.chat(request_messages, None)
        except Exception:
            return "（摘要生成失败，旧消息已丢弃）"

        if response.finish_reason == "error" or not response.content:
            return "（摘要生成失败，旧消息已丢弃）"
        return response.content.strip()

    @staticmethod
    def _flatten_messages(messages: list[dict]) -> str:
        """把旧消息展开为可读文本，保留工具调用链的关键信息。

        tool_calls / tool 结果若不展开，摘要会丢失"模型调了什么
        工具、得到什么结果"这一事实来源，所以逐消息显式拼接。
        """
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content")

            if role == "assistant" and msg.get("tool_calls"):
                for call in msg["tool_calls"]:
                    fn = call.get("function", {})
                    lines.append(f"[调用工具] {fn.get('name')}({fn.get('arguments')})")

            if content:
                lines.append(f"[{role}] {content}")

            if role == "tool":
                call_id = msg.get("tool_call_id", "")
                lines.append(f"[工具结果 {call_id}] {content}")

        return "\n".join(lines)

    def _save_to_history(self, summary: str, original_count: int) -> None:
        """追加归档摘要到 workspace/memory/HISTORY.md。"""
        history_path = os.path.join(
            self.workspace, "workspace", "memory", "HISTORY.md"
        )
        os.makedirs(os.path.dirname(history_path), exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = (
            f"## {timestamp}\n"
            f"压缩了 {original_count} 条旧消息\n\n"
            f"{summary}\n\n---\n"
        )
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(entry)
