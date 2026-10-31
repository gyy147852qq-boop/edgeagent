"""会话与对话历史管理（内存 + 落盘）。

- 每个会话独立保存：模式、工作目录、对话历史、最近命令、计划步骤。
- 落盘文件名为 ``<session_id>.json``，session_id 由 :func:`sanitize_session_id`
  限制为 ``[A-Za-z0-9_-]``，从根上阻断路径穿越。
- 完全访问模式闲置超时后由 :meth:`SessionManager.downgrade_idle` 自动降级为受限模式。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Config

logger = logging.getLogger("edgeagent.session")

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
MAX_RECENT_COMMANDS = 20


def utc_now_iso() -> str:
    """返回秒级 ISO8601 UTC 时间串。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sanitize_session_id(session_id: str | None) -> str:
    """校验并返回合法 session_id；非法时抛 ValueError。"""
    if not session_id:
        return uuid.uuid4().hex[:16]
    candidate = session_id.strip()
    if not SESSION_ID_RE.match(candidate):
        raise ValueError("session_id 非法：只允许 6-64 位字母、数字、下划线或短横线")
    return candidate


@dataclass
class Session:
    """单个会话的运行时状态。"""

    session_id: str
    mode: str = "restricted"
    cwd: str = ""
    title: str = "新会话"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    history: list[dict[str, Any]] = field(default_factory=list)
    recent_commands: list[dict[str, Any]] = field(default_factory=list)
    plan_steps: list[dict[str, Any]] = field(default_factory=list)
    # 以下字段不落盘
    last_active: float = field(default_factory=time.time)
    abort_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    running: bool = False

    # ---------- 序列化 ----------
    def to_disk(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "cwd": self.cwd,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": self.history[-200:],
            "recent_commands": self.recent_commands[-MAX_RECENT_COMMANDS:],
        }

    def meta(self) -> dict[str, Any]:
        """左侧会话列表用的轻量元信息。"""
        return {
            "session_id": self.session_id,
            "title": self.title,
            "mode": self.mode,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": len(self.history),
        }

    def touch(self) -> None:
        self.last_active = time.time()
        self.updated_at = utc_now_iso()

    def context_messages(self, limit: int) -> list[dict[str, Any]]:
        """裁剪后的历史消息（只保留 role/content）。"""
        trimmed = self.history[-limit:]
        return [{"role": m.get("role", "user"), "content": str(m.get("content", ""))} for m in trimmed]


class SessionManager:
    """会话注册表，负责创建、查询、重置与持久化。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._dir = cfg.sessions_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    # ---------- 查询 ----------
    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: str | None) -> Session:
        sid = sanitize_session_id(session_id)
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                session = self._new_session(sid)
                self._sessions[sid] = session
                self.save(session)
            return session

    def list_sessions(self) -> list[dict[str, Any]]:
        return sorted(
            (s.meta() for s in self._sessions.values()),
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

    # ---------- 变更 ----------
    def reset(self, session_id: str | None) -> Session:
        """清空历史与模式，保留 session_id（前端可继续使用同一会话）。"""
        sid = sanitize_session_id(session_id)
        with self._lock:
            session = self._sessions.get(sid) or self._new_session(sid)
            session.history.clear()
            session.recent_commands.clear()
            session.plan_steps.clear()
            session.mode = "restricted"
            session.cwd = self._cfg.default_cwd()
            session.title = "新会话"
            session.touch()
            session.abort_event.set()  # 让在飞的循环尽快退出
            self._sessions[sid] = session
            self.save(session)
            return session

    def append_message(self, session: Session, role: str, content: str, **meta: Any) -> None:
        """追加一条对话消息并落盘。"""
        message: dict[str, Any] = {"role": role, "content": content, "ts": utc_now_iso()}
        if meta:
            message["meta"] = meta
        session.history.append(message)
        if role == "user" and session.title == "新会话":
            session.title = content.strip().splitlines()[0][:24] or "新会话"
        session.touch()
        self.save(session)

    def add_recent_command(self, session: Session, record: dict[str, Any]) -> None:
        """记录最近命令（用于状态面板与下一轮提示词）。"""
        session.recent_commands.append(record)
        session.recent_commands = session.recent_commands[-MAX_RECENT_COMMANDS:]
        session.touch()
        self.save(session)

    def set_mode(self, session: Session, mode: str) -> None:
        session.mode = "full" if mode == "full" else "restricted"
        session.touch()
        self.save(session)

    def save(self, session: Session) -> None:
        """原子写入会话文件（先写临时文件再替换）。"""
        path = self._path(session.session_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(session.to_disk(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("会话落盘失败 %s: %s", path.name, exc)

    def downgrade_idle(self) -> list[Session]:
        """把闲置超阈值的完全访问会话降级为受限模式，返回被降级的会话。"""
        minutes = self._cfg.security.idle_downgrade_minutes
        threshold = minutes * 60
        changed: list[Session] = []
        now = time.time()
        for session in self._sessions.values():
            if session.mode == "full" and not session.running and now - session.last_active > threshold:
                session.mode = "restricted"
                session.touch()
                self.save(session)
                changed.append(session)
                logger.info("会话 %s 闲置 %s 分钟，已自动降级为限制访问模式", session.session_id, minutes)
        return changed

    # ---------- 内部 ----------
    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _new_session(self, session_id: str) -> Session:
        return Session(session_id=session_id, cwd=self._cfg.default_cwd())

    def _load_from_disk(self) -> None:
        """启动时恢复历史会话（损坏文件跳过，不影响启动）。"""
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                sid = sanitize_session_id(str(data.get("session_id") or path.stem))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("跳过无法加载的会话文件 %s: %s", path.name, exc)
                continue
            session = Session(
                session_id=sid,
                mode=str(data.get("mode") or "restricted"),
                cwd=str(data.get("cwd") or self._cfg.default_cwd()),
                title=str(data.get("title") or "历史会话"),
                created_at=str(data.get("created_at") or utc_now_iso()),
                updated_at=str(data.get("updated_at") or utc_now_iso()),
                history=[m for m in data.get("history", []) if isinstance(m, dict)],
                recent_commands=[c for c in data.get("recent_commands", []) if isinstance(c, dict)],
            )
            # 重启后一律回到受限模式，避免"上次是完全访问"被静默继承
            session.mode = "restricted"
            self._sessions[sid] = session

    def iter_sessions(self) -> Iterable[Session]:
        return self._sessions.values()
