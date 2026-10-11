"""时间工具：让 Agent 不用猜就能知道"现在"是几点。"""

from datetime import datetime

from agent.tools.base import Tool


class GetTimeTool(Tool):
    """获取当前的日期和时间。

    无参数工具的样板：parameters 的 properties 为空对象，模型
    调用时不需要传任何东西（传了也会被 **kwargs 静默吞掉）。

    为什么 System Prompt 里已有时间还要这个工具：prompt 里的时间
    是会话开始时构建的，长对话中会过期；模型需要精确的"现在"
    （比如算耗时、写时间戳）时调用本工具拿实时值。
    """

    name = "get_time"
    description = "获取当前的日期和时间"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S (%A)")
