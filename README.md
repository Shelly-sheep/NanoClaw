# 🦞 NanoClaw

一个**从零手写**的多渠道 AI Agent：自己实现 Agent 主循环、工具系统、子 Agent 协同、MCP 接入和消息网关，不依赖任何 Agent 框架（仅用 OpenAI SDK 调模型）。

支持 **CLI / Web / 飞书 / QQ 四种渠道同时在线**对话，流式输出，自动调用工具，难任务可派发子 Agent 分工协作。

## 特性

- **手写 Agent 核心**：思考 → 工具调用 → 再思考的循环，带防爆熔断（同签名工具重复调用 ≥10 次警告、≥20 次熔断）和长上下文自动压缩
- **内置工具**：读写文件、列目录、执行 Shell、联网搜索、网页抓取、获取时间
- **MCP 客户端**：启动时自动拉起 stdio 模式的 MCP Server，其工具注册进同一张工具表（自带古诗词示例服务）
- **多智能体协同**：主 Agent 可通过 `spawn_subagent` 派发独立子 Agent，支持递归（默认最大深度 2）和不同模型分工
- **多渠道网关**：CLI、Web（WebSocket 网页）、飞书、QQ 同时运行，按「渠道:用户」隔离会话；并发 4，同一会话内串行避免历史交叉
- **流式输出**：终端展示完整思考/工具调用过程并逐字输出；Web 逐 token 渲染（带闪烁光标）；飞书/QQ 只收最终结果
- **防重复回复**：单实例文件锁 + 平台消息 ID 滑动窗口去重（1000 条）
- **OpenAI 兼容**：默认对接硅基流动，换 `base_url` 即可接任何 OpenAI 兼容服务

## 环境要求

- Python 3.11+
- 一个 OpenAI 兼容的 LLM API Key（默认使用硅基流动）

## 安装

```bash
git clone https://github.com/Shelly-sheep/NanoClaw.git
cd NanoClaw

# 依赖（如用 conda，先 conda create -n AI_env python=3.11 && conda activate AI_env）
pip install openai mcp fastapi uvicorn lark-oapi qq-botpy ddgs html2text PyYAML
```

## 配置

复制模板并填入自己的凭证：

```bash
cp config.example.json config.json
```

编辑 `config.json`，至少填写 `api_key`：

| 字段 | 说明 |
| --- | --- |
| `api_key` | LLM API Key；也可用环境变量 `NANOCLAW_API_KEY` 覆盖（优先级更高） |
| `base_url` | OpenAI 兼容接口地址，默认 `https://api.siliconflow.cn/v1` |
| `models.main` / `models.subagent` / `models.cheap` | 主 Agent / 子 Agent / 轻量任务模型 |
| `concurrency` | 入站消息并发处理数（默认 4） |
| `subagent_max_depth` | 子 Agent 最大递归层级（默认 2） |
| `mcp_servers` | MCP Server 配置（命令 + 参数），默认含古诗词示例 |
| `feishu` / `qq` | 机器人 app_id / app_secret，**留空则自动跳过对应渠道** |
| `web` | Web 渠道开关、监听地址与端口（默认 `0.0.0.0:8080`） |

> `config.json` 已在 `.gitignore` 中，含真实密钥，请勿提交。

## 启动

```bash
python main.py
```

启动后四渠道按需上线，浏览器打开 `http://localhost:8080` 即可使用 Web 界面；同一目录只允许运行一个实例（文件锁防止机器人重复回复）。

### 终端命令

| 命令 | 作用 |
| --- | --- |
| 直接输入文字 | 与 Agent 对话 |
| `/tools` | 列出全部已注册工具（MCP 工具带标注） |
| `/clear` | 清空当前终端会话历史 |
| `/exit` | 退出 |

## 项目结构

```
nanoclaw/
├── main.py                     # 入口：组装组件、启动网关、启动横幅
├── config.py                   # 配置加载（config.json + 环境变量）
├── config.example.json         # 配置模板（无真实密钥）
├── identity.md                 # Agent 人设
├── gateway.py                  # 网关：渠道路由、会话锁、去重、流式桥接
│
├── agent/                      # Agent 核心
│   ├── loop.py                 # 主循环（防爆熔断 + 流式事件）
│   ├── context.py              # System Prompt 组装（历史 system 自动折叠）
│   ├── memory.py               # 长上下文压缩器
│   ├── skills.py               # 技能加载器（扫描 SKILL.md）
│   ├── subagent.py             # 子 Agent 装配管理
│   └── tools/
│       ├── base.py             # Tool 抽象基类
│       ├── registry.py         # 工具注册表
│       ├── filesystem.py       # read_file / write_file / list_dir
│       ├── shell.py            # exec
│       ├── web_search.py       # web_search（ddgs）
│       ├── web_fetch.py        # web_fetch
│       ├── time.py             # get_time
│       ├── spawn.py            # spawn_subagent
│       └── mcp.py              # MCP 客户端适配器
│
├── providers/                  # LLM 提供方
│   ├── base.py                 # 抽象基类（chat / chat_stream）
│   └── openai_compat.py        # OpenAI 兼容实现（流式 tool_calls 归并）
│
├── session/manager.py          # 会话历史 JSONL 持久化
├── bus/queue.py                # 消息总线（Inbound/Outbound 双队列）
│
├── channels/                   # 多渠道接入
│   ├── base.py                 # Channel 基类（show_process 开关）
│   ├── cli.py                  # 终端（保留思考/工具过程）
│   ├── web.py                  # FastAPI + WebSocket
│   ├── web_ui/index.html       # Web 聊天界面
│   ├── feishu.py               # 飞书长连接 Bot
│   └── qq.py                   # QQ 群 @ / 单聊 Bot
│
├── skills/                     # 技能目录（每个子目录含 SKILL.md）
├── mcp_servers/
│   └── poetry_server.py        # 示例：古诗词 MCP Server
└── workspace/                  # 运行时数据（不入库）
    ├── memory/                 # 长期记忆 MEMORY.md / 压缩历史
    ├── sessions/               # 各渠道会话 JSONL
    └── code/                   # Agent 文件工具工作目录
```

## 渠道说明

- **CLI**：完整展示「思考中 → 调用工具 → 工具结果」过程，回答逐字流式输出
- **Web**：等待时显示打字动画，正文逐 token 出现并带闪烁光标；不展示工具过程，只给最终结果
- **飞书 / QQ**：平台不支持逐字更新，只发送最终完整回复；按平台消息 ID 去重，避免重连重推导致重复回答

## MCP Server

默认配置会启动 `mcp_servers/poetry_server.py`（古诗词查询，3 个工具）。要接入自己的 MCP Server，在 `config.json` 的 `mcp_servers` 中添加：

```json
"mcp_servers": {
    "my_server": {
        "command": "python",
        "args": ["mcp_servers/my_server.py"]
    }
}
```

启动后其工具自动以 `{服务器名}__{工具名}` 的格式注册，模型可直接调用；退出时子进程会被干净回收。
