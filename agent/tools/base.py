from abc import ABC, abstractmethod

class Tool(ABC):
    """Agent 工具的抽象基类（所有工具的"模具"）。

    一个 Tool = 元信息 + 执行逻辑，两部分职责分离：
    - 元信息（name / description / parameters）经 to_function_definition()
      组装后放进请求体的 tools 列表发给 LLM，让模型知道
      "有哪些工具、什么时候用、怎么传参"；
    - execute() 是实际执行入口：主循环收到 LLM 的函数调用后，
      解析出工具名和参数，交给注册表找到对应 Tool 实例并调用它。

    子类职责（最简写法）：
        class ReadFileTool(Tool):
            name = "read_file"
            description = "读取文件内容"
            parameters = {...}          # JSON Schema
            async def execute(self, **kwargs) -> str:
                ...

    约定：
    - name 用 snake_case，如 "read_file"，LLM 通过它指定工具；
    - parameters 必须是标准 JSON Schema（OpenAI Function Calling 格式），
      否则部分兼容 API 会直接报错；
    - execute() 返回 str，结果原样回传给 LLM；工具内部异常应自行
      捕获并转成错误描述字符串返回，不要抛出去打断主循环。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名，LLM 通过它指定要调用的工具。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """工具功能说明，帮助 LLM 判断何时使用该工具。"""

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """参数定义（JSON Schema），约束 LLM 的入参结构。"""

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具逻辑，返回结果字符串（原样回传给 LLM）。"""

    def to_function_definition(self) -> dict:
        """把元信息组装成 OpenAI Function Calling 的单个 tools 项。

        返回格式：
            {"type": "function",
             "function": {"name": ..., "description": ..., "parameters": ...}}

        注册表对所有 Tool 逐个调用此方法，即可得到请求体
        需要的完整 tools 列表。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }