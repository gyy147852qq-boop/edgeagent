"""把 M12 验收的 JSON 结果渲染成单文件 HTML 报告。

报告不含任何脚本，全部静态 HTML + 内联样式，便于离线打开和直接归档。
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

#: 本轮验收过程中发现并已修复的问题（人工整理，作为交付物的一部分）
FIXED_ISSUES: list[dict[str, str]] = [
    {
        "title": "Guard 误拒合法的只读命令（分段逻辑缺陷）",
        "symptom": "「列出 D:\\AgentWork 下大于 5MB 的 log 文件」「查 CPU 占用最高的进程」这类命令被拒绝，"
        "提示『受限模式无法识别的命令：\\'e={[math]::round($_.length/1…\\'』。",
        "root_cause": "语句切分直接把 `;` 当分隔符，`Select-Object @{n='MB';e={...}}` 里的哈希表分隔符 "
        "被切成一条独立『语句』，片段 `e={...}` 无法匹配白名单 cmdlet。",
        "fix": "改为括号/引号/注释感知的顶层切分（`(){}[]` 内部的 `;`、`|` 不再切分）；"
        "保留 `all_cmdlets()` 全文正则扫描，因此脚本块内嵌套的危险命令仍会被识别。新增 2 条回归测试。",
        "severity": "高（功能性误拒，直接影响可用性）",
    },
    {
        "title": "$env: 环境变量可绕过路径白名单",
        "symptom": "`Get-ChildItem \"$env:USERPROFILE\\Desktop\"` 在受限模式下通过校验。",
        "root_cause": "路径抽取只匹配字面量盘符/相对路径，`$env:USERPROFILE` 对校验器不可见，"
        "等于校验器看不到真实路径。",
        "fix": "路径抽取前先展开 `$env:VAR` 与 `%VAR%`；无法解析的变量直接判为『不可校验路径』并拒绝；"
        "未知变量不再按相对路径放过。",
        "severity": "严重（安全绕过）",
    },
    {
        "title": "双开时第二个实例打开的页面是 401",
        "symptom": "已在运行时再次双击 exe，浏览器打开 `http://127.0.0.1:8765/` 但页面提示未授权。",
        "root_cause": "第二个实例走『已有实例』分支时直接拼了裸 URL，没有带上 token。",
        "fix": "从 `logs/launcher_status.json` 读取当前实例的 token 再拼 `?token=…`；读不到时退化为裸 URL。",
        "severity": "中（体验问题，不影响安全）",
    },
    {
        "title": "onefile 产物退出后端口偶发不释放",
        "symptom": "杀掉 exe 父进程后 8765 仍在监听，下次启动报端口占用。",
        "root_cause": "PyInstaller onefile 是『引导进程 + 真正子进程』，只终止父进程时子进程仍持有监听句柄。",
        "fix": "`kill_tree()` 统一走 `taskkill /T /F`；`shutdown()` 里调用新增的 `Executor.kill_all_sync()` "
        "清理残留 PowerShell 进程树。",
        "severity": "中（影响连续启动）",
    },
]

#: 已知但本轮不修的问题 / UI 瑕疵（诚实记录）
KNOWN_ISSUES: list[str] = [
    "确认卡片在长对话里会随聊天流滚走，用户向上翻才能看到；建议改成固定浮层。",
    "右侧状态面板首次展开需等 3-4 秒（进程 + 网卡采集），期间只显示『采样中』，缺少骨架屏。",
    "RAG 重建索引是全量的，目录内有上万文件时会明显变慢（当前仅按需触发，未做增量）。",
    "打包产物未做数字签名，SmartScreen 首次运行会提示『未知发布者』，需手动『仍要运行』。",
    "窗口模式下日志窗口只能看文件，没有内置的『查看日志』滚动面板（托盘菜单只有打开文件）。",
]

NEXT_STEPS: list[dict[str, str]] = [
    {
        "stage": "M13",
        "title": "浏览器自动化（Playwright）",
        "detail": "新增 `browser.*` 指令类型，必须配套：域名白名单、下载目录限定在 allowed_dirs、"
        "禁止导出 cookie/存储、每步截图存证。Guard 需要从『命令白名单』扩展到『动作 + 目标』白名单。",
    },
    {
        "stage": "M13",
        "title": "多命令批处理与两阶段确认",
        "detail": "当前一次只跑一条命令，长任务要来回十几轮。可引入 plan 事件（含 N 条待执行命令）→ "
        "一次性确认整批 → 逐条执行并逐条回报，失败即停。",
    },
    {
        "stage": "M14",
        "title": "安装包与签名",
        "detail": "Inno Setup 生成带开始菜单项的安装包，配代码签名证书消除 SmartScreen 提示；"
        "同时把数据目录固定到 `%LOCALAPPDATA%\\EdgeAgent`。",
    },
    {
        "stage": "M14",
        "title": "RAG 增量索引",
        "detail": "按 (路径, mtime, size) 维护指纹表，只重解析变化文件；配合后台线程避免阻塞首问。",
    },
]


CSS = """
:root{
  --bg:#f6f7f9; --card:#fff; --ink:#1f2328; --muted:#656d76; --line:#e3e6ea;
  --pass:#1a7f37; --pass-bg:#e8f5ec; --fail:#cf222e; --fail-bg:#fdecec;
  --skip:#9a6700; --skip-bg:#fff8e1; --accent:#0969da;
}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--ink);
  font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px;letter-spacing:.2px}
