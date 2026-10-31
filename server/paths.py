"""资源路径 / 数据路径解析（兼容源码运行与 PyInstaller 打包）。

两类路径必须分清，否则打包后必然踩坑：

- **只读资源**（``web/``、``config.example.yaml``、``.env.example``）：
  源码模式在仓库根；PyInstaller 单文件模式下被解压到 ``sys._MEIPASS``。
- **可写数据**（``config.yaml``、``.env``、``logs/``、``sessions/``、``.agent_token``）：
  永远不能写 ``_MEIPASS``（每次启动都是新的临时目录）。优先写在 exe 同级目录，
  若该目录不可写（例如装在 ``Program Files``），退回到 ``%LOCALAPPDATA%\\EdgeAgent``。

对外提供 :func:`get_resource_path`（字符串版，供外部脚本/日志使用）与
:func:`resource_path` / :func:`data_dir`（Path 版，供内部使用）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "EdgeAgent"

_data_dir_cache: Path | None = None


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出来的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """只读资源根目录。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """拼接只读资源路径，例如 ``resource_path("web", "index.html")``。"""
    return bundle_dir().joinpath(*parts)


def get_resource_path(relative: str = "") -> str:
    """字符串版的资源路径解析（兼容 ``_MEIPASS`` 与开发目录）。

    :param relative: 相对资源根的路径，可传空串表示根目录本身
    """
    base = bundle_dir()
    return str(base if not relative else base / relative)


def _is_writable(path: Path) -> bool:
    """探测目录是否可写（写一个探针文件再删掉）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".edgeagent_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """可写数据根目录。

    优先级：``EDGEAGENT_DATA_DIR`` 环境变量 → 源码仓库根 → exe 同级目录 →
    ``%LOCALAPPDATA%\\EdgeAgent``。
    """
    global _data_dir_cache
    if _data_dir_cache is not None:
        return _data_dir_cache

    override = os.getenv("EDGEAGENT_DATA_DIR", "").strip()
    if override:
        candidate = Path(override).expanduser()
        candidate.mkdir(parents=True, exist_ok=True)
        _data_dir_cache = candidate
        return candidate

    if not is_frozen():
        _data_dir_cache = bundle_dir()
        return _data_dir_cache

    exe_dir = Path(sys.executable).resolve().parent
    if _is_writable(exe_dir):
        _data_dir_cache = exe_dir
        return _data_dir_cache

    fallback = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    _data_dir_cache = fallback
    return fallback


def reset_cache() -> None:
    """清空缓存（测试用：切换 ``EDGEAGENT_DATA_DIR`` 后重新解析）。"""
    global _data_dir_cache
    _data_dir_cache = None
