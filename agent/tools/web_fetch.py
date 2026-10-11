"""网页抓取工具：WebFetchTool。

抓取指定 URL 的网页内容，用 html2text 转成 Markdown 纯文本回传给 LLM。
安全措施：
- 只允许 http/https 协议，拦截 file://、ftp:// 等协议，防止读取本地文件；
- 15 秒超时 + 跟随重定向；
- httpx.AsyncClient 用 async with 管理，确保连接正确关闭。

典型用法是先 web_search 搜到链接，再用本工具阅读详情。
"""

import re
from urllib.parse import urlparse

from agent.tools.base import Tool

# 回传给 LLM 的最大字符数，超出截断，避免挤占上下文窗口
MAX_OUTPUT_CHARS = 12000

# 常见浏览器 UA，避免被简单反爬规则拦截
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class WebFetchTool(Tool):
    """抓取指定 URL 的网页内容并转成纯文本。

    - 只允许 http/https，其他协议直接拦截；
    - 非 2xx 状态码返回 HTTP 错误信息；
    - HTML 转 Markdown：保留链接、忽略图片、不自动换行；
    - 输出清理连续空行，超过 12000 字符截断；
    - 所有异常转成错误字符串返回，不抛给主循环。
    """

    name = "web_fetch"
    description = "抓取指定 URL 的网页内容。当你需要阅读某个具体网页的详细内容时使用。通常配合 web_search 工具先搜索再抓取。"
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的网页 URL，如 https://example.com",
            }
        },
        "required": ["url"],
    }

    async def execute(self, url: str = "", **kwargs) -> str:
        try:
            parsed = urlparse(url)
        except Exception as e:
            return f"URL 解析失败: {e}"

        if parsed.scheme not in ("http", "https"):
            return "安全拦截：只允许 http/https 协议"
        if not parsed.netloc:
            return f"URL 格式不正确: {url}"

        try:
            import html2text
            import httpx

            # async with 确保连接池/响应正确关闭
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.get(url)

            if not 200 <= response.status_code < 300:
                return f"抓取失败：HTTP {response.status_code}"

            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            text = h.handle(response.text)

            # 连续多个空行压缩成一个
            text = re.sub(r"\n{3,}", "\n\n", text).strip()

            if len(text) > MAX_OUTPUT_CHARS:
                text = text[:MAX_OUTPUT_CHARS] + "\n...(内容过长，已截断)"
            return text
        except Exception as e:
            return f"抓取出错: {type(e).__name__}: {e}"
