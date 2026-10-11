"""Shell 命令执行工具：ExecTool。

在指定工作区目录下执行 Shell 命令并把输出回传给 LLM。
安全防线是一道 deny_patterns 正则黑名单：递归删除、格式化磁盘、
关机重启、提权、下载执行脚本、开网络后门、Fork 炸弹等高危命令
在执行前直接拦截，不进入子进程。

注意黑名单只能挡住"已知的坏"，挡不住"未知的好被滥用"
（如 mv 覆盖、python -c 删文件），所以它是最低限度的护栏，
更高危的场景应配合人工确认或沙箱执行。
"""

import asyncio
import os
import re

from agent.tools.base import Tool

# 命令执行硬超时（秒），防止死循环/阻塞命令挂死主循环
EXEC_TIMEOUT_SECONDS = 60

# 回传给 LLM 的最大字符数，超出截断，避免挤占上下文窗口
MAX_OUTPUT_CHARS = 10000

# 危险命令正则黑名单（re.IGNORECASE 匹配）
DENY_PATTERNS = [
    r"rm\s+.*-r",               # rm -r / rm -rf 递归删除
    r"rm\s+-rf",
    r"rmdir\s+/s",              # Windows 递归删除
    r"format\s+",               # 格式化磁盘
    r"mkfs",                    # Linux 格式化
    r"shutdown",
    r"reboot",
    r"sudo\s+",                 # 权限提升
    r"\bsu\b",
    r"chmod\s+777",             # 危险权限修改
    r">\s*/dev/",               # 覆盖设备文件
    r"wget\s+.*\|\s*sh",        # 下载执行恶意脚本
    r"curl\s+.*\|\s*bash",
    r"nc\s+-l",                 # 开网络后门
    r"ncat\s+-l",
    r"dd\s+if=",                # 磁盘镜像覆写
    r":\(\)\{.*\}",             # Fork 炸弹 :(){ :|:& };:
]


class ExecTool(Tool):
    """执行 Shell 命令并返回输出结果。

    - 命令在 workspace 目录下执行（构造函数中转成绝对路径）；
    - 命中 deny_patterns 的命令直接拦截返回，不创建子进程；
    - 60 秒超时，超时 kill 进程；
    - stdout + stderr 拼接返回，超过 10000 字符截断；
    - 所有异常转成错误字符串返回，不抛给主循环。
    """

    name = "exec"
    description = "执行 Shell 命令并返回输出结果"
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 Shell 命令",
            }
        },
        "required": ["command"],
    }

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = os.path.abspath(workspace)

    def _is_dangerous(self, command: str) -> str | None:
        """检查命令是否命中危险模式，命中返回拦截信息，否则返回 None。"""
        for pattern in DENY_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return f"安全拦截：检测到危险命令模式 '{pattern}'"
        return None

    async def execute(self, command: str = "", **kwargs) -> str:
        blocked = self._is_dangerous(command)
        if blocked:
            return blocked

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=EXEC_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return f"命令执行超时（{EXEC_TIMEOUT_SECONDS}秒），已终止"

            output = stdout.decode(errors="replace")
            if stderr:
                output += "标准错误:" + stderr.decode(errors="replace")

            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n...(输出过长，已截断)"

            output += f"\n[退出码: {process.returncode}]"
            return output
        except Exception as e:
            return f"命令执行失败: {type(e).__name__}: {e}"
