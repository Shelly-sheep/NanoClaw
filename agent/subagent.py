"""子 Agent 管理器：SubagentManager。

职责：集中管理"派生子 Agent"的配置与工厂逻辑，向工具注册表提供
装配好的 SpawnSubagentTool。 spawn 工具本体（递归层级控制、
Dummy 会话、固定人设）在 agent/tools/spawn.py；本模块只做
"按配置把工具造出来"这一件事，避免 main.py 里堆装配细节。
"""

from agent.tools.registry import ToolRegistry
from agent.tools.spawn import SpawnSubagentTool


class SubagentManager:
    """按全局配置创建并注册子 Agent 派生工具。

    - provider_factory: (model | None) -> LLMProvider，由调用方提供
      （main.py 里按 config 构建，决定主/子模型回落策略）；
    - tools_registry: 主工具表，spawn 工具会被注册进去；
    - workspace / max_depth: 工作区与最大递归层级；
    - allowed_models: 平台可用模型白名单，防止模型给 model 参数
      传幻觉值导致 API 400。
    """

    def __init__(
        self,
        provider_factory,
        tools_registry: ToolRegistry,
        workspace: str,
        max_depth: int = 2,
        allowed_models: set[str] | None = None,
    ) -> None:
        self.provider_factory = provider_factory
        self.tools_registry = tools_registry
        self.workspace = workspace
        self.max_depth = max_depth
        self.allowed_models = allowed_models

    def build_tool(self) -> SpawnSubagentTool:
        """造一个 depth=0 的派生工具（递归层级由工具内部管理）。"""
        return SpawnSubagentTool(
            provider_factory=self.provider_factory,
            tools_registry=self.tools_registry,
            workspace=self.workspace,
            current_depth=0,
            max_depth=self.max_depth,
            allowed_models=self.allowed_models,
        )

    def register(self) -> SpawnSubagentTool:
        """创建并注册到主工具表，返回工具实例。"""
        tool = self.build_tool()
        self.tools_registry.register(tool)
        return tool
