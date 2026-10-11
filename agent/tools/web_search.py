"""联网搜索工具：WebSearchTool。

用 DuckDuckGo（ddgs 库）搜索互联网，把结果格式化后回传给 LLM。
ddgs 的 DDGS().text() 是同步阻塞调用，直接 await 不到，
必须用 asyncio.to_thread() 丢到线程池，避免卡住事件循环。
"""

import asyncio

from agent.tools.base import Tool

# 回传给 LLM 的最大字符数，超出截断，避免挤占上下文窗口
MAX_OUTPUT_CHARS = 8000


class WebSearchTool(Tool):
    """用 DuckDuckGo 搜索互联网并返回格式化结果。

    - query 为搜索关键词，max_results 控制返回条数（默认 5）；
    - 无结果返回提示语，所有异常转成错误字符串返回，不抛给主循环；
    - 拼接输出超过 8000 字符时截断。
    """

    name = "web_search"
    description = "搜索互联网获取最新信息。当你需要查询实时信息、最新新闻或不确定的知识时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回几条结果",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    async def execute(self, query: str = "", max_results: int = 5, **kwargs) -> str:
        try:
            # text() 是同步阻塞 API，用 to_thread 包装避免卡住事件循环
            results = await asyncio.to_thread(self._search, query, max_results)
        except Exception as e:
            # ddgs 在无结果时会抛 "No results found" 异常，归为无结果而非出错
            err = str(e).lower()
            if "no results" in err:
                return "未找到相关结果"
            if "timeout" in err or "timed out" in err:
                return (
                    "搜索出错：连接 DuckDuckGo 超时（当前网络环境可能无法直连，"
                    "需要配置代理后重试）"
                )
            return f"搜索出错: {e}"

        if not results:
            return "未找到相关结果"

        output = ""
        for i, item in enumerate(results, 1):
            output += f"### {i}. {item.get('title', '')}\n"
            output += f"链接: {item.get('href', '')}\n"
            output += f"{item.get('body', '')}\n"

        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + "\n...(输出过长，已截断)"
        return output

    @staticmethod
    def _search(query: str, max_results: int) -> list:
        """同步执行搜索（在线程池中调用）。"""
        from ddgs import DDGS

        return list(DDGS().text(query, max_results=max_results))
