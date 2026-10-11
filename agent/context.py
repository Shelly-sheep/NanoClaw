"""上下文构造器：负责组装每次发给 LLM 的 messages。

发给模型的内容 = System Prompt + 历史对话 + 当前用户消息。
System Prompt 承担四件事：
- 人设（identity.md）：决定语气和风格；
- 当前时间：回答"今天/现在"类问题不必调工具；
- 工作区路径：模型调文件工具时拼相对路径，得先知道根在哪；
- 长期记忆（workspace/memory/MEMORY.md）：跨会话要点，第六章实现。

历史对话由调用方（agent/loop.py）维护并传入，本类只负责拼装、
不持有对话状态——主循环以后想改历史策略（裁剪、摘要）不用动这里。
"""

import os
from datetime import datetime

# identity.md 缺失或为空时的兜底人设
DEFAULT_IDENTITY = "你是 NanoClaw，一个务实的 AI 助手，回答简洁准确。"


class ContextBuilder:
    """System Prompt 与完整 messages 列表的组装器。

    - identity_file: 人设文件路径，默认 identity.md（相对路径按
      启动目录解析）；缺失或为空时用内置默认人设，不报错——
      保证项目刚拉下来还没写人设也能跑；
    - 记忆读 workspace/memory/MEMORY.md，为第六章"长期记忆"
      预留的接口：现在文件不存在返回空串，System Prompt 里
      就不出现记忆段落。
    """

    def __init__(
        self,
        workspace: str,
        identity_file: str = "identity.md",
        skills_summary: str = "",
    ) -> None:
        self.workspace = os.path.abspath(workspace)
        self.identity_file = identity_file
        self.skills_summary = skills_summary

    def _load_identity(self) -> str:
        """读人设文件；缺失/为空返回默认人设。"""
        try:
            with open(self.identity_file, "r", encoding="utf-8") as f:
                return f.read().strip() or DEFAULT_IDENTITY
        except FileNotFoundError:
            return DEFAULT_IDENTITY

    def _load_memory(self) -> str:
        """读长期记忆；不存在返回空串（第六章接口预留）。"""
        memory_path = os.path.join(self.workspace, "memory", "MEMORY.md")
        try:
            with open(memory_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def build_system_prompt(self) -> str:
        """拼接完整 System Prompt：人设 + 时间 + 工作区 + 记忆（可选）。"""
        parts = [
            self._load_identity(),
            f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M (%A)')}",
            f"工作区路径：{self.workspace}（文件工具的路径都相对此目录）",
        ]
        memory = self._load_memory()
        if memory:
            parts.append(f"长期记忆：\n{memory}")
        parts.append(self._memory_instructions())
        if self.skills_summary:
            parts.append(f"## 可用技能\n{self.skills_summary}")
        return "\n\n".join(parts)

    def build_messages(
        self, history: list[dict] | None = None, current_message: str = ""
    ) -> list[dict]:
        """组装完整 messages：[system] + 历史对话 + 当前用户消息。

        - history: OpenAI 格式历史，正常情况下不含 system（system 每轮
          重建）；若历史里混入了 system 消息（如长对话压缩产生的
          "[历史摘要]"），必须把它们折叠进本条唯一的 system prompt，
          不能原样保留——硅基流动要求 messages 中 system 只能出现在
          最开头，历史里再出现 system 会报 20015；
        - current_message 为空串时不追加空的用户消息。
        """
        system_content = self.build_system_prompt()
        history = list(history or [])

        # 抽出历史中所有 system 消息，文本并入开头的 system prompt
        folded_systems = [
            m.get("content", "")
            for m in history
            if m.get("role") == "system" and m.get("content")
        ]
        if folded_systems:
            system_content += "\n\n" + "\n\n".join(folded_systems)

        messages = [{"role": "system", "content": system_content}]
        # 非 system 历史按原顺序拼接
        messages.extend(m for m in history if m.get("role") != "system")
        if current_message:
            messages.append({"role": "user", "content": current_message})
        return messages

    def _memory_instructions(self) -> str:
        return """
        \n\n## 记忆管理指引
        当你在对话中发现以下类型的重要信息时，使用 write_file 工具更新 工作目录下的workspace/memory/MEMORY.md：
        - 用户的姓名、职业、技术偏好
        - 用户的项目信息和工作习惯
        - 用户明确要求你记住的事情
        - 用户纠正过你的错误（避免下次再犯）

        更新时读取现有内容，在末尾追加新条目，保持 Markdown 列表格式。
        不要记录琐碎的对话细节，只记录长期有价值的信息。
        """