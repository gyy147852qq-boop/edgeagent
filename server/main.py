"""EdgeAgent 本地代理入口（FastAPI）。

职责：
1. 只监听 127.0.0.1，静态托管 ``web/`` 下的单页应用；
2. Bearer Token + Origin/Host 校验中间件；
3. ``/api/chat`` 驱动「模型 → 解析 → 校验 → 确认 → 执行 → 回传」闭环，以 SSE 流式下发；
4. 会话、模式、中止、审计等管理接口。

安全约定：api_key 只在服务端内存中，任何响应体都不会包含它。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audit import AuditLogger, setup_logging
from .config import Config, load_config
from .confirm import ConfirmManager
from .executor import ExecutionResult, Executor
from .guard import WHITELIST_RESTRICTED, PermissionGuard, Verdict
from .llm_client import LLMError, LLMRouter, augment_messages, build_llm_client
from .providers import build_selection, describe_presets, get_preset
from .parser import ParseError, ParsedCommand, parse_model_output
from .prompts import build_correction_prompt, build_system_prompt
from .rag import KnowledgeBase
from .session import Session, SessionManager, sanitize_session_id
from .state import StateCollector
from .web_agent import WebAgentError, driver_status, list_sites

VERSION = "1.0.0"
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
MAX_PARSE_ATTEMPTS = 3  # 首次 + 最多 2 次纠错
MODEL_OUTPUT_CONTEXT_CHARS = 3000

logger = logging.getLogger("edgeagent.main")

Emit = Callable[[dict[str, Any]], Any]


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=8000)
    mode: str = "restricted"
    # 前端切入完全访问模式时必须在二次确认后置 true，否则拒绝切换
    confirm_full_access: bool = False


class ConfirmRequest(BaseModel):
    session_id: str
    command_id: str
    approved: bool


class ResetRequest(BaseModel):
    session_id: str | None = None


class AbortRequest(BaseModel):
    session_id: str
    kill_process: bool = True


class ModeRequest(BaseModel):
    session_id: str
    mode: str
    confirm_full_access: bool = False


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=20)


class ProviderRequest(BaseModel):
    """切换 AI 服务商。

    ``kind``：``openai``（API）/ ``web``（网页版驱动）/ ``mock``（内置模型）。
    """

    kind: str = Field(default="openai", pattern="^(openai|web|mock)$")
    preset: str = Field(default="deepseek", min_length=1, max_length=40)
    model: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=300)


class WebLoginRequest(BaseModel):
    """网页版登录：``start=true`` 打开浏览器，``false`` 关闭并保存登录态。"""

    start: bool = True


def sse(event: dict[str, Any]) -> str:
    """把事件序列化成 SSE 帧。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def load_or_create_token(cfg: Config) -> str:
    """读取或生成访问 token，并写入 ``.agent_token``（权限 600）。"""
    env_token = os.getenv("AGENT_TOKEN", "").strip()
    path = cfg.token_path
    if env_token:
        _write_token(path, env_token)
        return env_token
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if len(existing) >= 16:
            return existing
    token = secrets.token_urlsafe(32)
    _write_token(path, token)
    return token


