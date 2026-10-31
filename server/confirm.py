"""人工确认层。

一次确认 = 一个挂起的 :class:`asyncio.Future`：
- 调用方 ``await request(...)`` 阻塞在协程里，不占用线程；
- 前端通过 ``POST /api/confirm`` 回来把 Future 置为结果；
- 超时 / 用户中止 / 服务关闭 一律视为"拒绝"（fail-closed）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import Config

logger = logging.getLogger("edgeagent.confirm")


@dataclass
class PendingConfirm:
    """等待用户点击的一条确认请求。"""

    session_id: str
    command_id: str
    command: str
    risk: str
    reason: str
    future: asyncio.Future[bool] = field(repr=False)
    created_at: float = field(default_factory=time.time)

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.command_id,
            "command": self.command,
            "risk": self.risk,
            "reason": self.reason,
            "expires_in": None,
        }


class ConfirmManager:
    """挂起/恢复确认请求的注册表。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._pending: dict[str, PendingConfirm] = {}

    @staticmethod
    def _key(session_id: str, command_id: str) -> str:
        return f"{session_id}:{command_id}"

    def new_command_id(self) -> str:
        return uuid.uuid4().hex[:12]

    async def request(
        self,
        *,
        session_id: str,
        command_id: str,
        command: str,
        risk: str,
        reason: str,
        abort_event: asyncio.Event | None = None,
    ) -> bool:
        """挂起等待用户确认。返回 True 表示批准，False 表示拒绝/超时/中止。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        pending = PendingConfirm(
            session_id=session_id,
            command_id=command_id,
            command=command,
            risk=risk,
            reason=reason,
            future=future,
        )
        self._pending[self._key(session_id, command_id)] = pending
        abort_task: asyncio.Task[bool] | None = None
        try:
            waiters: list[asyncio.Future[Any]] = [future]
            if abort_event is not None:
                abort_task = asyncio.create_task(abort_event.wait())
                waiters.append(abort_task)
            done, _ = await asyncio.wait(
                waiters,
                timeout=self._cfg.security.confirm_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                logger.info("确认请求超时：session=%s command=%s", session_id, command_id)
                return False
            if future in done:
                return bool(future.result())
            logger.info("确认请求被中止：session=%s command=%s", session_id, command_id)
            return False
        finally:
            self._pending.pop(self._key(session_id, command_id), None)
            if abort_task is not None:
                abort_task.cancel()
            if not future.done():
                future.cancel()

    def resolve(self, session_id: str, command_id: str, approved: bool) -> bool:
        """前端确认回调。返回 True 表示确实有对应的挂起请求被唤醒。"""
        pending = self._pending.get(self._key(session_id, command_id))
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(bool(approved))
        return True

    def reject_all(self, session_id: str) -> int:
        """中止会话时把所有挂起请求置为拒绝，避免协程永久挂住。"""
        count = 0
        for key, pending in list(self._pending.items()):
            if pending.session_id == session_id and not pending.future.done():
                pending.future.set_result(False)
                count += 1
                self._pending.pop(key, None)
        return count

    def pending_for(self, session_id: str) -> list[dict[str, Any]]:
        """当前会话的挂起请求（用于状态接口/断线重连后恢复按钮）。"""
        return [p.payload() for p in self._pending.values() if p.session_id == session_id]

    @property
    def pending_count(self) -> int:
        return len(self._pending)
