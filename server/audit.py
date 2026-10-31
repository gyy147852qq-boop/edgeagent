"""结构化审计日志（JSON Lines）。

每条记录一行 JSON，包含：时间、事件类型、session_id、模型原始输出、解析后命令、
风险等级、校验结果、用户是否确认、执行结果摘要、拒绝原因。

隐私与安全：
- 自动脱敏：键名包含 key/token/secret/authorization 的字段值替换为 ``***``；
- 正则兜底：抹掉形如 ``sk-xxxx``、``Bearer xxxx`` 的字符串；
- 单字段长度上限 4000 字符，避免日志被输出刷爆。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config

SENSITIVE_KEYS = ("key", "secret", "authorization", "token", "password")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
)
MAX_FIELD_CHARS = 4000

logger = logging.getLogger("edgeagent.audit")


def redact(value: Any) -> Any:
    """递归脱敏：密钥字段置空、字符串抹掉疑似密钥。"""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if any(token in str(key).lower() for token in SENSITIVE_KEYS):
                cleaned[key] = "***"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in SECRET_PATTERNS:
            text = pattern.sub("***", text)
        if len(text) > MAX_FIELD_CHARS:
            text = text[:MAX_FIELD_CHARS] + "...[截断]"
        return text
    return value


class AuditLogger:
    """按天滚动的 JSONL 审计日志。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._dir = cfg.logs_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def current_file(self) -> Path:
        return self._dir / f"audit-{datetime.now().strftime('%Y-%m-%d')}.jsonl"

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        """写入一条审计记录并返回落盘对象（便于同一请求复用）。"""
        record: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            **redact(fields),
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                with self.current_file.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                logger.warning("审计日志写入失败: %s", exc)
        return record

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        """读取最近 N 条审计记录（调试与状态面板用）。"""
        path = self.current_file
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


def setup_logging(cfg: Config) -> None:
    """配置根 logger：控制台 + 文件，统一格式。"""
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.logging.level, logging.INFO))
    if root.handlers:  # 避免 uvicorn reload 重复挂载
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = logging.FileHandler(cfg.logs_dir / "edgeagent.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
