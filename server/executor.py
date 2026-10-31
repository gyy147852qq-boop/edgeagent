"""PowerShell 执行器。

安全与健壮性要点：
- 只用 ``asyncio.create_subprocess_exec``（参数列表，**不用 shell=True**），杜绝命令注入；
- 固定 ``-NoProfile -NonInteractive -ExecutionPolicy Bypass``，避免用户 profile 干扰；
- 独立进程组 + ``taskkill /T``，超时可终止整棵进程树；
- 环境变量按白名单传递，密钥类变量绝不透传；
- 单流输出超过阈值截断并标记 ``truncated``；
- 通过哨兵行回传执行后的工作目录与 ``$LASTEXITCODE``（哨兵行会从 stdout 中剔除）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .config import Config

logger = logging.getLogger("edgeagent.executor")

CREATE_NEW_PROCESS_GROUP: Final[int] = 0x00000200
CWD_MARK: Final[str] = "__EDGEAGENT_CWD__"
EXIT_MARK: Final[str] = "__EDGEAGENT_EXIT__"
POWERSHELL: Final[str] = "powershell.exe"
KILL_GRACE_SECONDS: Final[float] = 5.0


@dataclass
class ExecutionResult:
    """一次命令执行的结果。"""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    truncated: bool = False
    timed_out: bool = False
    cwd: str = ""

    @property
    def ok(self) -> bool:
        return not self.timed_out and (self.exit_code in (0, None))

    def brief(self, limit: int = 600) -> str:
        """给模型/审计用的紧凑摘要。"""
        text = (self.stdout or "").strip()
        if self.stderr.strip():
            text = f"{text}\n[stderr] {self.stderr.strip()}".strip()
        if len(text) > limit:
            text = text[:limit] + " ...[已截断]"
        status = "timeout" if self.timed_out else f"exit={self.exit_code}"
        return f"{status}\n{text}"


class Executor:
    """管理 PowerShell 子进程的生命周期。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    # ---------------- 对外接口 ----------------
    def clamp_timeout(self, timeout: int | None) -> int:
        """把超时收敛到 [1, max_timeout]。"""
        raw = timeout if isinstance(timeout, int) and timeout > 0 else self._cfg.executor.default_timeout
        return max(1, min(raw, self._cfg.executor.max_timeout))

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: int | None = None,
        session_id: str = "default",
    ) -> ExecutionResult:
        """执行一条 PowerShell 命令并返回结果（不抛异常，错误体现在结果里）。"""
        timeout_s = self.clamp_timeout(timeout)
        workdir = self._resolve_cwd(cwd)
        script = self._wrap_script(command)
        args = [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]
        env = self._build_env()

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=workdir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=CREATE_NEW_PROCESS_GROUP,
            )
        except (FileNotFoundError, OSError) as exc:
            logger.error("启动 PowerShell 失败: %s", exc)
            return ExecutionResult(
                stderr=f"无法启动 PowerShell：{exc}",
                exit_code=-1,
                duration_ms=int((time.perf_counter() - started) * 1000),
                cwd=workdir,
            )

        self._procs[session_id] = proc
        timed_out = False
        try:
            try:
                out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                logger.warning("命令超时（%ss），终止进程树 pid=%s", timeout_s, proc.pid)
                await self.kill(session_id)
                try:
                    out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=KILL_GRACE_SECONDS)
                except (asyncio.TimeoutError, ProcessLookupError):
                    out_b, err_b = b"", b""
        finally:
            self._procs.pop(session_id, None)

        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout_text = self._decode(out_b)
        stderr_text = self._decode(err_b)

        stdout_text, new_cwd, marker_exit = self._split_markers(stdout_text, workdir)
        exit_code = marker_exit if marker_exit is not None else proc.returncode

        truncated = False
        stdout_text, cut_out = self._truncate(stdout_text)
        stderr_text, cut_err = self._truncate(stderr_text)
        truncated = cut_out or cut_err

        if timed_out:
            stderr_text = (stderr_text + f"\n[EdgeAgent] 命令执行超过 {timeout_s}s，已被强制终止").strip()
            exit_code = 124

        return ExecutionResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
            duration_ms=duration_ms,
            truncated=truncated,
            timed_out=timed_out,
            cwd=new_cwd,
        )

    async def kill(self, session_id: str) -> bool:
        """终止指定会话正在执行的进程树。返回是否找到运行中的进程。"""
        proc = self._procs.get(session_id)
        if proc is None or proc.returncode is not None:
            return False
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("taskkill 失败: %s", exc)
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        return True

    def is_running(self, session_id: str) -> bool:
        proc = self._procs.get(session_id)
        return proc is not None and proc.returncode is None

    def kill_all_sync(self) -> int:
        """同步终止所有仍在运行的子进程树，返回终止数量。

        GUI 客户端退出时调用：即使事件循环已经停止，也要保证没有残留的
        ``powershell.exe`` 占着文件和端口。
        """
        killed = 0
        for session_id, proc in list(self._procs.items()):
            self._procs.pop(session_id, None)
            if proc.returncode is not None:
                continue
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                if proc.returncode is None:
                    proc.kill()
                killed += 1
            except (ProcessLookupError, OSError):
                pass
        return killed

    # ---------------- 内部实现 ----------------
    def _resolve_cwd(self, cwd: str | None) -> str:
        """确保 cwd 真实存在，否则回落到允许目录 / 项目根。"""
        candidates = [cwd] if cwd else []
        candidates.extend(self._cfg.security.allowed_dirs)
        candidates.append(str(self._cfg.base_dir))
        for cand in candidates:
            if cand and Path(cand).is_dir():
                return os.path.realpath(cand)
        return str(Path(self._cfg.base_dir))

    @staticmethod
    def _wrap_script(command: str) -> str:
        """包一层外壳注入哨兵，保证能取回执行后的 cwd 与退出码。"""
        return (
            "$ErrorActionPreference='Continue'\n"
            "$ProgressPreference='SilentlyContinue'\n"
            "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}\n"
            "try {\n"
            "& {\n"
            f"{command}\n"
            "}\n"
            "} finally {\n"
            f"try {{ $__eaLoc = (Get-Location).ProviderPath }} catch {{ $__eaLoc = $PWD.Path }}\n"
            f"Write-Output ('{CWD_MARK}' + $__eaLoc)\n"
            f"Write-Output ('{EXIT_MARK}' + $LASTEXITCODE)\n"
            "}\n"
        )

    def _build_env(self) -> dict[str, str]:
        """按白名单挑选环境变量，其余（含密钥）一律不传。"""
        allow = {name.lower() for name in self._cfg.executor.env_allowlist}
        env: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.lower() in allow:
                env[key] = value
        env.setdefault("SystemRoot", os.environ.get("SystemRoot", r"C:\Windows"))
        env.setdefault("windir", env["SystemRoot"])
        env.setdefault("PATHEXT", os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"))
        env.setdefault("PATH", os.environ.get("PATH", r"C:\Windows\System32;C:\Windows"))
        env.setdefault("TEMP", os.environ.get("TEMP", r"C:\Windows\Temp"))
        env.setdefault("TMP", env["TEMP"])
        env["POWERSHELL_TELEMETRY_OPTOUT"] = "1"
        return env

    @staticmethod
    def _decode(data: bytes | None) -> str:
        """优先 UTF-8，失败回落 GBK（中文 Windows 默认代码页），最后 replace。"""
        if not data:
            return ""
        for encoding in ("utf-8", "gbk"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    def _truncate(self, text: str) -> tuple[str, bool]:
        limit = self._cfg.executor.max_output_chars
        if len(text) <= limit:
            return text, False
        return text[:limit] + f"\n...[输出超过 {limit} 字符已截断]", True

    @staticmethod
    def _split_markers(text: str, fallback_cwd: str) -> tuple[str, str, int | None]:
        """剔除哨兵行，返回 (清洗后 stdout, 新 cwd, $LASTEXITCODE)。"""
        cwd = fallback_cwd
        exit_code: int | None = None
        kept: list[str] = []
        for line in text.splitlines():
            if line.startswith(CWD_MARK):
                value = line[len(CWD_MARK) :].strip()
                if value:
                    cwd = value
            elif line.startswith(EXIT_MARK):
                value = line[len(EXIT_MARK) :].strip()
                if value:
                    try:
                        exit_code = int(value)
                    except ValueError:
                        pass
            else:
                kept.append(line)
        return "\n".join(kept).strip("\n"), cwd, exit_code
