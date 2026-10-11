"""NanoClaw 入口：组装所有组件，通过 Gateway 启动多渠道服务。

新版架构：渠道 <-> MessageBus <-> Gateway <-> Agent
- 渠道负责收发消息（CLI / 飞书 / QQ / Web ...）
- Gateway 按 session_key 路由到独立的 Agent 实例（工厂按需创建）
- Agent 不再在 main 里直接创建，而是通过 agent_factory 按需生成

用法：
    python main.py
    交互命令：/exit 退出  /clear 清空历史  /tools 查看工具
"""

import asyncio
import fcntl
import os
import sys

from config import load_config
from providers.openai_compat import OpenAICompatProvider
from agent.tools.registry import ToolRegistry
from agent.tools.filesystem import ReadFileTool, WriteFileTool, ListDirTool
from agent.tools.time import GetTimeTool
from agent.tools.shell import ExecTool
from agent.tools.web_search import WebSearchTool
from agent.tools.web_fetch import WebFetchTool
from agent.tools.mcp import MCPClientManager
from agent.subagent import SubagentManager
from agent.skills import SkillsLoader
from agent.context import ContextBuilder
from agent.memory import MemoryConsolidator

# 项目根目录（main.py 所在位置），数据文件统一放在其下的 workspace/ 内
WORKSPACE = os.path.dirname(os.path.abspath(__file__))

# 项目内直接运行（python main.py）时，把父目录加入模块搜索路径，
# 使 nanoclaw 包形式的导入可解析
sys.path.insert(0, os.path.dirname(WORKSPACE))
from nanoclaw.session.manager import SessionManager
from agent.loop import AgentLoop
from bus.queue import MessageBus
from channels.cli import CLIChannel
from channels.feishu import FeishuChannel
from channels.qq import QQChannel
from channels.web import WebChannel
from gateway import Gateway

BANNER = (
    "=" * 48
    + "\n  🦞 NanoClaw v0.9 — 完整版\n"
    + "=" * 48
)

# 单实例锁文件句柄：保持全局引用到进程退出，flock 随进程结束自动释放
_instance_lock_file = None


