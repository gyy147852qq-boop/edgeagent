"""极简 Chrome DevTools Protocol 客户端。

为什么不用 Playwright
---------------------
Playwright 自带 node 内核（解包后上百 MB），打进 exe 会让体积翻几倍，且它的 driver
路径在 PyInstaller 下很容易断。而 CDP 是 Edge/Chrome **原生**协议：只要用
``--remote-debugging-port`` 启动浏览器，就能通过 WebSocket 执行 JS、读 DOM。

所以这里只做两件事：
1. 拉起一个独立的 Edge 实例（独立 profile，不碰用户日常浏览器配置）；
2. 连上它的调试端口，提供 ``eval()`` 这一个核心能力。

依赖只有 ``websockets``（纯 Python，约 200 KB），打包友好。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("edgeagent.cdp")

#: 隐藏子进程窗口
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class CDPError(RuntimeError):
    """浏览器启动或调试协议失败。"""

    def __init__(self, message: str, *, code: str = "cdp_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


#: Edge 常见安装位置（按优先级）
_EDGE_CANDIDATES: tuple[str, ...] = (
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
)


def find_edge() -> str | None:
    """定位 msedge.exe，找不到返回 ``None``。"""
    for raw in _EDGE_CANDIDATES:
        path = os.path.expandvars(raw)
        if os.path.isfile(path):
            return path
    # 兜底：注册表里记录的安装路径
    try:
        import winreg  # type: ignore[import-not-found]

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
            ):
                try:
                    with winreg.OpenKey(hive, key) as handle:
                        value, _ = winreg.QueryValueEx(handle, "")
                        if value and os.path.isfile(value):
                            return value
                except OSError:
                    continue
    except Exception:  # pragma: no cover - 非 Windows
        pass
    return None


def free_port() -> int:
    """拿一个空闲的本地端口给调试协议用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stable_port(profile_dir: str | Path) -> int:
    """由 profile 路径派生一个稳定端口。

    固定端口是"复用已开实例"的前提：同一 profile 第二次启动时能探测到
    上次那个 Edge 还活着，直接连上去，而不是再拉一个（拉起来也会被
    Chromium 转交给已有实例然后自己退出）。
    """
    digest = hashlib.sha256(os.path.abspath(str(profile_dir)).lower().encode("utf-8")).hexdigest()
    return 9300 + int(digest[:4], 16) % 100


def _pid_alive(pid: int) -> bool:
    """用 tasklist 判断进程是否还活着（不依赖第三方库）。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            creationflags=_NO_WINDOW,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def kill_tree(pid: int) -> None:
    """结束整棵进程树（Chromium 是多进程，必须 /T）。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=20,
            creationflags=_NO_WINDOW,
        )
        return
    try:
        os.kill(pid, 9)
    except Exception:
        pass


