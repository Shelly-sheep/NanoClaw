"""会话持久化管理器：SessionManager。

把每个会话的消息历史以 JSONL（每行一条 JSON）追加写入文件，
重启进程后可用 session_key 恢复历史。

存储布局：sessions_dir/{session_key}.jsonl
- session_key 中的 ":" 替换为 "_"，避免非法文件名字符，
  如 "cli:direct" → cli_direct.jsonl；
- 所有会话文件都落在 workspace/ 下，不污染项目根目录。

JSONL 而非单个 JSON 的原因：append 模式逐行写入，
不需要每次保存都重写整个文件，长对话也不会丢历史。
"""

import json
import os
from datetime import datetime


class SessionManager:
    """会话消息的保存、读取、清理与列举。

    - sessions_dir: JSONL 文件存储目录，构造时自动创建；
    - 文件中每条消息带 timestamp 便于调试，回传给 OpenAI API
      时在 get_history() 中剥除（API 不认识该字段）。
    """

    def __init__(self, sessions_dir: str = "workspace/sessions") -> None:
        self.sessions_dir = sessions_dir
        os.makedirs(self.sessions_dir, exist_ok=True)

    def _get_session_path(self, session_key: str) -> str:
        """把 session_key 映射为 JSONL 文件路径。

        ":" 是 Windows 文件名非法字符，统一替换为 "_"。
        """
        safe_key = session_key.replace(":", "_")
        return os.path.join(self.sessions_dir, f"{safe_key}.jsonl")

    def save_message(self, session_key: str, message: dict) -> None:
        """追加保存一条消息，自动加 ISO 时间戳。"""
        path = self._get_session_path(session_key)
        record = dict(message)
        record["timestamp"] = datetime.now().isoformat()
        # ensure_ascii=False 让中文原样存储；a 模式追加不改动历史
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_history(self, session_key: str) -> list[dict]:
        """读取会话全部消息，剥除 timestamp 后返回。

        文件不存在返回空列表；空行跳过；损坏行不静默丢弃，
        打印警告但保留其他行，避免一行坏数据清掉整个会话。
        """
        path = self._get_session_path(session_key)
        if not os.path.isfile(path):
            return []

        history: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print(f"警告：跳过损坏的会话行 {path}:{line_no}")
                    continue
                record.pop("timestamp", None)
                history.append(record)
        return history

    def clear(self, session_key: str) -> None:
        """删除指定会话文件；不存在则什么都不做。"""
        path = self._get_session_path(session_key)
        if os.path.isfile(path):
            os.remove(path)

    def list_sessions(self) -> list[str]:
        """列出所有会话的 session_key（从文件名反向还原 "_" → ":"）。"""
        if not os.path.isdir(self.sessions_dir):
            return []

        keys: list[str] = []
        for entry in sorted(os.listdir(self.sessions_dir)):
            if not entry.endswith(".jsonl"):
                continue
            stem = entry[: -len(".jsonl")]
            keys.append(stem.replace("_", ":"))
        return keys
