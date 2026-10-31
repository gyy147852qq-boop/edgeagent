"""系统状态采集器（M10 增强版）。

新增能力：
- **进程 Top 5**：按 CPU 时间与工作集合（内存）各取前 5（一次 PowerShell 采集，避免多次启动）；
- **磁盘健康**：枚举所有盘符的容量与剩余百分比；
- **网络状态**：有默认网关的网卡、IPv4 与网关地址；
- **采集节流**：5 秒内重复调用直接命中缓存，避免高频刷新拖垮机器（纯只读，无需人工确认）。

不引入 psutil 等原生依赖：CPU/内存走 ``ctypes`` 调 kernel32，磁盘走 ``shutil``，
进程与网卡这类无法用 ctypes 廉价获取的信息，用一次 ``powershell.exe -NoProfile`` 采集并缓存。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from typing import Any

from .config import Config
from .session import Session

logger = logging.getLogger("edgeagent.state")

CREATE_NO_WINDOW = 0x08000000
PROBE_TIMEOUT_SECONDS = 12

#: 一次采集进程与网卡；输出单个 JSON，减少进程启动开销
PROBE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$byCpu = @(Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 5 | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        id = $_.Id
        cpu_seconds = if ($null -ne $_.CPU) { [math]::Round([double]$_.CPU, 1) } else { 0 }
        mem_mb = [math]::Round([double]$_.WorkingSet64 / 1MB, 1)
    }
})
$byMem = @(Get-Process | Sort-Object -Property WorkingSet64 -Descending | Select-Object -First 5 | ForEach-Object {
    [pscustomobject]@{
        name = $_.Name
        id = $_.Id
        mem_mb = [math]::Round([double]$_.WorkingSet64 / 1MB, 1)
    }
})
$net = @(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | ForEach-Object {
    [pscustomobject]@{
        interface = $_.InterfaceAlias
        ipv4 = @($_.IPv4Address)[0].IPAddress
        gateway = @($_.IPv4DefaultGateway)[0].NextHop
    }
})
[pscustomobject]@{ by_cpu = $byCpu; by_mem = $byMem; network = $net } | ConvertTo-Json -Depth 6 -Compress
"""


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _filetime_to_int(value: _FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class StateCollector:
    """采集前端右侧面板与提示词需要的系统状态。"""

    #: 采集节流窗口：5 秒内重复调用直接返回缓存
    CACHE_TTL = 5.0

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._last_cpu_sample: tuple[int, int, float] | None = None
        self._last_cpu_percent: float | None = None
        self._info_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._probe_cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    # ---------------- 系统资源 ----------------
    def cpu_usage(self) -> float | None:
        """两次采样之间的整机 CPU 占用率（百分比，首次调用返回 None）。"""
        if os.name != "nt":
            return None
        try:
            idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            total = _filetime_to_int(kernel) + _filetime_to_int(user)
            idle_total = _filetime_to_int(idle)
            now = time.time()
            if self._last_cpu_sample is None:
                self._last_cpu_sample = (total, idle_total, now)
                return None
            prev_total, prev_idle, _ = self._last_cpu_sample
            self._last_cpu_sample = (total, idle_total, now)
            delta_total = total - prev_total
            delta_idle = idle_total - prev_idle
            if delta_total <= 0:
                return self._last_cpu_percent
            percent = (1 - delta_idle / delta_total) * 100
            self._last_cpu_percent = round(max(0.0, min(100.0, percent)), 1)
            return self._last_cpu_percent
        except (AttributeError, OSError):
            return None

    def memory_usage(self) -> float | None:
        """整机内存占用率（百分比）。"""
        if os.name != "nt":
            return None
        try:
            status = _MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return float(status.dwMemoryLoad)
        except (AttributeError, OSError):
            return None

    def disk_usage(self, path: str) -> dict[str, Any] | None:
        """指定路径所在卷的容量信息。"""
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        total_gb = round(usage.total / (1024**3), 1)
        free_gb = round(usage.free / (1024**3), 1)
        percent = round((usage.used / usage.total) * 100, 1) if usage.total else None
        return {"path": path, "total_gb": total_gb, "free_gb": free_gb, "used_percent": percent}

    def disks(self) -> list[dict[str, Any]]:
        """枚举所有可见盘符的容量与剩余百分比（磁盘健康视图）。"""
        result: list[dict[str, Any]] = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            if not usage.total:
                continue
            free_percent = round(usage.free / usage.total * 100, 1)
            result.append(
                {
                    "drive": f"{letter}:",
                    "total_gb": round(usage.total / (1024**3), 1),
                    "free_gb": round(usage.free / (1024**3), 1),
                    "used_percent": round(usage.used / usage.total * 100, 1),
                    "free_percent": free_percent,
                    # 低于 10% 视为需要注意
                    "low_space": free_percent < 10.0,
                }
            )
        return result

    # ---------------- 进程 / 网络（PowerShell 一次采集 + 缓存） ----------------
    def _run_probe(self) -> dict[str, Any]:
        """执行一次 PowerShell 采集，返回 {by_cpu, by_mem, network}。"""
        empty: dict[str, Any] = {"by_cpu": [], "by_mem": [], "network": []}
        if os.name != "nt":
            return empty
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    PROBE_SCRIPT,
                ],
                capture_output=True,
                timeout=PROBE_TIMEOUT_SECONDS,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("进程/网络采集失败：%s", exc)
            return empty

        text = self._decode(completed.stdout).strip()
        if not text:
            return empty
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("采集输出无法解析：%s", text[:200])
            return empty
        if not isinstance(data, dict):
            return empty

        def _as_list(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [value]
            return []

        return {
            "by_cpu": _as_list(data.get("by_cpu")),
            "by_mem": _as_list(data.get("by_mem")),
            "network": _as_list(data.get("network")),
        }

    def process_and_network(self, *, refresh: bool = False) -> dict[str, Any]:
        """带 TTL 缓存的进程/网络快照。"""
        now = time.time()
        with self._lock:
            cached = self._probe_cache
            if not refresh and cached is not None and (now - cached[0]) < self.CACHE_TTL:
                return cached[1]
        data = self._run_probe()
        with self._lock:
            self._probe_cache = (time.time(), data)
        return data

    @staticmethod
    def _decode(data: bytes | None) -> str:
        if not data:
            return ""
        for encoding in ("utf-8-sig", "gbk"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    # ---------------- 汇总 ----------------
    def system_info(self, cwd: str | None = None, *, refresh: bool = False) -> dict[str, Any]:
        """汇总系统信息（5 秒内重复调用命中缓存）。"""
        key = cwd or ""
        now = time.time()
        if not refresh:
            cached = self._info_cache.get(key)
            if cached is not None and (now - cached[0]) < self.CACHE_TTL:
                info = dict(cached[1])
                info["cached"] = True
                return info

        target = cwd if cwd and os.path.isdir(cwd) else self._cfg.default_cwd()
        probe = self.process_and_network(refresh=refresh)
        info: dict[str, Any] = {
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cpu": self.cpu_usage(),
            "cpu_count": os.cpu_count(),
            "mem": self.memory_usage(),
            "disk": self.disk_usage(target),
            "disks": self.disks(),
            "top_processes": {"by_cpu": probe.get("by_cpu", []), "by_memory": probe.get("by_mem", [])},
            "network": probe.get("network", []),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "collected_at": time.time(),
            "cache_ttl": self.CACHE_TTL,
            "cached": False,
        }
        self._info_cache[key] = (now, info)
        return info

    # ---------------- 会话快照 ----------------
    def snapshot(
        self,
        session: Session,
        *,
        pending: list[dict[str, Any]] | None = None,
        running: bool = False,
    ) -> dict[str, Any]:
        """``GET /api/state`` 的响应体。"""
        return {
            "cwd": session.cwd,
            "mode": session.mode,
            "allowed_dirs": list(self._cfg.security.allowed_dirs),
            "recent_commands": session.recent_commands[-10:],
            "pending_confirms": pending or [],
            "running": running,
            "system": self.system_info(session.cwd),
        }