class EdgeProcess:
    """一个受管 Edge 实例（独立 profile + 稳定调试端口）。

    生命周期要点：
    - 端口由 profile 路径稳定派生，因此**同一 profile 只会有一个实例**；
    - 启动前先探测该端口：已有实例在跑就复用，不再拉起新进程；
    - 启动成功把 PID 写进 profile，下次启动前用它清理自己上次的残留；
    - 退出时 ``taskkill /T`` 杀整棵树，避免 Chromium 子进程变孤儿占着 profile。
    """

    def __init__(self, profile_dir: str | Path, *, headless: bool = False) -> None:
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.port = stable_port(self.profile_dir)
        self._proc: subprocess.Popen[bytes] | None = None
        self._log_path = self.profile_dir.parent / "logs" / "edge.log"

    @property
    def http_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def _pid_file(self) -> Path:
        return self.profile_dir / "edgeagent.pid"

    # ---------------- 探测与清理 ----------------
    def probe(self) -> bool:
        """调试端口是否有 DevTools 服务在响应。"""
        try:
            with httpx.Client(trust_env=False, timeout=1.5) as client:
                return client.get(f"{self.http_base}/json/version").status_code == 200
        except Exception:
            return False

    def _port_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _read_pid(self) -> int | None:
        try:
            return int(self._pid_file.read_text(encoding="utf-8").strip())
        except Exception:
            return None

    def _write_pid(self, pid: int) -> None:
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._pid_file.write_text(str(pid), encoding="utf-8")
        except OSError:
            pass

    def _kill_stale(self) -> None:
        """清理自己上次遗留的 Edge（只认 pid 文件，绝不碰用户其它浏览器窗口）。"""
        pid = self._read_pid()
        if not pid or not _pid_alive(pid):
            return
        logger.info("清理上次遗留的 Edge：PID %s", pid)
        kill_tree(pid)
        time.sleep(1.5)

    def start(self, url: str, *, timeout: float = 40.0) -> None:
        """启动（或复用）Edge，并等待调试端口就绪。"""
        edge = find_edge()
        if not edge:
            raise CDPError(
                "找不到 Microsoft Edge。请确认已安装 Edge。",
                code="edge_missing",
            )

        self.profile_dir.mkdir(parents=True, exist_ok=True)

        # 1) 已有实例在跑：直接复用，不重复拉起
        if self.probe():
            logger.info("复用已在运行的 Edge：%s", self.http_base)
            return

        # 2) 清理自己上次留下的残留进程（常见于异常退出）
        self._kill_stale()

        # 3) 固定端口被别的程序占了就临时换一个
        if not self._port_free(self.port):
            self.port = free_port()

        args = [
            edge,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            # 首次启动会弹「同步确认」之类的引导窗口抢焦点，这些参数把它压掉
            "--no-first-run",
            "--no-default-browser-check",
            "--no-service-autorun",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--metrics-recording-only",
            "--disable-features=msEdgeSyncPromo,msEdgeIdentitySync,Translate,msEdgeSidebarV2",
            # 关键：浏览器会把"后台/被遮挡"的标签页降频，SSE 流和 DOM 更新都会暂停，
            # 表现为回复只出半句就"稳定"了。这三个开关保证标签页始终全速运行。
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            # 注意：不要再加 --disable-blink-features=AutomationControlled
            # 新版 Edge 会提示"不受支持的命令行标志"，反而更容易被识别。
        ]
        if self.headless:
            args.append("--headless=new")
        args.append(url)

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._log_path, "ab")
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - 参数固定，无 shell
                args,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        finally:
            handle.close()

        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.probe():
                self._write_pid(self._proc.pid)
                logger.info("Edge 调试端口就绪：%s", self.http_base)
                return

            exit_code = self._proc.poll()
            if exit_code is not None:
                # 进程退出了：Chromium 可能把命令行转交给了同 profile 的已有实例。
                # 再探一次端口，通了说明复用成功，不必当成错误。
                time.sleep(1.0)
                if self.probe():
                    logger.info("Edge 已把命令转交给同一 profile 的实例，复用成功")
                    self._write_pid(self._proc.pid)
                    return
                raise CDPError(
                    f"Edge 启动后立即退出（退出码 {exit_code}）。该 profile 可能被另一个 Edge 窗口占用，"
                    f"或上次的浏览器没关干净。请关闭那个窗口后重试（日志：{self._log_path}）。",
                    code="edge_exited",
                )
            try:
                with httpx.Client(trust_env=False, timeout=2) as client:
                    client.get(f"{self.http_base}/json/version")
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.3)

        self.stop()
        raise CDPError(
            f"等待 Edge 调试端口超时（{timeout:.0f}s）。{last_error}",
            code="edge_timeout",
        )

    def stop(self) -> None:
        """结束这个 profile 对应的 Edge（自己拉起的或复用的那个）。

        退出方式直接决定**登录态保不保得住**：Chromium 只在正常退出时把内存里的
        Cookie 刷进 SQLite，``taskkill /F`` 会直接掐死进程，登录态就丢了。
        所以这里先给它最多 6 秒自己退（调用方应已发 ``Browser.close``），
        超时才强杀。
        """
        proc = self._proc
        self._proc = None

        pid: int | None = None
        if proc is not None and proc.poll() is None:
            pid = proc.pid
        if pid is None:
            candidate = self._read_pid()
            if candidate and _pid_alive(candidate):
                pid = candidate

        if pid is not None:
            for _ in range(15):  # 6 秒
                if not _pid_alive(pid):
                    logger.info("Edge 已正常退出，登录态已落盘")
                    break
                time.sleep(0.4)
            else:
                logger.warning("Edge 未在 6 秒内退出，强制结束（本次 Cookie 可能来不及落盘）")
                kill_tree(pid)
                time.sleep(0.5)

        try:
            self._pid_file.unlink()
        except OSError:
            pass

    @property
    def alive(self) -> bool:
        """进程在，或端口上有实例（复用的情形）。"""
        if self._proc is not None and self._proc.poll() is None:
            return True
        return self.probe()

    def page_targets(self) -> list[dict[str, Any]]:
        """列出可用的页面 target。"""
        try:
            with httpx.Client(trust_env=False, timeout=5) as client:
                resp = client.get(f"{self.http_base}/json/list")
                data = resp.json()
                return [item for item in data if item.get("type") == "page"]
        except Exception as exc:
            logger.debug("读取 target 列表失败：%s", exc)
            return []

    def new_tab(self, url: str) -> bool:
        """让浏览器新开一个标签页（通过 HTTP 端点，无需 CDP）。"""
        try:
            with httpx.Client(trust_env=False, timeout=10) as client:
                resp = client.put(f"{self.http_base}/json/new?{url}")
                return resp.status_code in {200, 201}
        except Exception as exc:
            logger.debug("新开标签失败：%s", exc)
            return False


class CDPSession:
    """一条到具体页面 target 的 WebSocket 会话。"""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None

    async def connect(self, *, timeout: float = 20.0) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
            raise CDPError(
                "缺少 websockets 依赖，无法连接浏览器调试端口。请执行：pip install websockets",
                code="dependency_missing",
            ) from exc

        self._ws = await asyncio.wait_for(
            websockets.connect(self._ws_url, max_size=32 * 1024 * 1024, ping_interval=20),
            timeout=timeout,
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """持续读取消息，把响应分发给等待中的 future。"""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                message_id = message.get("id")
                if message_id is None:
                    continue  # 事件通知，忽略
                future = self._pending.pop(int(message_id), None)
                if future is not None and not future.done():
                    future.set_result(message)
        except Exception as exc:  # 连接断开
            logger.debug("CDP 读循环结束：%s", exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CDPError("浏览器连接已断开", code="cdp_disconnected"))

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        """发送一条 CDP 命令并等待结果。"""
        if self._ws is None:
            raise CDPError("CDP 会话未连接", code="cdp_not_connected")

        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future

        await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        response = await asyncio.wait_for(future, timeout=timeout)

        error = response.get("error")
        if error:
            raise CDPError(f"{method} 失败：{error.get('message')}", code="cdp_command_failed")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def eval(self, expression: str, *, timeout: float = 30.0) -> Any:
        """在页面里执行一段 JS，返回其值（``returnByValue``）。"""
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        details = result.get("result", {})
        if result.get("exceptionDetails"):
            description = result["exceptionDetails"].get("text") or "JS 执行异常"
            raise CDPError(f"页面脚本出错：{description}", code="js_error")
        return details.get("value")

    async def close(self) -> None:
        """关闭会话与读循环。"""
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
