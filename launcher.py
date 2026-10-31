#!/usr/bin/env python3
"""EdgeAgent 桌面启动器（M11）。

一次启动完成四件事：

1. **自检**：Python 版本、依赖、``config.yaml`` / ``.env``、Edge 浏览器；
2. **引导**：缺配置就从模板生成，缺 API Key 就弹窗让用户填；
3. **服务**：在**同进程内**用后台线程跑 uvicorn（不产生野进程，退出即释放端口）；
4. **外壳**：优先 ``pywebview`` 原生窗口 → ``pystray`` 托盘 + 浏览器 → ``tkinter`` 窗口 → 纯 headless。

打包兼容：只读资源走 ``sys._MEIPASS``，可写数据走 exe 同级目录（见 ``server/paths.py``）；
无控制台时把 stdout/stderr 重定向到 ``logs/app.log``。

命令行：
    python launcher.py                # 正常启动（托盘/窗口 + 浏览器）
    python launcher.py --check        # 只做自检，输出 JSON，退出码 0/1
    python launcher.py --headless     # 只跑服务（自动化验证用）
    python launcher.py --port 8766    # 覆盖端口
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

APP_TITLE = "EdgeAgent"
LAUNCHER_VERSION = "1.0.0"
REQUIRED_MODULES = ("fastapi", "uvicorn", "httpx", "yaml", "dotenv")
OPTIONAL_MODULES = {
    "pypdf": "PDF 知识库",
    "pystray": "系统托盘",
    "PIL": "托盘图标（Pillow）",
    "webview": "内嵌窗口（pywebview）",
}


# ---------------------------------------------------------------------------
# 路径（必须最先可用，后续 import 都依赖它）
# ---------------------------------------------------------------------------
def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(relative: str = "") -> str:
    """兼容 ``sys._MEIPASS`` 与开发目录的资源路径解析（供打包后调用）。"""
    base = _bundle_dir()
    return str(base if not relative else base / relative)


def _default_data_dir() -> Path:
    if not getattr(sys, "frozen", False):
        return _bundle_dir()
    exe_dir = Path(sys.executable).resolve().parent
    try:
        probe = exe_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return exe_dir
    except OSError:
        fallback = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / APP_TITLE
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


# ---------------------------------------------------------------------------
# 日志：无控制台时把 stdout/stderr 写进 logs/app.log
# ---------------------------------------------------------------------------
class _Tee:
    """同时写多个流；任一目标不可用不影响其它目标。"""

    def __init__(self, *streams: Any) -> None:
        self._streams = [s for s in streams if s is not None]

    def write(self, data: str) -> int:
        for stream in self._streams:
            try:
                stream.write(data)
                stream.flush()
            except (OSError, ValueError):
                continue
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            try:
                stream.flush()
            except (OSError, ValueError):
                continue

    def isatty(self) -> bool:
        return False


def _bootstrap_logging(data_dir: Path) -> Path:
    """把标准输出/错误重定向到 logs/app.log，并返回日志文件路径。"""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    handle = open(log_path, "a", encoding="utf-8", buffering=1)
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launcher start (v{LAUNCHER_VERSION}) =====\n")

    # 重定向到管道/文件时统一用 UTF-8，避免 GBK 控制台下中文乱码
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    sys.stdout = _Tee(getattr(sys, "__stdout__", None), handle)  # type: ignore[assignment]
    sys.stderr = _Tee(getattr(sys, "__stderr__", None), handle)  # type: ignore[assignment]

    def _excepthook(exc_type, exc, tb) -> None:
        import traceback

        traceback.print_exception(exc_type, exc, tb)
        try:
            _show_error("EdgeAgent 发生未处理的异常", "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass

    sys.excepthook = _excepthook
    return log_path


# ---------------------------------------------------------------------------
# 弹窗（tkinter 可选）
# ---------------------------------------------------------------------------
def _has_tk() -> bool:
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:
        return False


def _show_error(title: str, message: str) -> None:
    print(f"[ERROR] {title}: {message}")
    if not _has_tk():
        return
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message[:2000])
        root.destroy()
    except Exception:
        pass


def _ask_api_key() -> str:
    """弹窗询问 API Key；用户取消返回空串。"""
    if not _has_tk():
        return ""
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        value = simpledialog.askstring(
            "EdgeAgent 首次配置",
            "请输入 LLM API Key（OpenAI 兼容接口）。\n留空表示稍后在 .env 中手动填写：",
            show="*",
        )
        root.destroy()
        return (value or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 自检与自动部署
# ---------------------------------------------------------------------------
def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def check_dependencies() -> dict[str, Any]:
    missing = [name for name in REQUIRED_MODULES if not _module_available(name)]
    extras = {name: _module_available(name) for name in OPTIONAL_MODULES}
    return {"ok": not missing, "missing": missing, "optional": extras}


def install_dependencies() -> tuple[bool, str]:
    """调用 pip 安装依赖；失败时尝试 ``--user``。"""
    if getattr(sys, "frozen", False):
        return False, "打包版本不应缺依赖，请检查构建配置"
    req = Path(get_resource_path("requirements.txt"))
    if not req.is_file():
        return False, f"未找到 {req}"
    base = [sys.executable, "-m", "pip", "install", "-r", str(req), "--disable-pip-version-check"]
    for extra in ([], ["--user"]):
        cmd = base + extra
        try:
            print(f"[SETUP] 执行 {' '.join(cmd)}")
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"pip 执行失败：{exc}"
        if completed.returncode == 0:
            return True, completed.stdout[-1500:]
        last_error = (completed.stderr or completed.stdout or "")[-1500:]
    return False, last_error


def ensure_config(data_dir: Path) -> dict[str, Any]:
    """config.yaml 不存在时从模板复制一份。"""
    config_path = data_dir / "config.yaml"
    created = False
    if not config_path.is_file():
        example = Path(get_resource_path("config.example.yaml"))
        if example.is_file():
            shutil.copyfile(example, config_path)
            created = True
            print(f"[SETUP] 已生成默认配置：{config_path}")
    return {"exists": config_path.is_file(), "path": str(config_path), "created": created}


def read_api_key(data_dir: Path) -> str:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        return ""
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() == "LLM_API_KEY":
            return value.strip().strip('"').strip("'")
    return ""


def write_api_key(data_dir: Path, api_key: str) -> None:
    env_path = data_dir / ".env"
    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("LLM_API_KEY"):
            lines[index] = f"LLM_API_KEY={api_key}"
            replaced = True
            break
    if not replaced:
        lines.append(f"LLM_API_KEY={api_key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_env_file(data_dir: Path) -> Path:
    env_path = data_dir / ".env"
    if not env_path.is_file():
        example = Path(get_resource_path(".env.example"))
        if example.is_file():
            shutil.copyfile(example, env_path)
        else:
            env_path.write_text("LLM_API_KEY=\n", encoding="utf-8")
    return env_path


def detect_edge() -> dict[str, Any]:
    """检测 Edge 浏览器版本（扩展模式作为备选入口时需要）。"""
    candidates = [
        Path(os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    exe = next((path for path in candidates if path.is_file()), None)
    version = ""
    try:
        import winreg

        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    version = str(winreg.QueryValueEx(key, "pv")[0])
                    break
            except OSError:
                continue
    except ImportError:
        pass
    return {"installed": bool(exe or version), "version": version, "path": str(exe) if exe else ""}


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_health(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# 启动器主体
# ---------------------------------------------------------------------------
class DesktopLauncher:
    """管理内嵌服务与应用外壳的生命周期。"""

    def __init__(self, port: int, data_dir: Path, *, mode: str, simulate_browser: bool = False) -> None:
        self.port = port
        self.data_dir = data_dir
        self.mode = mode
        self.simulate_browser = simulate_browser
        self.app: Any = None
        self.server: Any = None
        self.thread: threading.Thread | None = None
        self.token = ""
        self.browser_opened = False
        self.ready = False
        self.started_at = time.time()
        self._shutdown_lock = threading.Lock()
        self._stopped = False

    # ---------------- 服务 ----------------
    def start_server(self) -> None:
        """在后台线程里跑 uvicorn（同进程，退出即释放端口）。"""
        from server.config import load_config
        from server.main import create_app
        import uvicorn

        cfg = load_config()
        cfg.server.port = self.port
        cfg.server.open_browser = False  # 由启动器决定如何展示 UI

        # 没有密钥时自动降级到内置模型：否则用户点开界面说第一句话就报「未配置密钥」，
        # 首次体验极差。这里只在"真的要调真实接口却没有 key"时降级，配置了 key 时不受影响。
        if not cfg.llm.enabled and not cfg.llm.is_mock:
            print(f"[WARN] 未配置 {cfg.llm.api_key_env}，自动切换为内置 mock 模型（回答为脚本化，仅供体验界面）")
            cfg.llm.provider = "mock"

        self.app = create_app(cfg)
        self.token = getattr(self.app.state, "token", "")

        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="edgeagent-uvicorn", daemon=True)
        self.thread.start()

    def wait_ready(self, timeout: float = 20.0) -> bool:
        return wait_for_health(self.port, timeout)

    @property
    def url(self) -> str:
        base = f"http://127.0.0.1:{self.port}/"
        return f"{base}?token={self.token}" if self.token else base

    def open_ui(self) -> None:
        """打开 Web UI（模拟模式下只记录动作，不真的拉起浏览器）。"""
        if self.simulate_browser:
            print(f"[UI] simulate browser open: http://127.0.0.1:{self.port}/ (token hidden)")
            self.browser_opened = True
            self.write_status()
            return
        try:
            webbrowser.open(self.url)
            self.browser_opened = True
            # 打印真实打开的地址（token 脱敏），否则排障时会误以为没带 token
            shown = f"http://127.0.0.1:{self.port}/?token=***" if self.token else f"http://127.0.0.1:{self.port}/"
            print(f"[UI] browser opened: {shown}")
        except Exception as exc:  # pragma: no cover - 无浏览器环境
            print(f"[WARN] 打开浏览器失败：{exc}")
        self.write_status()

    # ---------------- 状态文件（供 verify.ps1 / 排障读取） ----------------
    def write_status(self, **extra: Any) -> None:
        payload = {
            "pid": os.getpid(),
            "port": self.port,
            "url": f"http://127.0.0.1:{self.port}/",
            "token_present": bool(self.token),
            "browser_opened": self.browser_opened,
            "ready": self.ready,
            "mode": self.mode,
            "frozen": bool(getattr(sys, "frozen", False)),
            "data_dir": str(self.data_dir),
            "started_at": self.started_at,
            "updated_at": time.time(),
            "launcher_version": LAUNCHER_VERSION,
        }
        payload.update(extra)
        target = self.data_dir / "logs" / "launcher_status.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---------------- 退出 ----------------
    def shutdown(self) -> None:
        """停止服务、杀掉所有子进程、释放端口。可重复调用。"""
        with self._shutdown_lock:
            if self._stopped:
                return
            self._stopped = True
        print("[SHUTDOWN] 正在关闭 EdgeAgent ...")
        try:
            if self.server is not None:
                self.server.should_exit = True
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(timeout=8)
        try:
            executor = getattr(self.app.state, "executor", None)
            if executor is not None:
                killed = executor.kill_all_sync()
                if killed:
                    print(f"[SHUTDOWN] 已终止 {killed} 棵残留 PowerShell 进程树")
        except Exception as exc:
            print(f"[WARN] 清理子进程失败：{exc}")
        try:
            # 网页版会拉起独立的 Edge 实例，退出时必须一并关掉，
            # 否则它会一直占着 webprofile，下次启动会因 profile 被占用而失败。
            router = getattr(self.app.state, "llm", None) if self.app is not None else None
            closer = getattr(router, "close_sync", None)
            if closer is not None:
                closer()
                print("[SHUTDOWN] 已关闭网页版浏览器")
        except Exception as exc:
            print(f"[WARN] 关闭浏览器失败：{exc}")
        self.write_status(stopped=True, stopped_at=time.time())
        print("[SHUTDOWN] 完成")

    # ---------------- 外壳 ----------------
    def run(self) -> int:
        self.start_server()
        if not self.wait_ready():
            _show_error("EdgeAgent 启动失败", f"服务未能在 20 秒内就绪，请查看日志：{self.data_dir / 'logs' / 'app.log'}")
            self.shutdown()
            return 3
        self.write_status(ready=True)
        self.ready = True
        print(f"[READY] http://127.0.0.1:{self.port}")

        if self.mode == "headless":
            # headless 默认不打开浏览器；--simulate-browser 时只记录动作（供验证脚本断言）
            if self.simulate_browser:
                self.open_ui()
            try:
                while not self._stopped:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                self.shutdown()
            return 0

        if self.mode == "webview":
            return self._run_webview()
        if self.mode == "tray":
            return self._run_tray()
        if self.mode == "window":
            return self._run_window()
        return self._run_window()

    def _run_webview(self) -> int:
        try:
            import webview
        except Exception:
            print("[WARN] pywebview 不可用，回退到托盘/窗口模式")
            return self._run_tray()

        self.mode = "webview"
        self.write_status(mode="webview")
        webview.create_window(APP_TITLE, self.url, width=1280, height=860, min_size=(960, 640))
        try:
            webview.start()  # 阻塞直到窗口关闭
        finally:
            self.shutdown()
        return 0

    def _run_tray(self) -> int:
        try:
            import pystray  # noqa: F401
            from PIL import Image, ImageDraw  # noqa: F401
        except Exception:
            print("[WARN] pystray/Pillow 不可用，回退到窗口模式")
            return self._run_window()

        import pystray
        from PIL import Image, ImageDraw

        self.mode = "tray"
        self.write_status(mode="tray")

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(47, 109, 246, 255))
        draw.polygon([(22, 20), (42, 32), (22, 44)], fill=(255, 255, 255, 255))
        draw.rectangle((22, 47, 42, 51), fill=(255, 255, 255, 255))

        log_path = self.data_dir / "logs" / "app.log"

        def on_open(icon=None, item=None) -> None:
            self.open_ui()

        def on_logs(icon=None, item=None) -> None:
            try:
                os.startfile(str(log_path))  # type: ignore[attr-defined]
            except Exception as exc:
                print(f"[WARN] 打开日志失败：{exc}")

        def on_quit(icon, item=None) -> None:
            self.shutdown()
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem("打开面板", on_open, default=True),
            pystray.MenuItem("查看日志", on_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_quit),
        )
        icon = pystray.Icon("EdgeAgent", image, f"{APP_TITLE} · 127.0.0.1:{self.port}", menu)
        threading.Timer(0.8, self.open_ui).start()
        tray_started = time.time()
        icon.run()
        # 托盘循环可能"秒退"（无交互式桌面、图标创建被拒等）。此时服务不能跟着退出，
        # 否则用户看到的是页面刚打开就连不上。这里回退到后台常驻，保证界面可用。
        if not self._stopped and time.time() - tray_started < 2.0:
            print("[WARN] 托盘无法常驻（可能缺少交互式桌面），回退到后台常驻模式")
            self.mode = "headless"
            self.write_status(mode="headless")
            return self._run_headless_fallback()
        self.shutdown()
        return 0

    def _run_window(self) -> int:
        self.mode = "window"
        self.write_status(mode="window")
        try:
            import tkinter as tk
        except Exception:
            print("[WARN] tkinter 不可用，退回 headless 模式")
            self.mode = "headless"
            return self._run_headless_fallback()

        root = tk.Tk()
        root.title(f"{APP_TITLE} · 127.0.0.1:{self.port}")
        root.geometry("460x210")
        root.resizable(False, False)

        tk.Label(
            root,
            text="EdgeAgent 已在后台运行",
            font=("Microsoft YaHei UI", 13, "bold"),
            pady=10,
        ).pack()
        tk.Label(
            root,
            text=f"http://127.0.0.1:{self.port}\n关闭窗口即退出并释放端口",
            font=("Microsoft YaHei UI", 9),
            fg="#666666",
        ).pack(pady=(0, 10))

        frame = tk.Frame(root)
        frame.pack(pady=6)

        def on_open() -> None:
            self.open_ui()

        tk.Button(frame, text="打开面板", width=12, command=on_open).grid(row=0, column=0, padx=6)
        tk.Button(
            frame,
            text="查看日志",
            width=12,
            command=lambda: os.startfile(str(self.data_dir / "logs" / "app.log")),  # type: ignore[attr-defined]
        ).grid(row=0, column=1, padx=6)
        tk.Button(frame, text="退出", width=12, command=lambda: root.destroy()).grid(row=0, column=2, padx=6)

        threading.Timer(0.8, self.open_ui).start()
        try:
            root.mainloop()
        finally:
            self.shutdown()
        return 0

    def _run_headless_fallback(self) -> int:
        if not self.browser_opened:  # 托盘分支可能已经打开过，别重复弹标签页
            self.open_ui()
        try:
            while not self._stopped:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0


# ---------------------------------------------------------------------------
# 自检入口
# ---------------------------------------------------------------------------
def run_selfcheck(data_dir: Path, *, interactive: bool, allow_install: bool, port: int) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": False,
        "launcher_version": LAUNCHER_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "data_dir": str(data_dir),
        "resource_dir": get_resource_path(),
        "port": port,
        "steps": [],
    }

    def note(name: str, ok: bool, detail: Any = "") -> None:
        report["steps"].append({"name": name, "ok": ok, "detail": detail})

    note("Python >= 3.11", report["python_ok"], report["python"])
    if not report["python_ok"]:
        report["error"] = "需要 Python 3.11 及以上版本"
        return report

    deps = check_dependencies()
    if not deps["ok"] and allow_install and not getattr(sys, "frozen", False):
        # 打包后的 exe 内没有可用的 pip，自动安装只会产出误导性的报错，
        # 直接给出明确提示让用户去源码目录装依赖或重新打包。
        print(f"[SETUP] 缺少依赖 {deps['missing']}，尝试自动安装 ...")
        installed, output = install_dependencies()
        deps = check_dependencies()
        note("依赖自动安装", installed and deps["ok"], output[-300:] if output else "")
    report["dependencies"] = deps
    note("依赖齐全", deps["ok"], {"missing": deps["missing"], "optional": deps["optional"]})
    if not deps["ok"]:
        hint = (
            "缺少依赖："
            + ", ".join(deps["missing"])
            + "。当前为打包版 exe，无法自动安装，请到源码目录执行 "
            "pip install -r requirements.txt 后重新执行 python build.py 打包。"
            if getattr(sys, "frozen", False)
            else f"缺少依赖：{', '.join(deps['missing'])}，请执行 pip install -r requirements.txt"
        )
        report["error"] = hint
        return report

    config = ensure_config(data_dir)
    report["config"] = config
    note("config.yaml 就绪", config["exists"], config["path"])

    ensure_env_file(data_dir)
    api_key = read_api_key(data_dir)
    if not api_key and interactive:
        entered = _ask_api_key()
        if entered:
            write_api_key(data_dir, entered)
            api_key = entered
    report["api_key_configured"] = bool(api_key)
    note("LLM API Key", bool(api_key), "已配置" if api_key else "未配置（可用 LLM_PROVIDER=mock 体验）")

    edge = detect_edge()
    report["edge"] = edge
    note("Edge 浏览器", edge["installed"], edge["version"] or edge["path"] or "未检测到（不影响客户端模式）")

    busy = port_in_use(port)
    report["port_in_use"] = busy
    note(f"端口 {port}", not busy, "占用中" if busy else "空闲")

    report["ok"] = bool(
        report["python_ok"] and deps["ok"] and config["exists"] and not busy
    )
    if not report["ok"] and "error" not in report:
        report["error"] = "端口被占用" if busy else "自检未通过"
    return report


def _pick_mode(args: argparse.Namespace) -> str:
    if args.headless:
        return "headless"
    if args.mode:
        return args.mode
    if _module_available("webview"):
        return "webview"
    if _module_available("pystray") and _module_available("PIL"):
        return "tray"
    if _has_tk():
        return "window"
    return "headless"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EdgeAgent 桌面启动器")
    parser.add_argument("--check", action="store_true", help="只做自检并输出 JSON")
    parser.add_argument("--headless", action="store_true", help="只启动服务，不创建窗口/托盘")
    parser.add_argument("--mode", choices=["webview", "tray", "window", "headless"], help="强制指定外壳模式")
    parser.add_argument("--port", type=int, default=0, help="覆盖端口（默认读 config.yaml）")
    parser.add_argument("--data-dir", default="", help="覆盖可写数据目录")
    parser.add_argument("--no-install", action="store_true", help="缺少依赖时不自动 pip install")
    parser.add_argument("--simulate-browser", action="store_true", help="验证用：记录打开浏览器的动作但不真的打开")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    data_dir = Path(args.data_dir).resolve() if args.data_dir else _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    if args.data_dir:
        os.environ["EDGEAGENT_DATA_DIR"] = str(data_dir)

    log_path = _bootstrap_logging(data_dir)
    print(f"[BOOT] data_dir={data_dir} log={log_path}")

    port = args.port or int(os.getenv("PORT", "8765"))
    report = run_selfcheck(
        data_dir,
        interactive=not args.check and not args.headless,
        allow_install=not args.no_install,
        port=port,
    )

    if args.check:
        # 用标记包裹 JSON：正常日志也会打到 stdout，验证脚本只取标记之间的内容
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        print("__EDGEAGENT_CHECK_BEGIN__")
        print(payload)
        print("__EDGEAGENT_CHECK_END__")
        # windowed exe 没有控制台，额外落一份文件供打包后验证
        try:
            report_path = data_dir / "logs" / "check_report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(payload, encoding="utf-8")
        except OSError:
            pass
        return 0 if report["ok"] else 1

    # 端口已被占用：可能是本程序已经在跑（双击两次），直接把面板打开
    # 注意：run_selfcheck 在依赖缺失等早退路径下不会写入 port_in_use，这里必须用 .get()
    if report.get("port_in_use"):
        if wait_for_health(port, timeout=2.0):
            # 已有实例的 token 在 data_dir/.agent_token，带上它用户就不用再手输
            url = f"http://127.0.0.1:{port}/"
            token_file = data_dir / ".agent_token"
            if token_file.is_file():
                try:
                    token = token_file.read_text(encoding="utf-8").strip()
                    if token:
                        url = f"{url}?token={token}"
                except OSError:
                    pass
            print(f"[BOOT] 检测到已有实例在运行：http://127.0.0.1:{port}/")
            if not args.simulate_browser:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            return 0
        _show_error(
            "端口被占用",
            f"端口 {port} 已被其它程序占用，请关闭占用进程或修改 config.yaml 的 server.port。\n\n日志：{log_path}",
        )
        return 2

    if not report["ok"]:
        _show_error("EdgeAgent 自检未通过", report.get("error", "未知错误") + f"\n\n日志：{log_path}")
        return 2

    mode = _pick_mode(args)
    launcher = DesktopLauncher(port, data_dir, mode=mode, simulate_browser=args.simulate_browser)
    atexit.register(launcher.shutdown)
    try:
        return launcher.run()
    except KeyboardInterrupt:
        return 0
    finally:
        launcher.shutdown()


if __name__ == "__main__":
    sys.exit(main())