def _write_token(path: Path, token: str) -> None:
    try:
        path.write_text(token, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError as exc:  # 写不了就只放在内存里
        logger.warning("token 文件写入失败（%s），本次仅内存有效", exc)


def _origin_allowed(origin: str, cfg: Config) -> bool:
    """只接受本机来源，且端口必须是本服务端口。"""
    if not origin:
        return True
    try:
        from urllib.parse import urlparse

        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host not in LOCAL_HOSTNAMES:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return port == cfg.server.port


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------
def create_app(cfg: Config | None = None) -> FastAPI:
    """构建 FastAPI 应用。测试可传入自定义 Config。"""
    cfg = cfg or load_config()
    setup_logging(cfg)
    token = load_or_create_token(cfg)

    audit = AuditLogger(cfg)
    sessions = SessionManager(cfg)
    guard = PermissionGuard(cfg)
    executor = Executor(cfg)
    confirmer = ConfirmManager(cfg)
    llm = LLMRouter(cfg, cfg.base_dir)
    collector = StateCollector(cfg)
    kb = KnowledgeBase(cfg)
    started_at = time.time()

    for warning in cfg.warnings:
        logger.warning(warning)
    if not llm.enabled:
        logger.warning(
            "当前 provider 不可用（preset=%s，缺少密钥？），/api/chat 将返回错误",
            llm.selection.preset,
        )
    elif llm.is_mock:
        logger.warning("当前使用内置 mock 模型，仅用于离线自测")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        watchdog = asyncio.create_task(_idle_watchdog())
        opener = asyncio.create_task(_open_browser_later())
        logger.info("EdgeAgent 已启动：http://%s:%s", cfg.server.host, cfg.server.port)
        logger.info("允许目录：%s", ", ".join(cfg.security.allowed_dirs))
        try:
            yield
        finally:
            watchdog.cancel()
            opener.cancel()
            for task in (watchdog, opener):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            logger.info("EdgeAgent 已停止")

    async def _idle_watchdog() -> None:
        """后台任务：完全访问模式闲置超时自动降级。"""
        # 检查间隔随阈值自适应：阈值被缩到 30 秒时（自动化验证），间隔也随之缩短
        threshold_seconds = max(1.0, cfg.security.idle_downgrade_minutes * 60)
        interval = max(2.0, min(20.0, threshold_seconds / 4))
        while True:
            await asyncio.sleep(interval)
            try:
                for session in sessions.downgrade_idle():
                    audit.log(
                        "mode_auto_downgraded",
                        session_id=session.session_id,
                        mode="restricted",
                        reason=f"闲置超过 {cfg.security.idle_downgrade_minutes} 分钟",
                    )
            except Exception:  # 看门狗永不因异常退出
                logger.exception("闲置降级检查失败")

    async def _open_browser_later() -> None:
        if not cfg.server.open_browser:
            return
        await asyncio.sleep(1.2)
        url = f"http://127.0.0.1:{cfg.server.port}/?token={token}"
        try:
            await asyncio.to_thread(webbrowser.open, url)
        except Exception:  # pragma: no cover - 浏览器缺失不影响服务
            logger.info("请手动在 Edge 打开 %s", url)

    app = FastAPI(title="EdgeAgent", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)

    # ---------------- 中间件 ----------------
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Host/Origin 校验 + 安全响应头（含 CSP）。"""
        host_header = (request.headers.get("host") or "").split(":")[0].strip().lower()
        if host_header and host_header not in LOCAL_HOSTNAMES:
            return JSONResponse({"detail": "非法 Host（仅允许本机访问）"}, status_code=403)

        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin, cfg):
            return JSONResponse({"detail": "非法 Origin"}, status_code=403)

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.endswith(
            (".html", ".js", ".css")
        ):
            # 前端资源必须每次回源校验：exe 升级后浏览器若用启发式缓存，
            # 会继续加载旧版 app.js，导致「界面还是老样子」（按钮缺失等）。
            # no-cache 允许缓存但强制校验 ETag，比 no-store 更省流量。
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    def require_token(authorization: str | None = Header(default=None)) -> None:
        """Bearer Token 校验依赖。"""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="缺少 Bearer Token")
        provided = authorization[7:].strip()
        if not secrets.compare_digest(provided, token):
            raise HTTPException(status_code=401, detail="Token 无效")

    auth = [Depends(require_token)]

    # ---------------- 基础接口 ----------------
    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """免鉴权存活探测：前端用于判断"代理是否在线"（不含任何敏感信息）。"""
        return {"ok": True, "version": VERSION}

    @app.get("/api/health", dependencies=auth)
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": VERSION,
            "llm_configured": llm.enabled,
            "provider": llm.provider,
            "model": llm.model,
            "preset": llm.selection.preset,
            "uptime_seconds": int(time.time() - started_at),
            "pending_confirms": confirmer.pending_count,
            "idle_downgrade_minutes": cfg.security.idle_downgrade_minutes,
            "knowledge": {"enabled": cfg.knowledge.enabled, "dirs": list(cfg.knowledge.dirs)},
        }

    # ---------------- AI 服务商切换 ----------------
    @app.get("/api/providers", dependencies=auth)
    async def list_providers() -> dict[str, Any]:
        """可选服务商列表 + 当前选择。密钥只以布尔值呈现，绝不下发。"""
        return {
            "ok": True,
            "current": llm.selection.public(),
            "presets": describe_presets(cfg.llm.api_key_env),
            "web_sites": list_sites(),
            "web_available": driver_status()["available"],
            "web_driver": driver_status(),
        }

    @app.post("/api/provider", dependencies=auth)
    async def switch_provider(req: ProviderRequest) -> dict[str, Any]:
        """热切换服务商：立即生效、写入 provider.json、记审计。"""
        try:
            selection = build_selection(
                kind=req.kind,
                preset_id=req.preset,
                model=req.model,
                base_url=req.base_url,
                fallback_env=cfg.llm.api_key_env,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        llm.apply(selection)
        audit.log(
            "provider_changed",
            kind=selection.kind,
            preset=selection.preset,
            model=selection.model,
            key_source=selection.key_source or None,
            enabled=selection.enabled,
        )

        warning: str | None = None
        if selection.kind == "openai" and not selection.api_key:
            preset = get_preset(selection.preset)
            warning = (
                f"未检测到 {preset.label} 的密钥。请在 .env 里填写 {preset.api_key_env}="
                f"（或通用的 {cfg.llm.api_key_env}）后重启服务，否则发消息会报配置错误。"
            )
        return {"ok": True, "current": llm.selection.public(), "warning": warning}

    @app.post("/api/provider/test", dependencies=auth)
    async def test_provider() -> dict[str, Any]:
        """用一条极短消息验证当前 provider 是否真的通。"""
        if llm.is_web:
            return {
                "ok": False,
                "code": "web_needs_login",
                "message": "网页版请先点「打开网页」登录，然后直接发一条消息验证；此接口不做自动化探测。",
            }
        if not llm.enabled:
            preset = get_preset(llm.selection.preset)
            return {
                "ok": False,
                "code": "no_api_key",
                "message": f"缺少密钥：请在 .env 填写 {preset.api_key_env} 后重启服务。",
            }

        started = time.perf_counter()
        try:
            reply = await llm.chat([{"role": "user", "content": "只回复两个字：正常"}])
        except LLMError as exc:
            audit.log("provider_test", preset=llm.selection.preset, ok=False, code=exc.code)
            return {"ok": False, "code": exc.code, "message": exc.message}
        except Exception as exc:  # 网络/证书等意外错误也要给前端一个可读结果
            audit.log("provider_test", preset=llm.selection.preset, ok=False, code="unexpected")
            return {"ok": False, "code": "unexpected", "message": f"{type(exc).__name__}: {exc}"}

        latency = round((time.perf_counter() - started) * 1000, 1)
        audit.log("provider_test", preset=llm.selection.preset, ok=True, latency_ms=latency)
        return {
            "ok": True,
            "latency_ms": latency,
            "model": llm.model,
            "sample": reply.strip()[:60],
        }

    @app.post("/api/web/login", dependencies=auth)
    async def web_login(req: WebLoginRequest) -> dict[str, Any]:
        """打开/关闭网页版登录窗口。

        登录态依赖 Playwright 的持久化 profile：``start=false`` 时关闭浏览器才会落盘，
        所以前端要提供「已完成登录」按钮。
        """
        if not req.start:
            try:
                logged_in = await llm.check_web_login()
            except WebAgentError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc
            audit.log("web_login", action="verify", preset=llm.selection.preset, logged_in=logged_in)
            if logged_in:
                # 登录完成后切到无头模式：之后发消息不再弹出浏览器窗口
                went_headless = False
                try:
                    went_headless = await llm.switch_web_headless()
                except Exception as exc:  # 切换失败不影响"已登录"这个事实
                    logger.warning("切换到无头模式失败：%s", exc)
                return {
                    "ok": True,
                    "logged_in": True,
                    "headless": went_headless,
                    "message": "已登录，可以直接发消息了" + ("（已切换到后台模式，不再弹窗）" if went_headless else ""),
                }
            return {
                "ok": False,
                "logged_in": False,
                "message": (
                    "还没登录。请在弹出的浏览器窗口里完成登录（扫码或账号密码），"
                    "登录成功后页面右上角会显示你的头像，再点一次「我已完成登录」。"
                ),
            }

        try:
            url = await llm.open_web_browser()
        except WebAgentError as exc:
            raise HTTPException(status_code=400, detail=exc.message) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"打开浏览器失败：{exc}") from exc

        audit.log("web_login", action="open", preset=llm.selection.preset, url=url)
        return {
            "ok": True,
            "url": url,
            "message": "浏览器已打开。请在该窗口内登录，登录成功后点「已完成登录」保存。",
        }

    # ---------------- 本地知识库（RAG） ----------------
    @app.post("/api/knowledge/search", dependencies=auth)
    async def knowledge_search(req: KnowledgeSearchRequest) -> dict[str, Any]:
        """检索本地文档片段。只读、受 Bearer Token 保护。"""
        if not cfg.knowledge.enabled:
            raise HTTPException(status_code=400, detail="知识库未启用（knowledge.enabled=false）")
        started = time.perf_counter()
        hits = await asyncio.to_thread(kb.search, req.query, req.top_k)
        took_ms = int((time.perf_counter() - started) * 1000)
        results = [hit.to_dict() for hit in hits]
        audit.log(
            "knowledge_search",
            query=req.query,
            top_k=req.top_k or cfg.knowledge.top_k,
            hits=len(results),
            sources=[item["source"] for item in results],
            took_ms=took_ms,
        )
        return {
            "ok": True,
            "query": req.query,
            "results": results,
            "took_ms": took_ms,
            "stats": await asyncio.to_thread(kb.stats),
        }

    @app.post("/api/knowledge/reindex", dependencies=auth)
    async def knowledge_reindex() -> dict[str, Any]:
        """强制重建索引（新增/修改文档后立即生效）。"""
        await asyncio.to_thread(kb.invalidate)
        stats = await asyncio.to_thread(kb.stats)
        audit.log("knowledge_reindex", files=stats.get("indexed_files"), chunks=stats.get("chunks"))
        return {"ok": True, "stats": stats}

    @app.get("/api/knowledge/status", dependencies=auth)
    async def knowledge_status() -> dict[str, Any]:
        return {"ok": True, "stats": await asyncio.to_thread(kb.stats)}

    @app.post("/api/session/create", dependencies=auth)
    async def create_session(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """新建会话（verify.ps1 与第三方客户端使用的显式入口）。"""
        raw_id = (payload or {}).get("session_id")
        try:
            session = sessions.get_or_create(str(raw_id) if raw_id else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit.log("session_created", session_id=session.session_id, mode=session.mode)
        return {"ok": True, "session_id": session.session_id, "session": session.meta()}

    # ---------------- 会话与状态 ----------------
    @app.get("/api/sessions", dependencies=auth)
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": sessions.list_sessions()}

    @app.get("/api/state", dependencies=auth)
    async def get_state(session_id: str) -> dict[str, Any]:
        session = _require_session(session_id)
        return collector.snapshot(
            session,
            pending=confirmer.pending_for(session.session_id),
            running=session.running,
        )

    @app.get("/api/session/history", dependencies=auth)
    async def session_history(session_id: str, limit: int = 200) -> dict[str, Any]:
        """会话历史（前端切换会话/刷新页面后恢复聊天气泡用）。"""
        session = _require_session(session_id)
        limit = max(1, min(500, limit))
        messages = []
        for msg in session.history[-limit:]:
            meta = msg.get("meta") or {}
            messages.append(
                {
                    "role": msg.get("role"),
                    "content": msg.get("content"),
                    "ts": msg.get("ts"),
                    "kind": meta.get("kind", ""),
                }
            )
        return {
            "session_id": session.session_id,
            "mode": session.mode,
            "cwd": session.cwd,
            "messages": messages,
        }

    @app.post("/api/session/reset", dependencies=auth)
    async def reset_session(req: ResetRequest) -> dict[str, Any]:
        session = sessions.reset(req.session_id)
        confirmer.reject_all(session.session_id)
        audit.log("session_reset", session_id=session.session_id)
        return {"ok": True, "session": session.meta()}

    @app.post("/api/mode", dependencies=auth)
    async def switch_mode(req: ModeRequest) -> dict[str, Any]:
        """切换权限模式。切到完全访问必须带 confirm_full_access=true（前端二次确认）。"""
        session = _require_session(req.session_id)
        target = "full" if req.mode == "full" else "restricted"
        if target == "full" and not req.confirm_full_access:
            raise HTTPException(status_code=400, detail="切换到完全访问模式必须二次确认")
        if session.mode != target:
            sessions.set_mode(session, target)
            audit.log(
                "mode_changed",
                session_id=session.session_id,
                mode=target,
                confirmed=req.confirm_full_access if target == "full" else None,
            )
        return {"ok": True, "mode": session.mode}

    @app.post("/api/confirm", dependencies=auth)
    async def confirm_command(req: ConfirmRequest) -> dict[str, Any]:
        session = _require_session(req.session_id)
        approved = bool(req.approved)
        found = confirmer.resolve(session.session_id, req.command_id, approved)
        audit.log(
            "user_confirm",
            session_id=session.session_id,
            command_id=req.command_id,
            approved=approved,
            found=found,
        )
        if not found:
            raise HTTPException(status_code=404, detail="没有找到待确认的命令（可能已超时或已中止）")
        return {"ok": True}

    @app.post("/api/abort", dependencies=auth)
    async def abort(req: AbortRequest) -> dict[str, Any]:
        """一键中止：置中止位、拒绝所有挂起确认、可选杀掉进程树。"""
        session = _require_session(req.session_id)
        session.abort_event.set()
        rejected = confirmer.reject_all(session.session_id)
        killed = await executor.kill(session.session_id) if req.kill_process else False
        audit.log(
            "abort",
            session_id=session.session_id,
            kill_process=req.kill_process,
            killed=killed,
            rejected_confirms=rejected,
        )
        return {"ok": True, "killed": killed, "rejected_confirms": rejected}

    # ---------------- 主闭环 ----------------
    @app.post("/api/chat", dependencies=auth)
    async def chat(req: ChatRequest) -> StreamingResponse:
        try:
            session = sessions.get_or_create(req.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if session.running:
            raise HTTPException(status_code=409, detail="该会话正在执行中，请先中止或等待完成")

        message = req.message.strip()
        if not message:
            raise HTTPException(status_code=400, detail="message 不能为空")

        # 请求里带的 mode 视为用户显式选择：切到 full 必须携带二次确认标记
        target_mode = "full" if req.mode == "full" else "restricted"
        if target_mode != session.mode:
            if target_mode == "full" and not req.confirm_full_access:
                raise HTTPException(status_code=400, detail="切换到完全访问模式必须二次确认")
            sessions.set_mode(session, target_mode)
            audit.log(
                "mode_changed",
                session_id=session.session_id,
                mode=target_mode,
                confirmed=req.confirm_full_access if target_mode == "full" else None,
                via="chat",
            )

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(sse(event))

        async def producer() -> None:
            try:
                await _run_turn(session, message, emit)
            except LLMError as exc:
                logger.warning("LLM 错误：%s", exc.message)
                audit.log("llm_error", session_id=session.session_id, code=exc.code, message=exc.message)
                await emit({"type": "error", "data": {"message": exc.message, "code": exc.code}})
                await emit({"type": "done", "data": {"summary": "本轮因模型调用失败而终止"}})
            except ParseError as exc:
                await emit(
                    {
                        "type": "error",
                        "data": {"message": f"模型输出无法解析：{exc.message}", "code": "parse_failed"},
                    }
                )
                await emit({"type": "done", "data": {"summary": "本轮因指令格式错误而终止"}})
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # 兜底：任何异常都不能让服务崩
                logger.exception("对话循环异常")
                audit.log("internal_error", session_id=session.session_id, error=str(exc))
                await emit({"type": "error", "data": {"message": f"内部错误：{exc}", "code": "internal"}})
                await emit({"type": "done", "data": {"summary": "本轮因内部错误而终止"}})
            finally:
                session.running = False
                await queue.put(None)

        task = asyncio.create_task(producer())

        async def event_stream() -> AsyncIterator[str]:
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # 心跳，防止中间层断连
                        continue
                    if item is None:
                        break
                    yield item
            finally:
                # 客户端断开时不取消正在执行的命令（避免"结果丢失但已落地"），
                # 由前端重连后通过 /api/state 恢复视图。
                if not task.done():
                    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---------------- 闭环实现 ----------------
    def _require_session(session_id: str) -> Session:
        try:
            sid = sanitize_session_id(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先新建会话")
        return session

    def _build_messages(session: Session) -> list[dict[str, Any]]:
        system_prompt = build_system_prompt(
            mode=session.mode,
            allowed_dirs=cfg.security.allowed_dirs,
            cwd=session.cwd,
            max_timeout=cfg.executor.max_timeout,
            recent_commands=session.recent_commands,
            whitelist=sorted(WHITELIST_RESTRICTED),
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        messages.extend(session.context_messages(cfg.session.max_history_messages))
        return messages

    async def _ask_model(session: Session, emit: Emit, query: str) -> str:
        """调用模型并逐字下发 token 事件，返回完整文本。

        调用前先按意图做一次本地知识库检索，命中则把片段注入 system 提示词的 <context>，
        并通过 SSE 的 ``knowledge`` 事件告知前端「本次回答引用了哪些文件」。
        """
        messages = _build_messages(session)
        messages, hits = await augment_messages(kb, messages, query)
        if hits:
            audit.log(
                "knowledge_injected",
                session_id=session.session_id,
                query=query,
                sources=[hit.get("source", "") for hit in hits],
                scores=[hit.get("score", 0) for hit in hits],
            )
            await emit({"type": "knowledge", "data": {"hits": hits}})

        text_parts: list[str] = []
        async for delta in llm.stream_chat(messages):
            text_parts.append(delta)
            await emit({"type": "token", "delta": delta})
        return "".join(text_parts)

    async def _parse_with_retry(session: Session, text: str, emit: Emit, query: str) -> ParsedCommand:
        """解析模型输出，失败时回传纠错提示，最多 2 次重试。"""
        last_error: ParseError | None = None
        for attempt in range(MAX_PARSE_ATTEMPTS):
            try:
                parsed = parse_model_output(
                    text,
                    default_timeout=cfg.executor.default_timeout,
                    max_timeout=cfg.executor.max_timeout,
                )
                for warning in parsed.warnings:
                    await emit({"type": "error", "data": {"message": warning, "code": "parse_warning"}})
                return parsed
            except ParseError as exc:
                last_error = exc
                audit.log(
                    "parse_error",
                    session_id=session.session_id,
                    attempt=attempt + 1,
                    error=exc.message,
                    model_output=text,
                )
                if attempt >= MAX_PARSE_ATTEMPTS - 1:
                    break
                await emit(
                    {
                        "type": "error",
                        "data": {
                            "message": f"模型输出不合法（{exc.message}），正在自动纠错重试 {attempt + 1}/{MAX_PARSE_ATTEMPTS - 1}",
                            "code": "parse_retry",
                        },
                    }
                )
                sessions.append_message(session, "user", build_correction_prompt(exc.message, exc.hint), kind="correction")
                text = await _ask_model(session, emit, query)
                sessions.append_message(session, "assistant", text)
        raise last_error or ParseError("解析失败")

    def _plan_step(parsed: ParsedCommand, verdict: Verdict | None) -> dict[str, Any]:
        return {
            "command": parsed.command,
            "risk": verdict.risk if verdict else (parsed.risk or "unknown"),
            "reason": parsed.reason,
            "timeout": parsed.timeout,
        }

    async def _run_turn(session: Session, message: str, emit: Emit) -> None:
        """一轮完整对话：模型 → 指令 → 校验 → 确认 → 执行 → 回传。"""
        session.running = True
        session.abort_event.clear()
        session.plan_steps = []
        sessions.append_message(session, "user", message)
        await emit({"type": "mode_changed", "data": {"mode": session.mode}})
        audit.log(
            "turn_start",
            session_id=session.session_id,
            mode=session.mode,
            message=message,
            cwd=session.cwd,
        )

        for step in range(cfg.session.max_steps):
            if session.abort_event.is_set():
                await emit({"type": "error", "data": {"message": "已被用户中止", "code": "aborted"}})
                await emit({"type": "done", "data": {"summary": "任务已被用户中止"}})
                audit.log("turn_aborted", session_id=session.session_id, step=step)
                return

            text = await _ask_model(session, emit, message)
            if not text.strip():
                audit.log("empty_model_output", session_id=session.session_id, step=step)
                raise ParseError("模型返回为空", hint="请返回一个包含 action 的 JSON 代码块。")
            sessions.append_message(session, "assistant", text)
            audit.log("model_output", session_id=session.session_id, step=step, model_output=text)

            parsed = await _parse_with_retry(session, text, emit, message)

            # ---- finish ----
            if parsed.action == "finish":
                await emit({"type": "done", "data": {"summary": parsed.summary}})
                audit.log("turn_finished", session_id=session.session_id, summary=parsed.summary)
                return

            # ---- ask_user ----
            if parsed.action == "ask_user":
                await emit({"type": "done", "data": {"summary": parsed.question, "awaiting_user": True}})
                audit.log("ask_user", session_id=session.session_id, question=parsed.question)
                return

            # ---- run_powershell ----
            if not parsed.command:
                raise ParseError("action=run_powershell 但缺少 command")
            command = parsed.command
            verdict = guard.evaluate(
                command,
                mode=session.mode,
                cwd=session.cwd,
                declared_risk=parsed.risk,
            )
            command_id = confirmer.new_command_id()
            session.plan_steps.append(_plan_step(parsed, verdict))
            await emit({"type": "plan", "data": {"steps": list(session.plan_steps)}})
            await emit(
                {
                    "type": "command",
                    "data": {
                        "id": command_id,
                        "command": command,
                        "risk": verdict.risk,
                        "reason": parsed.reason,
                        "cwd": session.cwd,
                        "timeout": parsed.timeout,
                        "require_confirm": bool(verdict.require_confirm and verdict.allowed),
                        "mode": session.mode,
                    },
                }
            )
            audit.log(
                "guard_verdict",
                session_id=session.session_id,
                command_id=command_id,
                command=command,
                declared_risk=parsed.risk,
                risk=verdict.risk,
                allowed=verdict.allowed,
                require_confirm=verdict.require_confirm,
                violations=verdict.violations,
                checked_paths=verdict.checked_paths,
                mode=session.mode,
            )

            if not verdict.allowed:
                reason = verdict.reason
                sessions.add_recent_command(
                    session,
                    {
                        "command": command,
                        "risk": verdict.risk,
                        "status": "denied",
                        "reason": reason,
                        "ts": time.strftime("%H:%M:%S"),
                    },
                )
                await emit(
                    {
                        "type": "error",
                        "data": {
                            "message": f"安全策略拒绝执行：{reason}",
                            "code": "guard_denied",
                            "data": {"id": command_id, "violations": verdict.violations},
                        },
                    }
                )
                sessions.append_message(
                    session,
                    "user",
                    f"【系统回执】命令被安全策略拒绝，未执行。\n命令：{command}\n拒绝原因：{reason}\n"
                    "请改用允许范围内的方案（不要重复提交同一命令），或向用户说明限制并请求指示。",
                )
                continue

            if verdict.require_confirm:
                await emit(
                    {
                        "type": "await_confirm",
                        "data": {
                            "id": command_id,
                            "command": command,
                            "risk": verdict.risk,
                            "reason": parsed.reason,
                            "timeout": cfg.security.confirm_timeout_seconds,
                        },
                    }
                )
                approved = await confirmer.request(
                    session_id=session.session_id,
                    command_id=command_id,
                    command=command,
                    risk=verdict.risk,
                    reason=parsed.reason,
                    abort_event=session.abort_event,
                )
                audit.log(
                    "confirm_result",
                    session_id=session.session_id,
                    command_id=command_id,
                    approved=approved,
                )
                if not approved:
                    aborted = session.abort_event.is_set()
                    sessions.add_recent_command(
                        session,
                        {
                            "command": command,
                            "risk": verdict.risk,
                            "status": "aborted" if aborted else "rejected",
                            "reason": "用户中止" if aborted else "用户未批准或确认超时",
                            "ts": time.strftime("%H:%M:%S"),
                        },
                    )
                    await emit(
                        {
                            "type": "error",
                            "data": {
                                "message": "已被用户中止" if aborted else "命令未获批准，已跳过执行",
                                "code": "aborted" if aborted else "user_rejected",
                                "data": {"id": command_id},
                            },
                        }
                    )
                    sessions.append_message(
                        session,
                        "user",
                        "【系统回执】用户拒绝执行该命令（或确认超时）。不要重复提交同一命令，"
                        "请给出更安全的替代方案，或直接询问用户希望怎么做。",
                    )
                    if aborted:
                        await emit({"type": "done", "data": {"summary": "任务已被用户中止"}})
                        audit.log("turn_aborted", session_id=session.session_id, step=step, stage="confirm")
                        return
                    continue

            # ---- 执行 ----
            session.touch()
            result = await executor.run(
                command,
                cwd=session.cwd,
                timeout=parsed.timeout,
                session_id=session.session_id,
            )
            if result.cwd and Path(result.cwd).is_dir():
                session.cwd = result.cwd

            await emit(
                {
                    "type": "result",
                    "data": {
                        "id": command_id,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.exit_code,
                        "duration_ms": result.duration_ms,
                        "truncated": result.truncated,
                        "timed_out": result.timed_out,
                        "cwd": result.cwd,
                    },
                }
            )
            sessions.add_recent_command(
                session,
                {
                    "command": command,
                    "risk": verdict.risk,
                    "status": "timeout" if result.timed_out else f"exit={result.exit_code}",
                    "duration_ms": result.duration_ms,
                    "ts": time.strftime("%H:%M:%S"),
                },
            )
            audit.log(
                "command_executed",
                session_id=session.session_id,
                command_id=command_id,
                command=command,
                risk=verdict.risk,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                timed_out=result.timed_out,
                truncated=result.truncated,
                stdout_preview=result.stdout,
                stderr_preview=result.stderr,
                cwd=result.cwd,
            )
            sessions.append_message(session, "user", _feedback_message(command, result))

        # 步数耗尽
        await emit(
            {
                "type": "error",
                "data": {
                    "message": f"已达到单轮最大步数 {cfg.session.max_steps}，为避免失控自动停止",
                    "code": "max_steps",
                },
            }
        )
        await emit({"type": "done", "data": {"summary": "步数达到上限，本轮结束"}})
        audit.log("max_steps_reached", session_id=session.session_id)

    def _feedback_message(command: str, result: ExecutionResult) -> str:
        """把执行结果整理成回传给模型的上下文（限制篇幅，避免撑爆上下文）。"""
        stdout = result.stdout[:MODEL_OUTPUT_CONTEXT_CHARS]
        stderr = result.stderr[:1500]
        return (
            "【命令执行结果】\n"
            f"命令：{command}\n"
            f"退出码：{result.exit_code}\n"
            f"耗时：{result.duration_ms}ms\n"
            f"超时：{'是' if result.timed_out else '否'}\n"
            f"输出截断：{'是' if result.truncated else '否'}\n"
            f"当前目录：{result.cwd}\n"
            f"--- stdout ---\n{stdout or '(空)'}\n"
            f"--- stderr ---\n{stderr or '(空)'}\n"
            "请根据结果决定下一步：需要继续则返回下一条 run_powershell，任务完成则返回 finish。"
        )

    # ---------------- 静态托管 ----------------
    web_dir = cfg.web_dir
    if not (web_dir / "index.html").is_file():
        logger.error("未找到前端文件：%s", web_dir / "index.html")
    # 首页：本机访问时自动补上 token 并重定向。
    # 用户经常直接敲 127.0.0.1:8765，如果强制手动粘贴 token 体验太差；
    # 只对本机回环地址生效，外部访问仍然走 token 输入。
    _expected_token = token

    @app.get("/", include_in_schema=False)
    async def index_page(request: Request, token: str | None = None):  # noqa: A002 - 查询参数名固定
        client_host = (request.client.host if request.client else "") or ""
        if client_host in LOCAL_HOSTNAMES and token != _expected_token:
            logger.info("本机访问未带 token，自动重定向到带 token 的地址")
            return RedirectResponse(url=f"/?token={_expected_token}", status_code=307)
        return FileResponse(str(web_dir / "index.html"))

    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    app.state.cfg = cfg
    app.state.token = token
    app.state.audit = audit
    app.state.sessions = sessions
    app.state.llm = llm
    app.state.executor = executor
    app.state.knowledge = kb
    return app


def main() -> None:
    """命令行入口：``python -m server.main``。"""
    cfg = load_config()
    app = create_app(cfg)
    print("=" * 64)
    print(f" EdgeAgent v{VERSION}  ->  http://127.0.0.1:{cfg.server.port}")
    print(f" 访问 token 已写入: {cfg.token_path}")
    print(f" 允许目录: {', '.join(cfg.security.allowed_dirs)}")
    print("=" * 64)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
