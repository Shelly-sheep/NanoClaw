"""MCP 客户端集成：把外部 MCP Server 的工具接入 NanoClaw。

两个类：
- MCPClientManager：按配置以 stdio 子进程方式拉起多个 MCP Server，
  维护长连接会话、列出远端工具、转发工具调用、退出时回收子进程；
- MCPTool：把一个远端 MCP 工具包装成本项目的 Tool，名字统一加
  "{服务器名}__{工具名}" 前缀，避免不同服务器的同名工具冲突。

关键实现注意（都是踩过的坑）：
1. ClientSession 必须手动 __aenter__()：它内部的消息读取循环在
   __aenter__ 里启动，只构造不 enter 的话 initialize() 会永久挂死；
2. stdio_client 的上下文管理器同样手动 enter，并把两个管理器都
   保存下来，shutdown 时按"先 session 后 stdio"的顺序 __aexit__，
   否则子进程 / 读写任务会残留；
3. 日志不用 emoji（Windows GBK 控制台会 UnicodeEncodeError），
   统一用 [MCP] / [!] 前缀。
"""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.tools.base import Tool

# inputSchema 缺失时的兜底（理论上 MCP Server 必须返回，防御一下）
_EMPTY_SCHEMA = {"type": "object", "properties": {}}


class MCPTool(Tool):
    """单个远端 MCP 工具的本地适配。

    - name 固定为 "{server}__{tool}"，如 poetry__random_poetry；
    - description / parameters 直接取自 MCP 协议返回的工具描述和
      inputSchema（inputSchema 本身就是 JSON Schema，可直接喂给
      OpenAI Function Calling，无需二次转换）；
    - execute 把调用经 MCPClientManager 转发给对应服务器。
    """

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict,
        client_manager: "MCPClientManager",
    ) -> None:
        self._server_name = server_name
        self._tool_name = tool_name
        self._full_name = f"{server_name}__{tool_name}"
        self._description = description or f"MCP 工具 {self._full_name}"
        self._parameters = input_schema or _EMPTY_SCHEMA
        self._client_manager = client_manager

    # 基类用抽象 property 定义这三个字段（property 是 data descriptor，
    # 不能用同名实例属性覆盖，必须以具体 property 实现）
    @property
    def name(self) -> str:
        return self._full_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict:
        return self._parameters

    async def execute(self, **kwargs) -> str:
        """转发调用到 MCP Server；异常转错误字符串，不打断主循环。"""
        try:
            return await self._client_manager.call_tool(
                self._server_name, self._tool_name, kwargs
            )
        except Exception as e:
            return f"[!] MCP 工具 {self._full_name} 调用失败：{type(e).__name__}: {e}"

    def to_function_definition(self) -> dict:
        """用远端 inputSchema 直接组装 OpenAI tools 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class MCPClientManager:
    """管理多个 MCP Server 的 stdio 连接与工具调用。

    配置形如（config.json 的 mcp_servers 字段）：
        {
          "poetry": {
            "command": "python",
            "args": ["mcp_servers/poetry_server.py"],
            "env": {...}        # 可选
          }
        }
    """

    def __init__(self, mcp_config: dict | None = None) -> None:
        self._config = mcp_config or {}
        # stdio_client 的上下文管理器（持有子进程与读写流）
        self._context_managers: dict[str, object] = {}
        # ClientSession 的上下文管理器（持有消息循环任务）
        self._session_managers: dict[str, object] = {}
        # 已完成 initialize 的活跃 session
        self._sessions: dict[str, ClientSession] = {}
        # 从各服务器收集到的 MCPTool
        self._tools: list[MCPTool] = []
        # 每个服务器一个常驻生命周期任务（进入/退出取消域必须在同一任务）
        # name -> (task, stop_event)
        self._lifecycles: dict[str, tuple] = {}
        # 连接阶段的错误，name -> Exception
        self._connect_errors: dict[str, Exception] = {}

    async def _connect_one(self, server_name: str, server_config: dict) -> None:
        """在【调用方任务】内连接单个 MCP Server 并登记工具。

        注意：本方法只能由该服务器的生命周期任务调用，不能用
        asyncio.gather 各自起任务再在别处 shutdown——stdio_client 和
        ClientSession 内部用 anyio 取消域，进入和退出必须在同一任务，
        否则 __aexit__ 报 "exit cancel scope in a different task"。
        """
        command = server_config.get("command", "")
        args = server_config.get("args", [])

        # 配置里写 "python" 时解析为当前解释器：主程序在虚拟环境
        # （如 conda AI_env）里运行，裸 "python" 可能指向没有装 mcp
        # 的系统解释器，导致子进程起不来
        if command in ("python", "python3"):
            command = sys.executable

        params = StdioServerParameters(
            command=command,
            args=list(args),
            env=server_config.get("env"),
            cwd=server_config.get("cwd"),
        )

        # 手动 __aenter__：拿到 (read, write) 流，管理器留存以便退出
        stdio_cm = stdio_client(params)
        read, write = await stdio_cm.__aenter__()
        self._context_managers[server_name] = stdio_cm

        # 手动 __aenter__：启动内部消息循环，不 enter 会在 initialize 挂死
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        self._session_managers[server_name] = session_cm

        await session.initialize()
        self._sessions[server_name] = session

        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            self._tools.append(
                MCPTool(
                    server_name=server_name,
                    tool_name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema,
                    client_manager=self,
                )
            )
        print(f"    🔌 MCP [{server_name}]: 发现 {len(tools_result.tools)} 个工具")

    async def _lifecycle(
        self,
        server_name: str,
        server_config: dict,
        ready: "asyncio.Event",
        stop: "asyncio.Event",
    ) -> None:
        """单个服务器的常驻生命周期：连接 → 保活 → 同任务内关闭。

        无论成功、连接失败还是被超时取消，回收都在 finally（本任务）内
        完成——这是 anyio 取消域对"进入/退出必须同任务"的硬性要求。
        """
        connected = False
        try:
            await self._connect_one(server_name, server_config)
            connected = True
            ready.set()
            # 保活：会话内部消息循环任务需要本任务一直挂到 shutdown
            await stop.wait()
        except Exception as e:
            # 普通连接失败：记录交给 connect_all 提示
            if not connected:
                self._connect_errors[server_name] = e
                ready.set()
            # 保活期异常：交给 finally 回收
        finally:
            session_cm = self._session_managers.pop(server_name, None)
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except BaseException:  # noqa: BLE001
                    pass
            stdio_cm = self._context_managers.pop(server_name, None)
            if stdio_cm is not None:
                try:
                    await stdio_cm.__aexit__(None, None, None)
                except BaseException:  # noqa: BLE001
                    pass
            self._sessions.pop(server_name, None)

    async def connect_all(self, timeout: float = 30.0) -> None:
        """连接所有配置的服务器。

        每个服务器一个独立生命周期任务，并用 ready 事件 + 超时等待其
        就绪；超时或报错只打印警告并取消该任务，不中断整体启动。
        顺序等待（而非 gather）是为了让取消域的进入/退出始终落在
        各服务器自己的生命周期任务里。
        """
        if not self._config:
            print("[MCP] 未配置 MCP 服务器，跳过")
            return

        for server_name, server_cfg in self._config.items():
            ready = asyncio.Event()
            stop = asyncio.Event()
            task = asyncio.create_task(
                self._lifecycle(server_name, server_cfg, ready, stop)
            )
            self._lifecycles[server_name] = (task, stop)

            try:
                await asyncio.wait_for(ready.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"[!] MCP 服务器 {server_name} 连接超时（{timeout}s），已跳过")
                task.cancel()
                await self._await_task_quiet(task)
                self._lifecycles.pop(server_name, None)
                continue

            err = self._connect_errors.get(server_name)
            if err is not None:
                print(
                    f"[!] MCP 服务器 {server_name} 连接失败："
                    f"{type(err).__name__}: {err}，已跳过"
                )
                await self._await_task_quiet(task)
                self._lifecycles.pop(server_name, None)

    @staticmethod
    async def _await_task_quiet(task) -> None:
        """等待任务结束，吞掉取消类异常。"""
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def get_tools(self) -> list[MCPTool]:
        """返回所有已连接服务器暴露的工具（供注册进 ToolRegistry）。"""
        return list(self._tools)

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> str:
        """调用指定服务器上的工具，把返回内容块拼成纯文本。

        服务器未连接 / 会话丢失时返回错误字符串；远端返回 isError
        时给结果加错误前缀，让模型能感知并调整。
        """
        session = self._sessions.get(server_name)
        if session is None:
            return f"[!] MCP 服务器 {server_name} 未连接，无法调用 {tool_name}"

        result = await session.call_tool(tool_name, arguments or {})

        # 内容可能是多个块，只取文本块拼接（图片 / 资源块暂不支持）
        texts = [
            block.text for block in (result.content or [])
            if getattr(block, "type", None) == "text"
        ]
        text = "\n".join(texts).strip()
        if not text:
            text = "（MCP 工具未返回文本内容）"
        if getattr(result, "isError", False):
            text = f"[!] MCP 工具返回错误：{text}"
        return text

    async def shutdown(self) -> None:
        """通知各生命周期任务自行关闭（保证进入/退出在同一任务）。

        逐个置位 stop 事件并等待任务结束（最多 5s 兜底），由生命周期
        任务自己在 finally 里 __aexit__，从而正确回收会话与子进程。
        """
        for name, (task, stop) in list(self._lifecycles.items()):
            if task.done():
                continue
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                print(f"[!] MCP 服务器 {name} 关闭超时，强制取消")
                task.cancel()
            except Exception as e:  # noqa: BLE001
                print(f"[!] 关闭 MCP 服务器 {name} 时出错：{e}")
        self._lifecycles.clear()
        self._sessions.clear()
        self._context_managers.clear()
        self._session_managers.clear()
        print("[MCP] 已关闭全部 MCP 连接")