h2{font-size:17px;margin:34px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.sub{color:var(--muted);font-size:13px;margin-bottom:22px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:20px 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{font-size:12px;color:var(--muted);margin-bottom:6px}
.card .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.card .v.small{font-size:16px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
  border-radius:10px;overflow:hidden;font-size:13px}
th,td{padding:9px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
th{background:#f0f2f5;font-weight:600;font-size:12px;color:var(--muted);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.num{color:var(--muted);font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}
.pass{color:var(--pass);background:var(--pass-bg)}
.fail{color:var(--fail);background:var(--fail-bg)}
.skip{color:var(--skip);background:var(--skip-bg)}
.mono{font-family:ui-monospace,Consolas,"Courier New",monospace;font-size:12px}
.muted{color:var(--muted)}
.issue{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;padding:14px 16px;margin-bottom:12px}
.issue h3{margin:0 0 8px;font-size:14px}
.issue dl{margin:0;display:grid;grid-template-columns:88px 1fr;gap:4px 12px}
.issue dt{color:var(--muted);font-size:12px}
.issue dd{margin:0}
ul.plain{margin:0;padding-left:20px}
ul.plain li{margin-bottom:6px}
.bar{height:8px;border-radius:999px;background:#e6e9ee;overflow:hidden;margin-top:10px}
.bar > i{display:block;height:100%;background:linear-gradient(90deg,#1a7f37,#2da44e)}
.foot{margin-top:40px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px}
"""


def _esc(value: Any) -> str:
    """统一的 HTML 转义，报告里所有动态内容都必须经过它。"""
    return html.escape("" if value is None else str(value), quote=True)


def _timing_rows(timings: dict[str, Any]) -> str:
    """性能指标表（键名与 m12_acceptance.py 里写入的一致）。"""
    labels = [
        ("cold_start_healthz", "冷启动 → /healthz 就绪", "s", "目标 < 3s"),
        ("cold_start_total", "冷启动 → 可交互", "s", "含首页拉取"),
        ("index_first_byte", "首页首字节", "s", "静态资源由同进程托管"),
        ("scenario_a_denied", "场景 A 越界拦截", "s", "Guard 拒绝应立即返回"),
        ("scenario_a_allowed", "场景 A 目录内扫描", "s", "含 PowerShell 冷启动"),
        ("rag_search_ms", "知识库检索", "ms", "TF-IDF 内存检索"),
        ("rag_chat_ms", "RAG 注入 + 模型首响", "s", "含上下文拼装"),
        ("state_first_ms", "状态采集首次", "ms", "进程 + 网卡实时采集"),
        ("state_cached_ms", "状态采集命中缓存", "ms", "5 秒 TTL 内复用"),
    ]
    rows: list[str] = []
    for key, label, unit, note in labels:
        if key not in timings:
            continue
        value = timings[key]
        shown = f"{value:.3f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)
        rows.append(
            f"<tr><td>{_esc(label)}</td><td class='num mono'>{_esc(shown)} {unit}</td>"
            f"<td class='muted'>{_esc(note)}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='3' class='muted'>无性能数据</td></tr>")
    return "\n".join(rows)


def _result_rows(results: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for index, item in enumerate(results, start=1):
        ok = bool(item.get("ok"))
        cls = "pass" if ok else "fail"
        text = "PASS" if ok else "FAIL"
        detail = item.get("detail") or ""
        actual = _esc(item.get("actual"))
        if detail:
            actual += f"<div class='muted mono' style='margin-top:4px'>{_esc(str(detail)[:300])}</div>"
        rows.append(
            "<tr>"
            f"<td class='num'>{index}</td>"
            f"<td>{_esc(item.get('name'))}</td>"
            f"<td class='muted'>{_esc(item.get('expected'))}</td>"
            f"<td>{actual}</td>"
            f"<td><span class='badge {cls}'>{text}</span></td>"
            "</tr>"
        )
    return "\n".join(rows)


def _issue_cards() -> str:
    cards: list[str] = []
    for issue in FIXED_ISSUES:
        cards.append(
            "<div class='issue'>"
            f"<h3>{_esc(issue['title'])}</h3>"
            "<dl>"
            f"<dt>严重程度</dt><dd>{_esc(issue['severity'])}</dd>"
            f"<dt>现象</dt><dd>{_esc(issue['symptom'])}</dd>"
            f"<dt>根因</dt><dd>{_esc(issue['root_cause'])}</dd>"
            f"<dt>修复</dt><dd>{_esc(issue['fix'])}</dd>"
            "</dl></div>"
        )
    return "\n".join(cards)


def render_report(payload: dict[str, Any]) -> str:
    """把验收结果字典渲染成完整 HTML 文档。"""
    summary = payload.get("summary", {})
    total = int(summary.get("total", 0))
    passed = int(summary.get("pass", 0))
    failed = int(summary.get("fail", 0))
    rate = (passed / total * 100) if total else 0.0
    results = payload.get("results", [])
    timings = payload.get("timings", {})

    cold_start = timings.get("cold_start_healthz")
    cold_text = f"{cold_start:.2f} s" if isinstance(cold_start, (int, float)) else "—"
    state_first = timings.get("state_first_ms")
    state_cached = timings.get("state_cached_ms")
    if isinstance(state_first, (int, float)) and isinstance(state_cached, (int, float)) and state_cached > 0:
        speedup = f"{state_first / state_cached:.0f}× 提速"
    else:
        speedup = "—"

    known = "".join(f"<li>{_esc(item)}</li>" for item in KNOWN_ISSUES)
    steps = "".join(
        f"<tr><td class='mono'>{_esc(step['stage'])}</td><td>{_esc(step['title'])}</td>"
        f"<td class='muted'>{_esc(step['detail'])}</td></tr>"
        for step in NEXT_STEPS
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EdgeAgent M12 真机验收报告</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <h1>EdgeAgent M12 真机验收报告</h1>
  <div class="sub">
    生成时间 {_esc(payload.get('generated_at'))} ·
    产物 <span class="mono">{_esc(payload.get('exe'))}</span>
    （{_esc(payload.get('exe_size_mb'))} MB）·
    模型提供方 mock（离线确定性，零费用）
  </div>

  <div class="cards">
    <div class="card"><div class="k">总体通过率</div><div class="v">{rate:.1f}%</div>
      <div class="bar"><i style="width:{rate:.1f}%"></i></div></div>
    <div class="card"><div class="k">通过 / 总数</div><div class="v">{passed} / {total}</div></div>
    <div class="card"><div class="k">失败</div><div class="v" style="color:{'#1a7f37' if failed == 0 else '#cf222e'}">{failed}</div></div>
    <div class="card"><div class="k">冷启动到就绪</div><div class="v">{_esc(cold_text)}</div></div>
    <div class="card"><div class="k">状态采集加速</div><div class="v small">{_esc(speedup)}</div></div>
  </div>

  <h2>1. 逐项结果</h2>
  <table>
    <thead><tr>
      <th style="width:44px">#</th><th style="width:270px">检查项</th>
      <th style="width:210px">预期</th><th>实际</th><th style="width:74px">状态</th>
    </tr></thead>
    <tbody>
{_result_rows(results)}
    </tbody>
  </table>

  <h2>2. 性能数据</h2>
  <table>
    <thead><tr><th style="width:280px">指标</th><th style="width:150px">实测</th><th>说明</th></tr></thead>
    <tbody>
{_timing_rows(timings)}
    </tbody>
  </table>

  <h2>3. 本轮发现并修复的缺陷</h2>
  {_issue_cards()}

  <h2>4. 已知问题与 UI 瑕疵</h2>
  <ul class="plain">
{known}
  </ul>

  <h2>5. 下一步优化建议</h2>
  <table>
    <thead><tr><th style="width:70px">阶段</th><th style="width:230px">方向</th><th>要点</th></tr></thead>
    <tbody>
{steps}
    </tbody>
  </table>

  <h2>6. 复现方式</h2>
  <p class="muted">验收脚本已固化到仓库，可随时重跑（需先生成 exe）：</p>
  <pre class="mono">python build.py
python tools/m12_acceptance.py</pre>
  <p class="muted">
    产物写入 <span class="mono">reports/m12_results_&lt;时间戳&gt;.json</span> 与
    <span class="mono">reports/m12_report_&lt;时间戳&gt;.html</span>。
    脚本会自行清理测试夹具，并在 <span class="mono">finally</span> 中终止服务、释放端口。
  </p>

  <div class="foot">
    本报告由 <span class="mono">tools/m12_report.py</span> 依据同批次 JSON 结果自动生成；
    所有动态内容均已 HTML 转义。审计日志样本见 <span class="mono">dist/logs/audit-*.jsonl</span>（密钥已脱敏）。
  </div>
</div>
</body>
</html>
"""


def main() -> int:
    """命令行入口：把已有的 JSON 结果重新渲染成 HTML（不重跑验收）。"""
    import sys

    if len(sys.argv) < 2:
        print("用法：python tools/m12_report.py <reports/m12_results_xxx.json>")
        return 2
    source = Path(sys.argv[1])
    payload = json.loads(source.read_text(encoding="utf-8"))
    target = source.with_name(source.name.replace("m12_results_", "m12_report_").replace(".json", ".html"))
    target.write_text(render_report(payload), encoding="utf-8")
    print(f"已生成：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
