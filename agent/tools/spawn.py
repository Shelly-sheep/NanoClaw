"""子 Agent 派生工具：SpawnSubagentTool。

主 Agent 遇到可独立完成的子任务时，可以调用本工具"开一个分身"：
- 分身是一个全新的、无历史的临时 AgentLoop，用固定的任务专员人设；
- 分身共享主 Agent 的工具集（文件、搜索、Shell 等），但不带主 Agent
  的对话上下文，只拿到 task 描述，跑完即销毁，不做会话持久化；
- 支持有限层级的递归分身（max_depth 默认 2），防止无限套娃
  烧 token：第 0 层可以分身，第 1 层分身出来的 Agent 拿到的
  spawn 工具已到第 2 层边界，不能再继续派生。

为什么子 Agent 用 DummySessionManager：子任务是"一次性"的，结果
只以工具返回值的形式交还给主 Agent，没有任何落盘价值；写 JSONL
只会污染 sessions 目录。
"""

import os
from uuid import uuid4

from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry

# 子 Agent 的固定人设：就一句话，不带主 Agent 的人设/时间/记忆，
# 避免分身"想太多"，拿到任务直接干活、直接输出结果
SUBAGENT_SYSTEM_PROMPT = "你是任务专员，完成任务直接输出结果"


class DummySessionManager:
    """空会话管理器：不持久化、不恢复，所有操作都是 no-op。

    独立实现而非继承 SessionManager：父类构造函数会创建目录、
    还带着 JSONL 读写逻辑，子 Agent 一个都用不到，继承反而引入
    隐式依赖。AgentLoop 只依赖下面三个方法，鸭子类型即可。
    """

    def get_history(self, session_key: str) -> list[dict]:
        """子 Agent 永远从零开始，没有历史。"""
        return []

    def save_message(self, session_key: str, message: dict) -> None:
        """不落盘。"""

    def clear(self, session_key: str) -> None:
        """无历史可清。"""


class _FixedContext:
    """固定一句话 System Prompt 的上下文构造器。

    AgentLoop 只依赖 context.workspace（装配压缩器用）和
    build_messages()，因此实现这个最小接口即可，不必继承
    ContextBuilder 再去 monkey-patch 它的私有方法。
    """

    def __init__(self, workspace: str, system_prompt: str) -> None:
        self.workspace = os.path.abspath(workspace)
        self._system_prompt = system_prompt

    def build_messages(
        self, history: list[dict] | None = None, current_message: str = ""
    ) -> list[dict]:
        messages = [{"role": "system", "content": self._system_prompt}]
        if history:
            messages.extend(history)
        if current_message:
            messages.append({"role": "user", "content": current_message})
        return messages


class SpawnSubagentTool(Tool):
    """派生一个临时子 Agent 执行独立子任务并返回结果。

    适用场景：子任务上下文独立（如"去读这 5 个文件并总结"），
    不适合把大量中间过程塞进主 Agent 的对话历史时使用。

    构造参数：
    - provider_factory: 可调用对象，接收 model（可为 None）返回一个
      LLMProvider 实例；model 为 None 时由工厂决定默认模型；
    - tools_registry: 主 Agent 的工具注册表，子 Agent 从中复制工具；
    - workspace / current_depth / max_depth: 工作区与递归层级控制。
    """

    name = "spawn_subagent"
    description = (
        "派生一个独立的子 Agent 执行子任务并返回结果。"
        "子 Agent 不共享当前对话上下文，只接收 task 描述，"
        "适合可独立完成、过程较长的子任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "交给子 Agent 的完整任务描述（需自包含，子 Agent 看不到当前对话）",
            },
            "model": {
                "type": "string",
                "description": (
                    "可选，指定子 Agent 使用的模型名；必须是平台上真实存在的"
                    "完整模型名，不确定就留空（使用默认模型），不要猜测模型名"
                ),
            },
        },
        "required": ["task"],
    }

    def __init__(
        self,
        provider_factory,
        tools_registry: ToolRegistry,
        workspace: str,
        current_depth: int = 0,
        max_depth: int = 2,
        allowed_models: set[str] | None = None,
    ) -> None:
        self.provider_factory = provider_factory
        self.tools_registry = tools_registry
        self.workspace = workspace
        self.current_depth = current_depth
        self.max_depth = max_depth
        # 平台可用模型白名单：模型可能给 model 参数传幻觉值
        # （如 "llama2"/"default"），不在名单内会直接 400（code 20012），
        # 这里回落 None（用工厂默认模型）避免白烧一轮调用。
        # None 表示不做校验
        self.allowed_models = allowed_models

    async def execute(self, task: str = "", model: str | None = None, **kwargs) -> str:
        if not task or not task.strip():
            return "错误：task 不能为空"

        # 模型名白名单校验：非法值回落默认模型（None 交给工厂决定）
        if model and self.allowed_models and model not in self.allowed_models:
            model = None

        # 1. 创建 Provider（工厂决定具体实现与默认模型）
        provider = self.provider_factory(model)

        # 2. 复制工具集：跳过 spawn_subagent 自己，
        #    否则子 Agent 无限制递归派生（下面按层级有条件地重新挂一个）
        sub_tools = ToolRegistry()
        for tool_name, tool in self.tools_registry._tools.items():
            if tool_name == self.name:
                continue
            sub_tools.register(tool)

        # 3. 未达深度上限时，给子 Agent 挂上下一层的 spawn 工具。
        #    current_depth 是"当前这层"的深度，子 Agent 处于 +1 层
        if self.current_depth + 1 < self.max_depth:
            sub_tools.register(
                SpawnSubagentTool(
                    provider_factory=self.provider_factory,
                    tools_registry=sub_tools,
                    workspace=self.workspace,
                    current_depth=self.current_depth + 1,
                    max_depth=self.max_depth,
                    allowed_models=self.allowed_models,
                )
            )

        # 4. 组装临时 Agent：固定一句话人设 + 空持久化
        #    延迟导入避免 agent.loop 与工具包的模块加载顺序耦合
        from agent.loop import AgentLoop

        context = _FixedContext(self.workspace, SUBAGENT_SYSTEM_PROMPT)
        agent = AgentLoop(
            provider=provider,
            tools=sub_tools,
            context=context,
            session_manager=DummySessionManager(),
            model=model,
            # 唯一会话键，避免任何缓存/持久化层面的撞车
            session_key=f"subagent:{uuid4().hex}",
        )

        # 5. 执行子任务，结果原样交还给主 Agent
        return await agent.run(task)
