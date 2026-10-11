"""配置加载：config.json + 环境变量。

API Key 的优先级：环境变量 NANOCLAW_API_KEY > config.json 里的 api_key。
真实 key 建议走环境变量（不进 git）；config.json 里留占位符即可。
"""

import json
import os
from dataclasses import dataclass, field, fields

SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"


@dataclass
class NanoClawConfig:
    """NanoClaw 全局配置。

    字段与 config.json 一一对应，JSON 里缺某项就用这里的默认值，
    所以最小可用的 config.json 只需要写 api_key 一行。
    """

    api_key: str = ""
    base_url: str = SILICONFLOW_BASE_URL
    model: str = "Qwen/Qwen3-8B"
    # 子 Agent 默认模型（spawn 时未显式指定 model 用它）；
    # 为空则与主模型相同
    subagent_model: str = ""
    # 轻量模型（摘要 / 压缩等低成本场景预留）
    cheap_model: str = ""
    workspace: str = "."
    max_iterations: int = 32
    identity_file: str = "identity.md"

    # 入站消息并发处理数：不同渠道 / 不同用户可同时对话
    concurrency: int = 4

    # 子 Agent（spawn_subagent）最大递归层级：
    # 2 表示主 Agent 可派生子 Agent，子 Agent 还能再派生一层；
    # 设为 1 则只允许一层分身，设为 0 等于关闭派生能力
    subagent_max_depth: int = 2

    # MCP 服务器配置：{服务器名: {command, args, env?}}，
    # 启动时以 stdio 子进程方式拉起，其工具自动注册进工具表
    mcp_servers: dict = field(default_factory=dict)

    # 飞书配置
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # QQ 配置
    qq_app_id: str = ""
    qq_app_secret: str = ""

    # Web 配置
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8080


def load_config(config_path: str = "config.json") -> NanoClawConfig:
    """读取配置文件生成 NanoClawConfig。

    - 文件不存在或 JSON 损坏：直接用默认值（首次运行友好，
      不强迫用户先建好配置才能启动）；
    - JSON 里的未知字段忽略（手滑写错键名不至于炸掉启动）；
    - 环境变量 NANOCLAW_API_KEY 优先级最高，覆盖文件里的 api_key。
    """
    config = NanoClawConfig()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    known = {f.name for f in fields(NanoClawConfig)}
    for key, value in data.items():
        if key in known:
            setattr(config, key, value)

    # 多模型配置（新格式）：{"models": {"main": ..., "subagent": ..., "cheap": ...}}
    # 优先级高于上面的扁平 "model" 字段；旧格式只写 "model" 也完全兼容
    models = data.get("models", {})
    if isinstance(models, dict):
        if models.get("main"):
            config.model = models["main"]
        if models.get("subagent"):
            config.subagent_model = models["subagent"]
        if models.get("cheap"):
            config.cheap_model = models["cheap"]

    if env_key := os.environ.get("NANOCLAW_API_KEY"):
        config.api_key = env_key

    # 加载飞书配置
    feishu = data.get("feishu", {})
    if feishu.get("app_id"):
        config.feishu_app_id = feishu["app_id"]
    if feishu.get("app_secret"):
        config.feishu_app_secret = feishu["app_secret"]

    # 加载 QQ 配置
    qq = data.get("qq", {})
    if qq.get("app_id"):
        config.qq_app_id = qq["app_id"]
    if qq.get("app_secret"):
        config.qq_app_secret = qq["app_secret"]

    # 加载 Web 配置
    web = data.get("web", {})
    if "enabled" in web:
        config.web_enabled = bool(web["enabled"])
    if web.get("host"):
        config.web_host = web["host"]
    if web.get("port"):
        config.web_port = int(web["port"])
    return config
