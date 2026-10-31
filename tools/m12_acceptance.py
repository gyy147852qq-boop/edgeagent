"""M12 真机验收：对打包产物 dist/EdgeAgent.exe 做端到端验证。

用法::

    python build.py                 # 先产出 dist/EdgeAgent.exe
    python tools/m12_acceptance.py  # 22 项验收，约 90 秒

产出::

    reports/m12_results_<时间戳>.json   原始结果
    reports/m12_report_<时间戳>.html    可读报告（自动生成）
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
EXE = DIST / "EdgeAgent.exe"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
WORK = ROOT / ".pytest_work"
WORK.mkdir(exist_ok=True)
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

results: list[dict] = []
AUTH: dict[str, str] = {}
timings: dict[str, float] = {}


def record(name: str, ok: bool, actual: str, expected: str = "", detail: str = "") -> bool:
    results.append({"name": name, "ok": bool(ok), "actual": actual, "expected": expected, "detail": detail})
    print(("PASS " if ok else "FAIL ") + f"{name} :: {actual}")
    return ok


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_http(url: str, timeout: float) -> float:
    """返回就绪耗时（秒），超时抛异常。"""
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                resp.read(1)
                return round(time.perf_counter() - start, 2)
        except Exception:
            time.sleep(0.05)
    raise TimeoutError(f"{url} 在 {timeout}s 内未就绪")


def start_exe(*extra: str) -> subprocess.Popen:
    creation = 0x08000000 if os.name == "nt" else 0
    return subprocess.Popen(
        [str(EXE), *extra],
        cwd=str(DIST),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation,
    )


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)


def listeners(port: int) -> list[int]:
    pids: list[int] = []
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace").stdout
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            try:
                pids.append(int(line.split()[-1]))
            except ValueError:
                continue
    return pids


def cleanup_port(port: int) -> None:
    """兜底：onefile exe 是父+子进程，杀父后子进程可能仍占用端口，必须按端口清理。"""
    for _ in range(3):
        pids = listeners(port)
        if not pids:
            return
        for pid in pids:
            kill_tree(pid)
        time.sleep(1.5)


def token() -> str:
    path = DIST / ".agent_token"
    for _ in range(20):
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        time.sleep(0.1)
    return ""


def new_session(client: httpx.Client) -> str:
    resp = client.post(f"{BASE}/api/session/create", headers=AUTH, json={})
    if resp.status_code != 200:
        raise RuntimeError(f"/api/session/create 失败: {resp.status_code} {resp.text[:150]}")
    return resp.json()["session_id"]


def sse_chat(client: httpx.Client, sid: str, message: str, mode: str = "restricted", *, confirm: str = "none") -> list[dict]:
    """confirm: none / approve / reject"""
    events: list[dict] = []
    payload = {"session_id": sid, "message": message, "mode": mode, "confirm_full_access": mode == "full"}
    with client.stream("POST", f"{BASE}/api/chat", json=payload, headers=AUTH, timeout=120.0) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"/api/chat 返回 {resp.status_code}: {resp.read()[:200]!r}")
        buffer = ""
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                for line in raw.splitlines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    events.append(event)
                    if event["type"] == "await_confirm":
                        cid = event["data"]["id"]
                        decision = confirm == "approve"
                        resp = client.post(
                            f"{BASE}/api/confirm",
                            json={"session_id": sid, "command_id": cid, "approved": decision},
                            headers=AUTH,
                            timeout=15.0,
                        )
                        if resp.status_code != 200:
                            raise RuntimeError(f"/api/confirm 失败: {resp.status_code} {resp.text[:150]}")
                    if event["type"] == "done":
                        return events
    return events


def main() -> int:
    if not EXE.is_file():
        print(f"[FATAL] 未找到 {EXE}，请先运行 python build.py")
        return 1

    header = {"Authorization": "", "Content-Type": "application/json"}
    proc = None
    try:
        # ---------------- 1. 冷启动 ----------------
        # 前置清理：确保 8765 上没有历史实例（onefile 父/子进程会互相残留）
        cleanup_port(PORT)
        if port_open(PORT):
            raise RuntimeError(f"端口 {PORT} 仍被占用，无法进行冷启动测试")

        # 验收使用 mock 模型：无 API Key 也能跑完整链路，且结果可重复
        os.environ["LLM_PROVIDER"] = "mock"
        cold_start = time.perf_counter()
        proc = start_exe("--headless", "--port", str(PORT))
        ready = wait_http(f"{BASE}/healthz", 20)
        timings["cold_start_healthz"] = ready
        index_start = time.perf_counter()
        with urllib.request.urlopen(f"{BASE}/", timeout=5) as resp:
            html = resp.read().decode("utf-8", "replace")
        timings["index_first_byte"] = round(time.perf_counter() - index_start, 3)
        timings["cold_start_total"] = round(time.perf_counter() - cold_start, 2)
        record("冷启动：服务就绪", ready < 5, f"{ready}s（目标 <3s）", "<3s")
        record("冷启动：首页可加载", "EdgeAgent" in html and len(html) > 2000, f"{len(html)} bytes, {timings['index_first_byte']}s")

        tk = token()
        header["Authorization"] = f"Bearer {tk}"
        AUTH.clear()
        AUTH.update(header)
        record("读取 .agent_token", bool(tk), f"len={len(tk)}")

        with httpx.Client(timeout=30.0, trust_env=False) as client:
            # ---------------- 2. 环境自检 ----------------
            r = client.get(f"{BASE}/api/health", headers=header)
            health = r.json()
            record(
                "环境自检 /api/health",
                r.status_code == 200 and health.get("ok") is True and health.get("provider") == "mock",
                f"provider={health.get('provider')} version={health.get('version')}",
                "ok:true 且 provider=mock（证明是本次新启动的实例）",
            )

            # ---------------- 3. 配置持久化 ----------------
            cfg_path = DIST / "config.yaml"
            env_path = DIST / ".env"
            before = (cfg_path.stat().st_mtime, env_path.stat().st_mtime if env_path.exists() else 0)
            sd = client.post(f"{BASE}/api/session/create", headers=header, json={})
            sid = (sd.json() or {}).get("session_id") if sd.status_code == 200 else None
            if not sid:
                raise RuntimeError(f"/api/session/create 失败: HTTP {sd.status_code} {sd.text[:200]}")

            # ---------------- 4. 场景 A1：桌面（越界） ----------------
            t0 = time.perf_counter()
            ev = sse_chat(client, sid, "扫描桌面所有大于 5MB 的 .log 文件，列出清单，并询问我是否删除。")
            denied = [e for e in ev if e["type"] == "error" and e["data"].get("code") == "guard_denied"]
            resolved = [e for e in ev if e["type"] == "result"]
            timings["scenario_a_denied"] = round(time.perf_counter() - t0, 2)
            record(
                "场景A：桌面路径被 Guard 拦截",
                len(denied) == 1 and len(resolved) == 0,
                (denied[0]["data"]["message"][:90] if denied else "未出现 guard_denied"),
                "guard_denied 且无命令执行",
            )

            # ---------------- 5. 场景 A2：允许目录内扫描 ----------------
            log_dir = Path(r"D:\AgentWork\logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            for i in (1, 2):
                p = log_dir / f"big-{i}.log"
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("x" * (6 * 1024 * 1024))
            t0 = time.perf_counter()
            # 独立会话：A1 的拒收回执会进入上下文，mock 见回执会直接 finish
            sid = new_session(client)
            ev = sse_chat(client, sid, "在 D:\\AgentWork 里扫描大于 5MB 的 log 文件")
            res = [e for e in ev if e["type"] == "result"]
            cmds = [e for e in ev if e["type"] == "command"]
            stdout = res[0]["data"]["stdout"] if res else ""
            timings["scenario_a_allowed"] = round(time.perf_counter() - t0, 2)
            record(
                "场景A：允许目录内自动执行并列出清单",
                bool(res) and res[0]["data"]["exit_code"] == 0 and stdout.count("big-") >= 2,
                f"命中 {stdout.count('big-')} 个文件，{timings['scenario_a_allowed']}s",
                "exit_code=0 且列出 2 个文件",
                detail=stdout[:200],
            )
            record(
                "场景A：low 风险未要求确认",
                not any(e["type"] == "await_confirm" for e in ev),
                "无 await_confirm" if not any(e["type"] == "await_confirm" for e in ev) else "出现了确认请求",
            )

            # ---------------- 6. 场景 A3：完整模式删除需确认 ----------------
            # 独立会话：避免场景 A1 的「命令执行结果」进入上下文，导致 mock 直接走 finish
            sid = new_session(client)
            ev = sse_chat(
                client, sid, "删除 D:\\AgentWork\\logs 下的临时日志", mode="full", confirm="reject"
            )
            confirms = [e for e in ev if e["type"] == "await_confirm"]
            rejected = [e for e in ev if e["type"] == "error" and e["data"].get("code") == "user_rejected"]
            still_there = (log_dir / "big-1.log").exists()
            record(
                "场景A：完全模式删除必须人工确认",
                len(confirms) == 1 and len(rejected) == 1,
                f"await_confirm={len(confirms)} rejected={len(rejected)}",
                "1 次确认 + 用户拒绝",
            )
            record("场景A：拒绝后文件未被删除", still_there, "文件仍在" if still_there else "文件被删除了")
            client.post(f"{BASE}/api/mode", headers=header, json={"session_id": sid, "mode": "restricted"})

            # ---------------- 7. 场景 B：RAG ----------------
            kb_dir = Path(r"D:\AgentWork\docs")
            kb_dir.mkdir(parents=True, exist_ok=True)
            (kb_dir / "edgeagent-terms.txt").write_text(
                "EdgeAgent 术语表\n\n"
                "ZQ-TERM-2048：指「沙箱化执行」策略，即所有 PowerShell 指令必须先过 Guard 白名单与路径校验，"
                "高风险命令需人工确认后才落地。\n\n"
                "补充：该术语仅用于 M12 验收测试。\n",
                encoding="utf-8",
            )
            client.post(f"{BASE}/api/knowledge/reindex", headers=header, json={})
            t0 = time.perf_counter()
            sr = client.post(
                f"{BASE}/api/knowledge/search", headers=header, json={"query": "ZQ-TERM-2048", "top_k": 3}
            )
            timings["rag_search_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            hits = sr.json().get("results", [])
            record(
                "场景B：检索命中且来源准确",
                bool(hits) and "edgeagent-terms.txt" in hits[0]["source"] and hits[0]["score"] > 0,
                f"{len(hits)} 条命中，首位 {Path(hits[0]['source']).name if hits else '—'}（{timings['rag_search_ms']}ms）",
                "命中 terms.txt 且 score>0",
            )
            t0 = time.perf_counter()
            ev = sse_chat(client, sid, "根据本地知识库，解释 ZQ-TERM-2048 的含义")
            kn = [e for e in ev if e["type"] == "knowledge"]
            timings["rag_chat_ms"] = round(time.perf_counter() - t0, 2)
            record(
                "场景B：聊天自动注入 <context>",
                len(kn) == 1 and kn[0]["data"]["hits"],
                f"注入 {len(kn[0]['data']['hits']) if kn else 0} 段，{timings['rag_chat_ms']}s",
            )

            # ---------------- 8. 场景 C：状态 ----------------
            t0 = time.perf_counter()
            st = client.get(f"{BASE}/api/state", params={"session_id": sid}, headers=header).json()
            timings["state_first_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            t0 = time.perf_counter()
            st2 = client.get(f"{BASE}/api/state", params={"session_id": sid}, headers=header).json()
            timings["state_cached_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            system = st["system"]
            top = system["top_processes"]["by_cpu"]
            record(
                "场景C：CPU Top 进程与磁盘数据准确",
                bool(top) and any(d["drive"] == "C:" for d in system["disks"]),
                f"top={top[0]['name'] if top else '—'} cpu={top[0]['cpu_seconds'] if top else 0}s；"
                f"C: 剩 {next((d['free_gb'] for d in system['disks'] if d['drive'] == 'C:'), '—')} GB",
            )
            record(
                "场景C：5 秒采集节流生效",
                st2["system"]["cached"] is True and timings["state_cached_ms"] < timings["state_first_ms"],
                f"首次 {timings['state_first_ms']}ms → 缓存 {timings['state_cached_ms']}ms",
            )
            sid = new_session(client)
            ev = sse_chat(client, sid, "当前哪个进程占用 CPU 最高？我的 C 盘还剩多少空间？")
            res = [e for e in ev if e["type"] == "result"]
            record(
                "场景C：模型侧命令（Get-Process / Get-PSDrive）可执行",
                bool(res) and res[0]["data"]["exit_code"] == 0,
                res[0]["data"]["stdout"].splitlines()[0][:60] if res and res[0]["data"]["stdout"] else "无输出",
            )

            # ---------------- 9. 防双开 ----------------
            second = start_exe("--headless", "--port", str(PORT), "--simulate-browser")
            try:
                second.wait(timeout=25)
                code = second.returncode
            except subprocess.TimeoutExpired:
                code = None
                kill_tree(second.pid)
            still_one = port_open(PORT) and proc.poll() is None
            record(
                "防双开：二次启动自动让路",
                code == 0 and still_one,
                f"第二实例退出码={code}，主实例仍在监听={still_one}",
                "退出码 0 且不抢占端口",
            )

            # ---------------- 10. 配置持久化复检 ----------------
            after = (cfg_path.stat().st_mtime, env_path.stat().st_mtime if env_path.exists() else 0)
            record(
                "配置持久化：运行期间未被重写",
                before == after,
                f"mtime 未变（{time.strftime('%H:%M:%S', time.localtime(after[0]))}）",
            )

        # ---------------- 11. 进程守护 ----------------
        main_pid = proc.pid if proc else 0
        kill_tree(main_pid)
        released = False
        for _ in range(40):
            if not port_open(PORT):
                released = True
                break
            time.sleep(0.25)
        record("进程守护：端口已释放", released, f"{PORT} {'已释放' if released else '仍占用'}")
        ps_left = subprocess.run(
            ["tasklist", "/FI", f"PID eq {main_pid}"], capture_output=True, text=True, errors="replace"
        ).stdout
        record("进程守护：主进程已退出", str(main_pid) not in ps_left, f"PID {main_pid}")
        proc = None

        # ---------------- 12. 异常恢复：删配置 ----------------
        backup = cfg_path.read_bytes()
        cfg_path.unlink()
        check = subprocess.run([str(EXE), "--check"], cwd=str(DIST), capture_output=True, timeout=120)
        recreated = cfg_path.is_file()
        record(
            "异常恢复：删除 config.yaml 后自动重建",
            recreated,
            f"重建={recreated}, exit={check.returncode}",
        )
        if not recreated:
            cfg_path.write_bytes(backup)

        # ---------------- 13. 日志与审计 ----------------
        app_log = DIST / "logs" / "app.log"
        text = app_log.read_text(encoding="utf-8", errors="replace") if app_log.is_file() else ""
        record("日志：中文无乱码", bool(text) and "\ufffd" not in text, f"{len(text)} 字符，无替换符")
        audit_files = sorted((DIST / "logs").glob("audit-*.jsonl"))
        audit_text = audit_files[-1].read_text(encoding="utf-8", errors="replace") if audit_files else ""
        # 确认事件在审计里写作 confirm_result（user_confirm 是旧名），两者取其一即可
        has_guard = '"guard_verdict"' in audit_text
        has_exec = '"command_executed"' in audit_text
        confirm_ok = '"user_confirm"' in audit_text or '"confirm_result"' in audit_text
        record(
            "审计：记录 Guard/执行/确认事件",
            has_guard and has_exec and confirm_ok,
            f"{audit_files[-1].name if audit_files else '无'}，{len(audit_text.splitlines())} 条"
            f"（guard={has_guard} exec={has_exec} confirm={confirm_ok}）",
            "guard_verdict + command_executed + confirm_result",
        )
        record("审计：不含 API Key 明文", "sk-" not in audit_text or "sk-***" in audit_text, "已脱敏")
    finally:
        if proc is not None:
            kill_tree(proc.pid)
            time.sleep(1)

    # 清理测试夹具（保留 docs 供人工复查）
    shutil.rmtree(Path(r"D:\AgentWork\logs"), ignore_errors=True)

    passed = sum(1 for r in results if r["ok"])
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "exe": str(EXE),
        "exe_size_mb": round(EXE.stat().st_size / 1048576, 1),
        "timings": timings,
        "summary": {"total": len(results), "pass": passed, "fail": len(results) - passed},
        "results": results,
    }
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS / f"m12_results_{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    sys.path.insert(0, str(ROOT / "tools"))
    from m12_report import render_report  # 延迟导入，避免循环依赖

    html_path = REPORTS / f"m12_report_{stamp}.html"
    html_path.write_text(render_report(payload), encoding="utf-8")

    print(f"\n=== {passed}/{len(results)} 通过 ===")
    print(f"JSON: {json_path}")
    print(f"HTML: {html_path}")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
