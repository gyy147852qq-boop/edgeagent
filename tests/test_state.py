"""系统状态采集（M10）单元测试：新增字段、缓存节流、磁盘健康。"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from server.config import Config, SecurityConfig
from server.state import StateCollector

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="需要 Windows")


@pytest.fixture()
def cwd() -> str:
    base = Path(__file__).resolve().parent.parent / ".pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    path = tempfile.mkdtemp(prefix="state_", dir=str(base))
    yield os.path.realpath(path)
    shutil.rmtree(path, ignore_errors=True)


def make_collector(workdir: str) -> StateCollector:
    cfg = Config(security=SecurityConfig(allowed_dirs=[workdir]))
    return StateCollector(cfg)


def test_disks_have_free_percent(cwd: str) -> None:
    collector = make_collector(cwd)
    disks = collector.disks()
    assert disks, "至少应枚举出一个盘符"
    for disk in disks:
        assert set(["drive", "total_gb", "free_gb", "used_percent", "free_percent", "low_space"]) <= set(disk)
        assert 0 <= disk["free_percent"] <= 100


def test_disk_usage_for_path(cwd: str) -> None:
    collector = make_collector(cwd)
    usage = collector.disk_usage(cwd)
    assert usage is not None
    assert usage["total_gb"] > 0
    assert 0 <= usage["used_percent"] <= 100


@WINDOWS_ONLY
def test_system_info_contains_m10_fields(cwd: str) -> None:
    collector = make_collector(cwd)
    info = collector.system_info(cwd)
    assert {"disks", "network", "top_processes", "cache_ttl", "cached"} <= set(info)
    assert isinstance(info["disks"], list) and info["disks"]
    assert isinstance(info["network"], list)
    assert set(info["top_processes"]) == {"by_cpu", "by_memory"}
    assert info["cached"] is False


@WINDOWS_ONLY
def test_top_processes_are_populated(cwd: str) -> None:
    collector = make_collector(cwd)
    info = collector.system_info(cwd)
    by_cpu = info["top_processes"]["by_cpu"]
    assert len(by_cpu) >= 1, "应至少采集到一个进程"
    first = by_cpu[0]
    assert "name" in first and "id" in first and "mem_mb" in first


@WINDOWS_ONLY
def test_collection_is_throttled(cwd: str) -> None:
    """5 秒内重复采集必须命中缓存，不再启动 PowerShell。"""
    collector = make_collector(cwd)
    first = collector.system_info(cwd)
    started = time.perf_counter()
    second = collector.system_info(cwd)
    elapsed = time.perf_counter() - started
    assert second["cached"] is True
    assert second["collected_at"] == first["collected_at"]
    assert elapsed < 1.0, f"缓存命中应该很快，实际 {elapsed:.2f}s"


@WINDOWS_ONLY
def test_refresh_bypasses_cache(cwd: str) -> None:
    collector = make_collector(cwd)
    first = collector.system_info(cwd)
    time.sleep(0.05)
    refreshed = collector.system_info(cwd, refresh=True)
    assert refreshed["cached"] is False
    assert refreshed["collected_at"] >= first["collected_at"]