def acquire_single_instance_lock() -> None:
    """获取单实例文件锁，防止两个进程同时在线。

    这是"机器人重复回答"的根治措施：飞书 / QQ 长连接如果有两个
    进程用同一份凭证同时在线，平台会把每条消息推给两个连接，
    Agent 就各跑一遍，用户收到两条语义重复的回复。flock 由内核
    在进程退出（含崩溃 kill -9）时自动释放，比手写 PID 文件可靠。
    """
    global _instance_lock_file
    lock_path = os.path.join(WORKSPACE, ".nanoclaw.lock")
    _instance_lock_file = open(lock_path, "w")
    try:
        fcntl.flock(_instance_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("错误：NanoClaw 已有一个实例在运行。")
        print("为避免飞书 / QQ 机器人重复回复，同一目录只允许启动一个实例。")
        print(f"如确认没有其他实例，请删除锁文件后重试：{lock_path}")
        sys.exit(1)
    _instance_lock_file.write(str(os.getpid()))
    _instance_lock_file.flush()


async def build_gateway() -> Gateway:
    """装配所有共享组件，返回已就绪的 Gateway。"""
    config = load_config()
    if not config.api_key:
        print("错误：未配置 API Key。")
        print("请在 config.json 填写 api_key，或设置环境变量 NANOCLAW_API_KEY。")
        sys.exit(1)

    # Provider 工厂：主 Agent 与子 Agent 共用。
    # model 为 None（子 Agent 未显式指定模型）时回落 subagent_model，
    # 再回落主模型——配合 config.json 的 models.main / models.subagent
    def make_provider(model: str | None = None) -> OpenAICompatProvider:
        chosen_model = model or config.subagent_model or config.model
        return OpenAICompatProvider(
            api_key=config.api_key, base_url=config.base_url, model=chosen_model
        )

    provider = make_provider(config.model)

    # 工具注册表所有 Agent 共享（工具本身无状态）
    tools = ToolRegistry()
    for tool in (
        ReadFileTool(config.workspace),
        WriteFileTool(config.workspace),
        ListDirTool(config.workspace),
    ):
        tools.register(tool)
    tools.register(GetTimeTool())
    tools.register(ExecTool(config.workspace))
    tools.register(WebSearchTool())
    tools.register(WebFetchTool())
    # 子 Agent 派生工具放最后注册：它在 execute 时才读注册表，
    # 届时所有工具（含它自己，复制时会跳过）都已就位
    # 白名单 = 配置里的全部模型，防止模型给 model 参数传幻觉值导致 400
    allowed_models = {
        m for m in (config.model, config.subagent_model, config.cheap_model) if m
    }
    subagent_manager = SubagentManager(
        provider_factory=make_provider,
        tools_registry=tools,
        workspace=config.workspace,
        max_depth=config.subagent_max_depth,
        allowed_models=allowed_models,
    )
    subagent_manager.register()
    print(f"  📦 已注册工具：{', '.join(tools.list_tools())}")

    # 技能系统：扫描 workspace/skills 下的 SKILL.md，摘要注入系统提示词
    skills_loader = SkillsLoader(os.path.join(config.workspace, "skills"))
    skills_summary = skills_loader.build_skills_summary()
    print(f"  📚 发现技能：{len(skills_loader.list_skills())} 个")

    # MCP 服务器：以 stdio 子进程方式拉起，远端工具注册进同一张工具表。
    # 给每个服务器补上 cwd=项目根目录，使配置里的相对 args 路径
    # （如 mcp_servers/poetry_server.py）无论从哪个目录启动都能找到。
    # 必须在事件循环内连接（MCP 的会话任务绑定当前 loop），
    # 因此 build_gateway 是 async。
    mcp_config = {}
    for server_name, server_cfg in (config.mcp_servers or {}).items():
        item = dict(server_cfg)
        item.setdefault("cwd", WORKSPACE)
        mcp_config[server_name] = item
    print("  🔌 MCP 连接中...")
    mcp_manager = MCPClientManager(mcp_config)
    await mcp_manager.connect_all()
    for mcp_tool in mcp_manager.get_tools():
        tools.register(mcp_tool)
        print(f"      📎 注册 MCP 工具：{mcp_tool.name}")

    context = ContextBuilder(
        workspace=config.workspace,
        identity_file=config.identity_file,
        skills_summary=skills_summary,
    )

    # 会话持久化：JSONL 放在 WORKSPACE/workspace/sessions，不直接放项目根目录
    session_manager = SessionManager(
        sessions_dir=os.path.join(WORKSPACE, "workspace", "sessions")
    )

    bus = MessageBus()

    # Agent 工厂：每个 session_key 生成独立 AgentLoop
    # （provider / tools / context / session_manager 跨 Agent 共享）
    def agent_factory(session_key: str) -> AgentLoop:
        agent = AgentLoop(
            provider=provider,
            tools=tools,
            context=context,
            model=config.model,
            max_iterations=config.max_iterations,
            session_manager=session_manager,
            session_key=session_key,
        )
        # 挂载压缩器（provider 与主 Agent 共用，摘要走同一模型）
        agent.consolidator = MemoryConsolidator(
            provider=provider,
            workspace=config.workspace,
            token_budget=6000,
        )
        return agent

    # 注册渠道（目前只有 CLI，后续可加飞书 / QQ / Web）
    cli = CLIChannel(bus)
    cli.tools = tools  # /tools 命令需要完整注册表（含描述）
    # /clear 回调：清空 CLI 渠道当前用户（sender_id=local）的历史
    # 注意：gateway 稍后创建，此处用闭包引用
    gateway_ref = []

    def clear_cli_history():
        if gateway_ref and "cli:local" in gateway_ref[0]._agents:
            gateway_ref[0]._agents["cli:local"].clear_history()

    cli._clear_callback = clear_cli_history

    # 注册渠道列表（CLI 始终启用）
    channels = [cli]

    # 飞书渠道（如果配置了 feishu app_id / app_secret 就自动启用）
    if config.feishu_app_id and config.feishu_app_secret:
        feishu_channel = FeishuChannel(bus, config.feishu_app_id, config.feishu_app_secret)
        channels.append(feishu_channel)
        print("[启动] 已启用飞书渠道")
    else:
        print("[启动] 未配置飞书，跳过飞书渠道")

    # QQ 渠道（如果配置了 qq app_id / app_secret 就自动启用）
    if config.qq_app_id and config.qq_app_secret:
        qq_channel = QQChannel(bus, config.qq_app_id, config.qq_app_secret)
        channels.append(qq_channel)
        print("[启动] 已启用 QQ 渠道")
    else:
        print("[启动] 未配置 QQ，跳过 QQ 渠道")

    # Web 渠道（默认启用，通过 config.web.enabled 控制）
    if config.web_enabled:
        web_channel = WebChannel(bus, host=config.web_host, port=config.web_port)
        # 端口预检测必须在任何渠道启动之前：端口被占用几乎总是意味着
        # 已有一个实例在跑，若等网关一起启动才发现，飞书/QQ 可能已经
        # 建立第二条长连接，导致重复回复。这里直接终止本进程
        try:
            web_channel._check_port_available()
        except RuntimeError as e:
            # 此时渠道尚未启动，但 MCP 子进程已连上，先干净回收再退出，
            # 否则 poetry_server 之类的 stdio 子进程会变孤儿
            print(f"错误：{e}")
            await mcp_manager.shutdown()
            sys.exit(1)
        channels.append(web_channel)
        print(f"[启动] 已启用 Web 渠道（http://{config.web_host}:{config.web_port}）")
    else:
        print("[启动] Web 渠道已禁用")

    gateway = Gateway(
        bus=bus,
        channels=channels,
        agent_factory=agent_factory,
        concurrency=config.concurrency,
    )
    # 交给 gateway 在 shutdown 时统一关闭 MCP 子进程连接
    gateway.mcp_manager = mcp_manager
    gateway_ref.append(gateway)

    print(f"  📡 启动渠道：{', '.join(c.name for c in channels)}")
    print(f"  🤖 模型：{config.model}（子 Agent：{config.subagent_model or config.model}）")
    print("  🚀 网关已启动！")
    return gateway


async def _run() -> None:
    """异步装配（含 MCP 连接，必须在事件循环内完成）并运行网关。"""
    gateway = await build_gateway()
    await gateway.run()


def main() -> None:
    print(BANNER)
    acquire_single_instance_lock()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[NanoClaw] 正在退出...")
        import os
        os._exit(0)  # 强制退出，杀掉残留的 WebSocket 子线程


if __name__ == "__main__":
    main()
