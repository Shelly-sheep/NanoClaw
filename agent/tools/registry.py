from agent.tools.base import Tool


class ToolRegistry:
    """工具注册表（工具的"仓库管理员"）。

    职责单一：持有所有已注册的 Tool 实例，并为 Agent 主循环
    提供三件事——
    1. 组装请求体：get_definitions() 把全部工具转成 OpenAI
       Function Calling 的 tools 列表，随消息一起发给 LLM；
    2. 分发执行：LLM 回复里带函数调用时，主循环拿到工具名
       和参数 dict，交给 execute() 查找并执行，返回结果字符串；
    3. 查询盘点：list_tools() 供调试/日志用。

    用 dict 存储是为了 O(1) 按名查找——执行阶段的主路径，
    不能在列表里线性扫。同名重复注册视为"覆盖更新"（比如
    测试时替换实现），不做特殊报错。
    """

    def __init__(self) -> None:
        # key=工具名，value=Tool 实例；工具名即 LLM 侧的 function.name
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。同名工具会被覆盖，用于替换实现。"""
        self._tools[tool.name] = tool

    def get_definitions(self) -> list[dict]:
        """把所有工具组装成 OpenAI 格式定义，直接放进请求体的 tools 字段。"""
        return [tool.to_function_definition() for tool in self._tools.values()]

    def list_tools(self) -> list[str]:
        """返回所有已注册的工具名，用于日志打印和调试。"""
        return list(self._tools.keys())

    async def execute(self, name: str, arguments: dict) -> str:
        """按名字执行工具，结果字符串原样作为 tool 消息回传给 LLM。

        两条错误路径都转成"错误描述字符串"返回而不是抛异常：
        - 工具不存在：LLM 可能编造工具名，属可预期情况；
        - 执行出错：工具 bug 或参数问题，应让 LLM 看到错误后
          自行调整，而不是打断整个 Agent 循环。
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：工具 '{name}' 不存在。可用工具：{', '.join(self._tools) or '无'}"
        try:
            return await tool.execute(**arguments)
        except Exception as e:
            return f"错误：工具 '{name}' 执行失败：{e}"
