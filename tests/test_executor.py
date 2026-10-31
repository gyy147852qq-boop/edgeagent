"""Executor 单元测试：输出捕获、退出码、超时终止、截断、cwd 回传、中止。

仅在 Windows 且存在 powershell.exe 时运行。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from server.config import Config, ExecutorConfig, SecurityConfig
from server.executor import Executor

pytestmark = pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="需要 Windows + powershell.exe",
)


def make_executor(workdir: str, *, max_chars: int = 8000, default_timeout: int = 20) -> Executor:
    cfg = Config(
        executor=ExecutorConfig(
            default_timeout=default_timeout,
            max_timeout=60,
            max_output_chars=max_chars,
            env_allowlist=[
                "SystemRoot",
                "SystemDrive",
                "windir",
                "PATH",
                "PATHEXT",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "COMSPEC",
            ],
        ),
        security=SecurityConfig(allowed_dirs=[workdir]),
    )
    return Executor(cfg)


@pytest.fixture()
def workdir() -> str:
    base = Path(__file__).resolve().parent.parent / ".pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    path = tempfile.mkdtemp(prefix="exec_", dir=str(base))
    yield os.path.realpath(path)
    shutil.rmtree(path, ignore_errors=True)


def test_capture_stdout(workdir: str) -> None:
    executor = make_executor(workdir)
    result = asyncio.run(executor.run("Write-Output 'hello-edgeagent'", cwd=workdir, timeout=30))
    assert "hello-edgeagent" in result.stdout
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.truncated is False


def test_capture_stderr_and_nonzero_exit(workdir: str) -> None:
    executor = make_executor(workdir)
    result = asyncio.run(
        executor.run("Write-Output 'before'; Write-Error 'boom-error'; exit 7", cwd=workdir, timeout=30)
    )
    assert "before" in result.stdout
    assert "boom-error" in result.stderr
    assert result.exit_code == 7


def test_timeout_kills_process_tree(workdir: str) -> None:
    executor = make_executor(workdir)
    started = time.perf_counter()
    result = asyncio.run(executor.run("Start-Sleep -Seconds 15", cwd=workdir, timeout=1))
    elapsed = time.perf_counter() - started
    assert result.timed_out is True
    assert result.exit_code == 124
    assert elapsed < 12, f"超时后应立即返回，实际耗时 {elapsed:.1f}s"


def test_output_truncation(workdir: str) -> None:
    executor = make_executor(workdir, max_chars=200)
    result = asyncio.run(executor.run("Write-Output ('x' * 900)", cwd=workdir, timeout=30))
    assert result.truncated is True
    assert "已截断" in result.stdout


def test_cwd_reported_after_set_location(workdir: str) -> None:
    executor = make_executor(workdir)
    sub = Path(workdir) / "sub"
    sub.mkdir()
    result = asyncio.run(
        executor.run(
            "Set-Location -LiteralPath 'sub'; Get-Location | Select-Object -ExpandProperty Path",
            cwd=workdir,
            timeout=60,
        )
    )
    # 失败时给出可直接定位的信息，避免只有一行 assert
    assert not result.timed_out, result.brief()
    assert os.path.isdir(result.cwd), f"回传 cwd 不存在：{result.cwd!r}，stderr={result.stderr!r}"
    assert os.path.samefile(result.cwd, str(sub)), f"cwd={result.cwd!r} 期望={str(sub)!r}"
    assert os.path.normcase(str(sub)) in os.path.normcase(result.stdout), result.stdout[:300]


def test_missing_cwd_falls_back(workdir: str) -> None:
    executor = make_executor(workdir)
    missing = str(Path(workdir) / "does-not-exist")
    result = asyncio.run(executor.run("Get-Location", cwd=missing, timeout=30))
    assert os.path.isdir(result.cwd)
    assert result.exit_code == 0


def test_utf8_chinese_output(workdir: str) -> None:
    executor = make_executor(workdir)
    result = asyncio.run(executor.run("Write-Output '中文输出测试-EdgeAgent'", cwd=workdir, timeout=30))
    assert "中文输出测试" in result.stdout


def test_kill_running_process(workdir: str) -> None:
    executor = make_executor(workdir)

    async def scenario() -> tuple[bool, float]:
        task = asyncio.create_task(executor.run("Start-Sleep -Seconds 20", cwd=workdir, timeout=30, session_id="s1"))
        await asyncio.sleep(1.5)
        killed = await executor.kill("s1")
        started = time.perf_counter()
        await task
        return killed, time.perf_counter() - started

    killed, waited = asyncio.run(scenario())
    assert killed is True
    assert waited < 10


def test_env_allowlist_filters_secrets(workdir: str) -> None:
    """非白名单环境变量不得透传到子进程。"""
    os.environ["EDGEAGENT_TEST_SECRET"] = "super-secret-value"
    try:
        executor = make_executor(workdir)
        result = asyncio.run(executor.run("Write-Output $env:EDGEAGENT_TEST_SECRET", cwd=workdir, timeout=30))
        assert "super-secret-value" not in result.stdout
    finally:
        os.environ.pop("EDGEAGENT_TEST_SECRET", None)
