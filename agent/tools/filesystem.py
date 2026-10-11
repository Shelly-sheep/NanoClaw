"""文件读写工具：ReadFileTool / WriteFileTool / ListDirTool。

三个工具共享同一套工作区安全检查（resolve_in_workspace）：
所有用户给的路径都按"相对 workspace 根目录"解析成绝对路径，
解析结果一旦落在 workspace 外（如 ../../ 穿越）立即拦截。
这是 LLM Agent 最经典的注入面——模型可能被诱导读 /etc/passwd
或覆盖系统文件，所以路径校验必须收敛在一个函数里，便于审计。
"""

import os

from agent.tools.base import Tool

# 工具输出给 LLM 的最大字符数；超长会挤占上下文窗口且模型也读不完
MAX_OUTPUT_CHARS = 16000


def resolve_in_workspace(workspace: str, user_path: str) -> str | None:
    """把用户路径解析为工作区内的绝对路径，越界则返回 None。

    检查用 startswith(workspace + os.sep) 而不是裸 startswith(workspace)：
    后者会误放行同前缀的兄弟目录（workspace=/a/ws 时，/a/ws2/secret
    也能通过），属于经典的路径校验漏洞。workspace 本身视为合法，
    以便 ListDirTool 列出根目录。
    """
    absolute_path = os.path.abspath(os.path.join(workspace, user_path))
    if absolute_path == workspace or absolute_path.startswith(workspace + os.sep):
        return absolute_path
    return None


class ReadFileTool(Tool):
    """读取工作区内一个文本文件的内容。

    - 入参 file_path 是相对 workspace 的路径（也接受绝对路径，
      但必须在 workspace 内）；
    - 内容超过 16000 字符时截断并附提示，防止大文件挤爆 LLM 上下文；
    - 文件不存在、是目录、路径越界，都以错误字符串返回，不抛异常。
    """

    name = "read_file"
    description = "读取指定文件的内容"
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "相对工作区根目录的文件路径，如 src/main.py",
            }
        },
        "required": ["file_path"],
    }

    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)

    async def execute(self, **kwargs) -> str:
        file_path = kwargs.get("file_path", "")
        path = resolve_in_workspace(self.workspace, file_path)
        if path is None:
            return f"错误：路径越界，禁止访问工作区外的文件：{file_path}"
        if not os.path.isfile(path):
            return f"错误：文件不存在或不是普通文件：{file_path}"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + f"\n...（内容过长已截断，原文件共 {len(content)} 字符）"
        return content


class WriteFileTool(Tool):
    """把内容写入工作区内的文件（整文件覆盖写）。

    - 自动创建缺失的父目录（os.makedirs exist_ok=True），
      让 LLM 写新文件时不用先建目录；
    - 覆盖已有文件时不做二次确认——由上层对话负责；
    - 写入路径同样受工作区越界检查保护。
    """

    name = "write_file"
    description = "把内容写入指定文件（覆盖写入，自动创建父目录）"
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "相对工作区根目录的文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入的完整内容",
            },
        },
        "required": ["file_path", "content"],
    }

    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)

    async def execute(self, **kwargs) -> str:
        file_path = kwargs.get("file_path", "")
        content = kwargs.get("content", "")
        path = resolve_in_workspace(self.workspace, file_path)
        if path is None:
            return f"错误：路径越界，禁止写入工作区外的文件：{file_path}"
        # dirname 不会为空：path 已是绝对路径，至少是 workspace 本身
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已写入 {file_path}（{len(content)} 字符）"


class ListDirTool(Tool):
    """列出工作区内某个目录的内容。

    - 目录名带 / 后缀，文件名后附字节数，一目了然；
    - 结果按名称排序，输出稳定，方便 LLM 对比前后变化；
    - dir_path 缺省时列出 workspace 根目录。
    """

    name = "list_dir"
    description = "列出指定目录下的文件和子目录"
    parameters = {
        "type": "object",
        "properties": {
            "dir_path": {
                "type": "string",
                "description": "相对工作区根目录的目录路径，缺省时列出根目录",
            }
        },
        "required": [],
    }

    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)

    async def execute(self, **kwargs) -> str:
        dir_path = kwargs.get("dir_path") or "."
        path = resolve_in_workspace(self.workspace, dir_path)
        if path is None:
            return f"错误：路径越界，禁止访问工作区外的目录：{dir_path}"
        if not os.path.isdir(path):
            return f"错误：目录不存在：{dir_path}"
        lines = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                lines.append(f"{name}/")
            else:
                lines.append(f"{name}  ({os.path.getsize(full)} 字节)")
        return "\n".join(lines) or "（空目录）"
