# EdgeAgent

一个跑在本机的 Windows 自动化 Agent：**Edge 浏览器聊天界面 + Python 本地 HTTP 代理 + 受限 PowerShell 执行器 + 人工确认层**。

你在 Edge 里用自然语言描述需求，模型只被允许返回**结构化 PowerShell 指令（JSON）**，代理负责校验、请求你确认、执行并回传结果，直到任务完成或你按下中止。

> 🔗 https://github.com/gyy147852qq-boop/edgeagent ｜ MIT License

---

## 快速上手（60 秒）

```bash
# 1. 依赖（Python 3.11+）
pip install -r requirements.txt

# 2. 配置密钥（复制模板后填入你的 Key，.env 不会被提交）
cp .env.example .env
notepad .env          # 填入 LLM_API_KEY=sk-...

# 3. 启动（自动打开浏览器面板）
python launcher.py
```

面板在 `http://127.0.0.1:8765/`，token 会自动注入到跳转链接里。

**没有 API Key 也能先跑起来**：界面右上角「AI 服务商」→ 选「网页版」→ 选 DeepSeek →
点「打开网页登录」，在弹出的 Edge 窗口里登录一次 DeepSeek 即可（之后自动转后台无头运行）。

**打包成 exe**（免 Python 环境）：

```bash
pip install pyinstaller
python build.py                       # 产出 dist/EdgeAgent.exe
dist\EdgeAgent.exe --mode tray        # 托盘常驻
```

> 打包前务必先 `pip install -r requirements.txt`。依赖没装进打包环境时，
> 产物会小很多（~10 MB 而非 ~26 MB），运行时自检会报「缺少依赖」。

**只读命令自动执行，写操作需点「确认执行」** —— 这是默认且推荐的安全姿态。
顶部可在「限制访问 / 完全访问」之间切换；完全访问模式每条命令仍会逐条确认。

```
Edge 页面(HTML/JS)  ──HTTP + SSE + Bearer Token──▶  本地代理(FastAPI, 127.0.0.1)
                                                        │
                                     会话管理 / 上下文组装
                                                        ▼
                                     大模型客户端(OpenAI 兼容, 流式)
                                                        │ 返回 JSON 指令
                                                        ▼
                                     指令解析器(容错 + 纠错重试)
                                                        ▼
                                     权限校验器(白名单/黑名单/路径/风险)
                                                        ▼
                                     人工确认层(异步挂起等待点击)
                                                        ▼
                                     PowerShell 执行器(超时/截断/白名单环境变量)
                                                        ▼
                                     状态采集器(cwd/目录/进程/磁盘/时间)
                                                        ▼
                                     前端展示 + 历史追加 → 再问模型
```

---

## 1. 目录结构

```
edgeagent/
├─ server/
│  ├─ main.py          FastAPI 入口、静态托管、Token/Origin 中间件、SSE 闭环
│  ├─ config.py        配置加载与校验（含 host 强制 127.0.0.1）
│  ├─ llm_client.py    OpenAI 兼容客户端，流式 + 有限重试
│  ├─ session.py       会话与对话历史（内存 + 落盘）
│  ├─ parser.py        解析 JSON 指令，容错与语义校验
│  ├─ guard.py         白名单 / 黑名单 / 路径校验 / 风险分级
│  ├─ confirm.py       人工确认队列（async Future 挂起）
│  ├─ executor.py      PowerShell 执行器（超时 / 进程树 / 截断）
│  ├─ state.py         状态采集（ctypes，无原生依赖）
│  ├─ audit.py         JSONL 审计日志 + 脱敏
│  ├─ providers.py     AI 服务商注册表（国产预设 + 网页版站点 + 密钥解析）
│  ├─ web_agent.py     网页版驱动（CDP + 四家站点适配器）
│  ├─ cdp.py           极简 DevTools 协议客户端（拉起 Edge + 执行 JS）
│  ├─ rag.py           本地知识库（PDF/TXT 抽取 + TF-IDF 检索）
│  ├─ prompts.py       系统提示词模板
│  ├─ paths.py         资源/数据路径兼容（_MEIPASS vs exe 同级目录）
│  └─ mock_llm.py      内置确定性假模型（LLM_PROVIDER=mock）
├─ web/
│  ├─ index.html       单页应用（无框架、无构建）
│  ├─ app.js           SSE 解析、渲染、重连、模式切换、知识库卡片
│  └─ style.css        浅色/深色自适应样式
├─ tests/
│  ├─ test_parser.py    14 个用例
│  ├─ test_guard.py     35 个用例
│  ├─ test_executor.py   9 个用例
│  ├─ test_mock_llm.py   9 个用例
│  ├─ test_rag.py       10 个用例
│  ├─ test_state.py      6 个用例
│  ├─ test_paths.py      4 个用例
│  ├─ test_providers.py 17 个用例
│  └─ test_web_agent.py 17 个用例
├─ conftest.py         pytest 的 sys.path 注入
├─ launcher.py         桌面客户端：自检 → 配置引导 → 内嵌服务 → 托盘/窗口
├─ build.py            PyInstaller 打包脚本（纯 Python 生成图标）
├─ verify.ps1          一键验证脚本（19 组检查 + HTML 报告）
├─ tools/
│  ├─ m12_acceptance.py  真机验收：对 dist/EdgeAgent.exe 跑 22 项场景
│  └─ m12_report.py      把验收 JSON 渲染成 HTML 报告
├─ config.yaml         实际运行配置（可直接改）
├─ config.example.yaml 配置模板
├─ .env.example        密钥模板
├─ requirements.txt    纯 Python 依赖，无需编译
├─ start.bat           一键启动（拉起桌面客户端）
├─ README.md
├─ reports/            verify.ps1 生成的 HTML 报告（运行后出现）
├─ build/              打包中间产物 + 图标（build.py 生成）
└─ dist/               EdgeAgent.exe（打包产物，约 26 MB）
```

运行期自动生成：`.agent_token`、`logs/`、`sessions/`、`reports/`、`build/`、`dist/`、`.venv/`（均已在 `.gitignore` 中排除）。

---

## 2. 快速开始

### 2.1 准备

- Windows 10/11，PowerShell 5.1（系统自带）
- Python 3.11+（`python --version` 可查）
- 一个 OpenAI 兼容接口的 `base_url` / `api_key` / `model`

### 2.2 填配置（两个文件）

`config.yaml` —— **必须按你的环境改这三项**：

```yaml
llm:
  base_url: "https://api.openai.com/v1"   # 你的 OpenAI 兼容地址，带 /v1
  model: "gpt-4o-mini"                    # 你的模型名
security:
  allowed_dirs:
    - "D:\\AgentWork"                     # 建议指向一个空的专用测试目录
```

`.env` —— 只放密钥（复制 `.env.example` 改名）：

```ini
LLM_API_KEY=sk-你的真实密钥
```

> 密钥只存在服务端内存与 `.env`，**不会**下发到前端，也不会写入审计日志。

`.env` 里还可以用环境变量覆盖配置：`LLM_BASE_URL`、`LLM_MODEL`、`ALLOWED_DIRS`（分号或逗号分隔）、`PORT`、`AGENT_TOKEN`。

### 2.3 安装与启动

**方式一：桌面客户端（推荐，M11）**

```bat
双击 start.bat
```

`start.bat` 会建 venv、装依赖，然后拉起 `launcher.py`：自检 → 缺配置就生成 → 弹窗填 API Key → 后台起服务 → 托盘图标 + 浏览器面板。右键托盘图标可以「打开面板 / 查看日志 / 退出」，退出时服务与残留 PowerShell 子进程会被一并清理。

**方式二：命令行（开发/排障）**

```bat
cd edgeagent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python launcher.py --check     :: 只自检，输出 JSON
python launcher.py             :: 客户端（托盘/窗口 + 浏览器）
python launcher.py --headless  :: 只跑服务（脚本/自动化）
python -m server.main          :: 纯服务模式（无 GUI，打印 token）
```

纯服务模式启动后会打印：

```
================================================================
 EdgeAgent v1.0.0  ->  http://127.0.0.1:8765
 访问 token 已写入: ...\edgeagent\.agent_token
 允许目录: D:\AgentWork
================================================================
```

### 2.4 在 Edge 中打开

- 默认会自动用系统浏览器打开 **带 token 的地址**：`http://127.0.0.1:8765/?token=xxxx`
- 手动打开时访问 `http://127.0.0.1:8765`，把 `.agent_token` 文件内容粘贴到引导页即可（token 只存 `sessionStorage`，并会立刻从地址栏清除）

> 端口被占用时改 `config.yaml` 的 `server.port`；`allowed_origins` 里的端口也要同步改，否则 Origin 校验会 403。

---

## 3. 双模式权限设计

| | 限制访问（默认） | 完全访问 |
|---|---|---|
| 命令范围 | 仅白名单 cmdlet | 更宽范围（黑名单外） |
| 路径 | 必须位于 `allowed_dirs` 内，敏感目录直接拒绝 | 读操作可越界；写/高风险操作触碰系统目录仍拒绝 |
| 需要确认 | `medium` / `high` 风险 | 默认**每条**都确认（`full_mode_confirm: all`） |
| 进入方式 | 默认 | 前端二次确认 + 勾选风险声明 + 写审计 |
| 自动退出 | — | 闲置 15 分钟 / 重置会话 / 重启服务 自动降级 |

### 3.1 风险分级

| 等级 | 含义 | 示例 | 限制模式 | 完全模式 |
|---|---|---|---|---|
| low | 只读 | `Get-ChildItem`、`Get-Content`、`Select-String` | 自动执行 | 需确认（策略可调） |
| medium | 写文件/改名/建目录、重定向 | `Copy-Item`、`New-Item`、`>` | 需确认 | 需确认 |
| high | 删除、下载、执行脚本、改系统配置 | `Remove-Item`、`Invoke-WebRequest` | **直接拒绝**（仅完全模式允许） | 需确认 |

> 模型自报的 `risk` 只是输入，Guard 会按命令内容**重新计算并取更严格值**。模型把 `Remove-Item` 标成 `low` 也照样按 `high` 处理。

### 3.2 黑名单（命中即拒绝，人工确认也无法绕过）

- 磁盘/引导：`Format-Volume`、`Clear-Disk`、`Initialize-Disk`、`diskpart`、`bcdedit`、`vssadmin`
- 关机/重启：`shutdown`、`Stop-Computer`、`Restart-Computer`、`logoff`
- 安全软件/策略：`Set-MpPreference`、`Add-MpPreference`、`Set-ExecutionPolicy`
- 远程执行/持久化：`Invoke-Expression`/`iex`、`Invoke-Command`、`-EncodedCommand`、`FromBase64String`、`DownloadString`、`Start-BitsTransfer`、`Add-Type`、`schtasks`、`New-Service`、`Register-ScheduledTask`
- 提权/账户：`runas`、`Start-Process -Verb RunAs`、`net user/localgroup`、`New-LocalUser`、`icacls`、`takeown`
- 注册表写入：`reg add/delete/import`、`Set-ItemProperty HKLM:\...`
- 危险组合：`Remove-Item -Recurse -Force` 作用于 `C:\Windows` / `Program Files` / `AppData` / `$env:SystemRoot`，或删除驱动器根目录

### 3.3 路径校验

- 命令里出现的**所有**路径（含 `Set-Location` 参数、相对路径、`..\..\Windows`、UNC）都先 `realpath` 规范化再判断；
- 限制模式：必须位于 `allowed_dirs` 之下，否则拒绝（`D:\AgentWork` 与 `D:\AgentWork2` 不会被混为一谈）；
- 两种模式都拒绝 `C:\Windows`、`C:\Program Files`、`C:\ProgramData`、`*\AppData\*`、`.ssh`、`.aws`、`.config`、`\\.\pipe\` 等敏感位置（完全模式下仅对写/高风险操作拒绝，纯读取放行）。

---

## 4. 接口（全部前缀 `/api`，除 `/healthz` 外都需要 `Authorization: Bearer <token>`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 免鉴权存活探测（前端判在线，用，不含敏感信息） |
| GET | `/api/health` | 版本、模型名、是否已配密钥、运行时长 |
| POST | `/api/chat` | 主闭环，SSE 流：`token` / `plan` / `command` / `await_confirm` / `result` / `mode_changed` / `error` / `done` |
| POST | `/api/confirm` | `{session_id, command_id, approved}` → `{ok}` |
| GET | `/api/state` | cwd、mode、allowed_dirs、recent_commands、pending_confirms、system |
| POST | `/api/session/create` | 新建会话，返回 `session_id`（verify.ps1 与第三方客户端用） |
| POST | `/api/session/reset` | 清空历史并回到限制模式（`session_id: null` 时新建会话） |
| POST | `/api/abort` | 中止：置中止位 + 拒绝挂起确认 + 杀进程树 |
| GET | `/api/sessions` | 会话列表（左侧栏用） |
| GET | `/api/session/history` | 会话历史（刷新/切换会话后恢复气泡） |
| POST | `/api/mode` | 切换模式，`full` 必须带 `confirm_full_access: true` |
| POST | `/api/knowledge/search` | 本地知识库检索：`{query, top_k}` → 命中片段（Bearer 保护） |
| POST | `/api/knowledge/reindex` | 强制重建索引（新增/修改文档后立即生效） |
| GET | `/api/knowledge/status` | 索引状态：文件数、块数、词表大小、跳过的文件 |
| GET | `/api/providers` | AI 服务商列表 + 当前选择（密钥只返回"是否已配"） |
| POST | `/api/provider` | 热切换服务商：`{kind, preset, model?}`，写入 provider.json |
| POST | `/api/provider/test` | 发一条极短消息验证连通性，返回耗时与采样 |
| POST | `/api/web/login` | 网页版登录：`{start:true}` 开浏览器，`{start:false}` 保存登录态 |

SSE 事件示例：

```
data: {"type":"token","delta":"我先看看目录。"}
data: {"type":"plan","data":{"steps":[{"command":"Get-ChildItem ...","risk":"low"}]}}
data: {"type":"command","data":{"id":"a1b2","command":"Get-ChildItem -LiteralPath 'D:\\AgentWork'","risk":"low","reason":"列出文件","cwd":"D:\\AgentWork","timeout":15,"require_confirm":false}}
data: {"type":"result","data":{"id":"a1b2","stdout":"...","stderr":"","exit_code":0,"duration_ms":412,"truncated":false,"cwd":"D:\\AgentWork"}}
data: {"type":"done","data":{"summary":"目录里共有 3 个 PDF。"}}
```

---

## 5. 安全设计清单

1. **只监听 `127.0.0.1`**：`config.yaml` 里写别的地址会被强制纠正并告警。
2. **Bearer Token**：启动随机生成，写 `.agent_token`（权限 600），常量时间比较；前端错误 token 一律 401。
3. **Origin + Host 双重校验**：只允许 `127.0.0.1` / `localhost` 且端口匹配，防 DNS rebinding 与跨站调用。
4. **CSP / nosniff / 无 referrer**：响应头固定，前端无任何外部资源。
5. **模型输出只当"候选 JSON"**：解析失败就走纠错重试，**绝不**把裸文本交给 PowerShell。
6. **一次一条命令**：多代码块只取第一个并提示；执行完回传结果再问下一步。
7. **默认先预览再执行**：命令卡片展示完整命令、风险、理由、工作目录、超时；需要确认时按钮才可点。
8. **执行器不用 shell**：`create_subprocess_exec` + 参数列表，`-NoProfile -NonInteractive -ExecutionPolicy Bypass`；环境变量白名单（密钥类不透传）。
9. **超时 + 进程树终止**：默认 30s、上限 120s，超时 `taskkill /T`，退出码标记 124。
10. **输出截断**：单流 > 8000 字符截断并 `truncated: true`。
11. **前端零 `innerHTML`**：所有模型文本用 `textContent` 写入，杜绝 XSS。
12. **审计可追溯**：`logs/audit-YYYY-MM-DD.jsonl` 记录模型原始输出、解析后命令、风险、校验结论、是否确认、执行摘要、拒绝原因；密钥自动脱敏。
13. **禁止落地可执行脚本**：`Set-Content` / `New-Item` / `Copy-Item` 等写操作一旦目标后缀是 `.ps1/.bat/.cmd/.vbs/.hta/.lnk` 等，受限模式直接拒绝，完全模式升级为 `high` 并强制确认（防持久化）。
14. **知识库只读且限域**：RAG 只索引 `knowledge.dirs`（默认 = `allowed_dirs`）内的文本/PDF，逐个 `realpath` 校验，软链接指向目录外会被跳过；检索接口同样需要 Bearer Token。

---

## 6. 审计日志

一天一个文件：`logs/audit-2026-10-27.jsonl`，每行一个 JSON。事件类型：

`turn_start` / `model_output` / `parse_error` / `guard_verdict` / `confirm_result` / `command_executed` / `turn_finished` / `turn_aborted` / `mode_changed` / `mode_auto_downgraded` / `user_confirm` / `abort` / `session_reset` / `llm_error` / `internal_error`

一条典型记录：

```json
{"ts":"2026-10-27T09:12:03.441+08:00","event":"guard_verdict","session_id":"9f1c...","command_id":"a1b2c3","command":"Remove-Item 'D:\\AgentWork\\a.tmp' -Force","declared_risk":"low","risk":"high","allowed":false,"require_confirm":false,"violations":["受限模式白名单外的命令：remove-item"],"mode":"restricted"}
```

---

## 7. 测试

```bat
cd edgeagent
python -m pytest tests -q
```

```
121 passed
```

覆盖点：

- **parser（14）**：标准代码块、文本+代码块、多代码块取首个、无代码块回退、智能引号/尾随逗号/注释修复、缺 JSON、未知 action、空 command、超长 command、timeout 收敛、risk 缺失、finish/ask_user
- **guard（35）**：白名单放行、黑名单拒绝、敏感路径、越界路径、`..` 穿越、网络命令与 `iex`、写操作需确认、白名单外 cmdlet、无法识别的表达式、完全模式放行删除但需确认、格式化/关机/Defender/提权拒绝、注册表写入拒绝、系统目录递归强删拒绝、确认策略、谎报低风险被提升、`Start-Sleep` low、解压到系统目录拒绝、压缩/解压越界拒绝、写 `.ps1` 拒绝（完全模式升为 high）、`Test-NetConnection`/`Get-FileHash` 为 low、**`$env:` / `%VAR%` 展开后再校验路径（防绕过）、计算属性 `@{n=…;e=…}` 不被误判、`)` 串联危险命令仍被拦、引号内 `|` 不切分**
- **executor（9）**：stdout 捕获、stderr + 非零退出码、超时终止进程树、输出截断、`Set-Location` 后回传 cwd、cwd 不存在回退、中文 UTF-8 输出、主动 kill、环境变量白名单过滤
- **mock_llm（9）**：离线假模型的分支确定性（列出/删除/新建/系统路径/耗时/坏 JSON/结束后收尾），保证 verify.ps1 的断言可重复
- **rag（10）**：中文二元组+英文分词、命中/不命中、空查询、目录边界（不索引外部文件）、非文本后缀忽略、损坏 PDF 不崩、`invalidate` 生效、意图识别
- **state（6）**：盘符剩余百分比、单盘用量、新增字段齐全、Top 进程非空、5 秒节流命中缓存、`refresh` 绕过缓存
- **paths（4）**：源码模式资源根定位、`get_resource_path` 返回字符串、`EDGEAGENT_DATA_DIR` 覆盖、非 frozen 判定

> executor / state 的进程采集测试需要 Windows，其它平台自动 skip。

---

## 8. 快速验证（verify.ps1）

不想手动点一遍 UI？双击或运行 `verify.ps1`，它会自己拉起代理、跑完 19 组检查、生成 HTML 报告，最后自动关服务。

```powershell
cd edgeagent
.\verify.ps1            # 完整验证（19 组，约 3 分钟）
.\verify.ps1 -Quick     # 只查环境 + 启服务 + 健康检查（约 10 秒）
.\verify.ps1 -RealLlm   # 用 .env 里的真实模型跑流程（会产生 API 费用）
```

### 参数

| 参数 | 说明 |
|---|---|
| `-Quick` | 只跑环境检查、配置检查、服务启动、`/api/health`，然后关服务 |
| `-Full` | 跑全部 19 组检查（默认行为，可省略） |
| `-RealLlm` | 使用真实接口；**不加则用内置 mock 模型**（结果可重复、零费用） |
| `-Port` | 代理端口，默认 8765（需与 `config.yaml` 一致） |
| `-IdleDowngradeSeconds` | 自动降级等待秒数，默认 30（通过环境变量注入，**不修改 config.yaml**） |
| `-PythonPath` | 指定 Python 解释器（自动探测失败时用） |
| `-KeepServer` | 结束后保留服务，便于手工查看（调试用） |

### 模型：mock 还是真实接口

- 默认走 `LLM_PROVIDER=mock`，由 `server/mock_llm.py` 按关键词返回固定指令，因此「列出/删除/新建/系统路径/耗时/坏 JSON」这些断言可以写死、每次都一样；
- 加 `-RealLlm` 时才调用真实模型，且 `.env` 没配密钥会**自动回退 mock** 并在报告里注明；
- 前端/手工联调也可以用 mock：`set LLM_PROVIDER=mock` 后正常启动即可（不花额度）。

### 19 组检查

1. 环境：Python ≥ 3.11、pip、`powershell.exe`、端口 8765 空闲
2. 配置：`.env`、`LLM_API_KEY`、`config.yaml` 关键项、依赖已安装
3. 服务：后台启动 → 轮询 `/healthz` ≤15s → 读 `.agent_token` → `/api/health` 返回 `ok:true`
4. 单元测试：`pytest tests -q` 并记录 pass/fail
5. 鉴权：无 token→401、伪造 Origin→403、正确 token+Origin→200
6. 会话：`POST /api/session/create` 返回 `session_id`
7. 受限模式 low：`command → result → done`，且**不出现** `await_confirm`
8. 受限模式高危：`guard_denied`，且没有任何 `result`（未落地）
9. 完全模式：切 full → `await_confirm` → 批准 → `result.exit_code=0` → `done`
10. 自动降级：切 full 后闲置 30 秒，轮询 `/api/state` 观察到 `mode=restricted`
11. 中止：命令运行中 `POST /api/abort` → `killed=true` → 退出码非 0 → `aborted` 事件 → 会话复位
12. 纠错：纯文本输出 → `parse_retry` ×2 → `parse_failed`，服务不崩（仅 mock 模式）
13. 路径穿越：`C:\Windows\...` 被 `guard_denied`，且审计里有 `allowed:false`
14. 审计：含 `guard_verdict` / `command_executed` / `user_confirm` / `mode_changed`，且不含密钥明文
15. 关闭：进程退出、端口释放
16. 扩展能力：`Compress-Archive` 压缩 → `Expand-Archive` 解压（校验产物落地）→ `Get-FileHash` 返回 64 位 SHA256（全部走人工确认流程）
17. RAG：重建索引 → 命中验证文档（score > 0）→ 无关查询不误报 → 无 token 返回 401
18. 状态感知：`/api/state` 含盘符剩余率、Top 进程、网卡；400ms 后再次调用命中 5 秒缓存
19. 桌面客户端：`launcher.py --check` 自检 → 客户端 5s 内就绪 → UI 打开动作落盘 → 退出后端口释放 → `dist/EdgeAgent.exe` 自检

### 报告

`reports/verify_report_YYYYMMDD_HHMMSS.html`，包含：

- 通过率卡片 + 检查项/通过/失败/跳过统计（绿=pass、红=fail、黄=skip）
- 逐项明细：编号、测试项、预期结果、实际结果、耗时、状态；失败项附带异常栈
- 环境信息（解释器、PS 版本、模型提供方、端口、会话 ID）
- 审计日志片段（自动脱敏 `sk-***`、`Bearer ***`、agent token）

### 注意事项

- 需要 PowerShell 5.1+；脚本文件必须保存为 **UTF-8 with BOM**（否则中文会乱码、解析报错）
- 会临时创建 `D:\AgentWork\verify_test.txt` 并在结束时删除；「删除文件」用例在受限模式执行，绝不会真删
- 每组用例使用**独立会话**，避免上一组上下文影响模型判断
- 失败不会中断流程：记录 fail 后继续，`try/finally` 保证服务一定被清理

---

## 9. 能力扩展（M10）

### 9.1 新增 PowerShell 能力

| 命令 | 用途 | 风险 | 约束 |
|---|---|---|---|
| `Expand-Archive` | 解压 zip | medium | 目标路径必须在 `allowed_dirs` 内；解压到 `C:\Windows`、`Program Files`、`AppData` 等敏感目录直接拒绝 |
| `Compress-Archive` | 压缩目录/文件 | medium | 源与目标都走路径校验，越界拒绝 |
| `Get-FileHash` | 校验完整性 | low | 只读 |
| `Select-String` | 正则搜索文本 | low | 只读（原本已在白名单） |
| `Set-Content` | 写入文本文件 | medium | 目标必须在允许目录；**禁止写 `.ps1/.bat/.cmd/.vbs/.hta/.lnk` 等可执行后缀**（受限模式直接拒绝，完全模式升为 high） |
| `Test-NetConnection` | Ping / 端口探测 | low | 只读网络探测，用来替代被禁用的 `curl` / `wget` |

这些都只是"多允许了几个命令"，没有任何绕过：每一条仍然要过 `PermissionGuard` 的白名单、路径校验、风险分级和确认层。

### 9.2 本地知识库（RAG）

- 模块：`server/rag.py`，**纯 Python**（PDF 用 `pypdf`，未安装时自动降级为只支持文本文件），不引入向量数据库；
- 索引范围：`knowledge.dirs`，**默认等于 `allowed_dirs`**，逐个 `realpath` 校验，软链接指向目录外会被跳过；
- 切块与检索：按段落聚合 ~600 字，重叠 100 字；中文二元组 + 英文单词分词，TF-IDF 余弦相似度排序；
- 触发方式：用户输入命中「查找 / 总结 / 根据 / 文档 / 规范 / PDF …」等意图词时，`llm_client.augment_messages()` 会先检索，把结果写进 System Prompt 的 `<context>` 标签，并在 SSE 里推一条 `knowledge` 事件（前端显示「本地知识库命中 N 段」卡片）；
- 接口（均需 Bearer Token）：`POST /api/knowledge/search`、`POST /api/knowledge/reindex`、`GET /api/knowledge/status`。

```yaml
knowledge:
  enabled: true
  dirs: []          # 留空 = 跟随 allowed_dirs；想加 D:\Docs 就写在这里
  max_files: 200    # 单次索引的文件数上限
  max_file_mb: 5    # 单文件大小上限
  chunk_chars: 600
  chunk_overlap: 100
  top_k: 5
  cache_seconds: 60 # 索引缓存时长，也可以用 reindex 接口强制刷新
```

### 9.3 系统状态感知增强

`GET /api/state` 的 `system` 字段新增：

- `disks`：每个盘符的 `total_gb / free_gb / used_percent / free_percent / low_space`（<10% 标记为 True）；
- `top_processes`：`by_cpu`（CPU 时间前 5）与 `by_memory`（工作集前 5），含 `name/id/cpu_seconds/mem_mb`；
- `network`：有默认网关的网卡、IPv4、网关地址；
- `cache_ttl` / `cached`：**5 秒内重复调用直接命中缓存**，不再启动 PowerShell 采集（进程/网卡信息本来是唯一需要外部采集的部分）。

进程与网卡用一次 `powershell.exe -NoProfile -Command` 批量采集并解析 JSON，CPU/内存/磁盘仍走 `ctypes` + `shutil`，不引入 `psutil` 之类的原生依赖。

---

## 10. 桌面客户端与打包（M11）

### 10.1 四级外壳，按环境自动降级

| 模式 | 触发条件 | 体验 |
|---|---|---|
| `webview` | 装了 `pywebview` | 独立窗口内嵌 Web UI，无浏览器地址栏 |
| `tray` | 装了 `pystray` + `Pillow`（requirements 已含） | 系统托盘图标 + 默认浏览器打开面板；右键「打开面板 / 查看日志 / 退出」 |
| `window` | 只有标准库 tkinter | 小窗口三个按钮（打开面板 / 查看日志 / 退出），关窗即退出 |
| `headless` | 加了 `--headless` 或无任何 GUI 库 | 只跑服务，自动化/服务器场景用 |

UI 仍是最初那套原生 HTML/CSS/JS，客户端只负责"起服务 + 摆窗口 + 收拾现场"。

### 10.2 启动流程

```
launcher.py
  ├─ 路径解析：只读资源 = sys._MEIPASS，可写数据 = exe 同级（不可写则 %LOCALAPPDATA%\EdgeAgent）
  ├─ 日志重定向：stdout/stderr → logs/app.log（windowed 模式没有控制台）
  ├─ 自检：Python ≥3.11 → 依赖（缺了自动 pip install，失败弹窗）→ config.yaml（缺了从模板生成）
  │        → LLM_API_KEY（缺了弹窗输入，可跳过）→ Edge 版本 → 端口占用
  ├─ 端口已占用且 /healthz 可用 → 认为已有实例，直接打开面板后退出（防双开）
  └─ 启动内嵌 uvicorn（同进程线程）→ 等 /healthz → 展示外壳
```

退出路径统一走 `shutdown()`：`server.should_exit = True` → 线程 join → `executor.kill_all_sync()` 杀掉残留 PowerShell 进程树 → 端口释放。

### 10.3 路径与日志（打包后最容易踩的坑）

- **只读资源**（`web/`、`config.example.yaml`、`.env.example`）在 Python 源码模式下位于仓库根，PyInstaller 单文件模式下位于 `sys._MEIPASS`；用 `server/paths.py` 的 `get_resource_path()` / `resource_path()` 统一解析。
- **可写数据**（`config.yaml`、`.env`、`logs/`、`sessions/`、`.agent_token`）绝不能写 `_MEIPASS`（每次启动都是新的临时目录）；`data_dir()` 优先 exe 同级目录，不可写时退回 `%LOCALAPPDATA%\EdgeAgent`，也可用 `EDGEAGENT_DATA_DIR` 覆盖。
- **日志**：`logs/app.log`（启动器与 stdout/stderr）、`logs/edgeagent.log`（服务日志）、`logs/audit-*.jsonl`（审计）、`logs/launcher_status.json`（运行状态，供排障与自动化读取）。

### 10.4 打包

```bat
python build.py                :: 单文件 dist\EdgeAgent.exe（约 26 MB）
python build.py --onedir       :: 目录模式，启动更快，便于排障
python build.py --icon-only    :: 只生成 build\edgeagent.ico
python build.py --console      :: 保留控制台窗口（调试用）
```

脚本做的事：

1. 检查/安装 PyInstaller；
2. 用**纯 Python**画一个 64×64 的 ICO（不依赖 Pillow），写入 `build/edgeagent.ico`；
3. `--windowed` 隐藏控制台，`--add-data` 带上 `web/`、`config.example.yaml`、`.env.example`、`requirements.txt`；
4. 补齐 uvicorn 的动态导入（`uvicorn.loops.auto` / `protocols.http.auto` / `lifespan.on` 等），否则打包后启动会报找不到协议实现。

产物行为：双击 `EdgeAgent.exe` → 在当前目录生成 `config.yaml` / `.env` / `logs/` → 托盘图标 + 浏览器面板。

> 单文件 exe 首次启动会解压到临时目录（约 2 秒），这是 PyInstaller onefile 的正常开销；要更快就用 `--onedir`。

### 10.5 常见问题

| 现象 | 处理 |
|---|---|
| 双击没反应 | 看 `logs/app.log`；多半是依赖缺失或 8765 被占用 |
| 弹「端口被占用」 | 已有实例在跑就直接用面板；否则改 `config.yaml` 的 `server.port` |
| 托盘图标不显示 | 缺 `pystray`/`Pillow`，会回退到窗口模式；想用托盘执行 `pip install pystray Pillow` |
| 打包后中文乱码 | 保证 `.ps1`/`.bat` 为 UTF-8(BOM)；exe 内部日志统一 UTF-8 |
| 杀进程后端口仍占用 | 用任务管理器结束整棵进程树，或用托盘「退出」（会走 `shutdown()` 清理） |

---

## 11. 真机验收（M12）

`verify.ps1` 验证的是「代理 + 接口」；M12 补上的是「**打包产物**在真机上跑起来对不对」。验收工具已固化到仓库：

```bat
python build.py                  :: 先产出 dist/EdgeAgent.exe
python tools\m12_acceptance.py   :: 22 项场景验收，约 90 秒
```

产物：

- `reports/m12_results_<时间戳>.json` —— 原始结果（供二次加工）
- `reports/m12_report_<时间戳>.html` —— 可直接归档的报告（含性能数据、缺陷复盘、优化建议）

### 22 项验收覆盖

| 阶段 | 检查项 | 验证方式 |
|---|---|---|
| 启动（4） | 冷启动到 `/healthz` 就绪、首页可加载、token 可读、`/api/health` 返回 provider/version | 计时 + HTTP |
| 场景 A（5） | 桌面路径被 Guard 拦截、允许目录内自动执行并列清单、low 风险不弹确认、完全模式删除必弹确认、拒绝后文件仍在 | 真实 `Get-ChildItem` / `Remove-Item` |
| 场景 B（2） | 知识库检索命中且来源准确、聊天自动注入 `<context>` | 临时写入含特征词的 txt 后检索 |
| 场景 C（3） | CPU Top 进程与磁盘数据准确、5 秒节流命中缓存、模型侧 `Get-Process` / `Get-PSDrive` 可执行 | `/api/state` + 真命令 |
| 健壮性（4） | 二次启动自动让路、配置不被改写、端口释放、主进程退出 | 双开 + `netstat` |
| 恢复与日志（4） | 删 `config.yaml` 后自动重建、中文日志无乱码、审计含 `guard_verdict`/`command_executed`/`confirm_result`、不含密钥明文 | 文件读取 |

### 本轮真机发现并修掉的问题

| 缺陷 | 根因 | 修复 |
|---|---|---|
| 合法的只读查询被误拒 | `Select-Object @{n='MB';e={...}}` 里的 `;` 被当语句分隔符，切出伪命令 `e={...}` | `_segments()` 改为括号/引号/注释感知的顶层切分；全文正则扫描保留，嵌套危险命令仍会命中 |
| `$env:` 绕过路径白名单 | 路径抽取只认字面量，环境变量对校验器不可见 | 抽取前展开 `$env:VAR` / `%VAR%`，无法解析的变量直接拒绝 |
| 双开时页面 401 | 第二实例拼的是裸 URL | 从 `logs/launcher_status.json` 取 token 再拼 |
| 退出后端口偶发不释放 | onefile 是「引导进程 + 子进程」 | 统一 `taskkill /T /F` + `Executor.kill_all_sync()` |

### 已知未修（不影响安全）

- 确认卡片会随聊天流滚走，长对话里需要往上翻；
- 右侧状态面板首次展开等 3-4 秒，缺骨架屏；
- RAG 重建索引是全量的；
- 产物未签名，SmartScreen 首次运行提示「未知发布者」。

---

---

## 12. AI 服务商接入（M13）

早期只能用「OpenAI 官方地址 + LLM_API_KEY」一套配置，国产用户要么没 key、要么得手改 yaml。
M13 把「用哪个 AI」变成一个可以随时切换的选择项，并且**不需要重启**。

### 12.1 两类接入

| | API 接入（`kind=openai`） | 网页版（`kind=web`） |
|---|---|---|
| 原理 | 标准 `/chat/completions` | CDP 驱动独立 Edge 实例，操作网页对话框 |
| 需要 | 服务商 API Key | 网页账号（手动登录一次） |
| 稳定性 | 高，服务商保证接口 | 取决于页面结构，站点改版会失效 |
| 费用 | 按 token 计费 | 走网页版额度 |
| 适用 | 日常使用（**推荐**） | 没有 API Key 时的兜底 |

切换入口在顶栏 `AI 服务商` 按钮，面板分三个 tab：**API 接入 / 网页版 / 内置模型**。

### 12.2 内置预设

| 预设 | 服务商 | 默认模型 | 密钥环境变量 |
|---|---|---|---|
| `deepseek` | 深度求索 | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| `tongyi` | 阿里云 DashScope | `qwen-plus` | `DASHSCOPE_API_KEY` |
| `doubao` | 字节 火山方舟 | `doubao-pro-32k` | `ARK_API_KEY` |
| `kimi` | 月之暗面 | `moonshot-v1-8k` | `MOONSHOT_API_KEY` |
| `zhipu` | 智谱 AI | `glm-4-flash` | `ZHIPU_API_KEY` |
| `qianfan` | 百度千帆 | `ernie-4.0-8k` | `QIANFAN_API_KEY` |
| `hunyuan` | 腾讯云 | `hunyuan-lite` | `HUNYUAN_API_KEY` |
| `openai` | OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `custom` | 任意兼容网关 | 自填 | `LLM_API_KEY` |

密钥查找顺序：**预设专属变量 → `LLM_API_KEY` → `OPENAI_API_KEY`**。只配一个通用 key 也能跑；
同一台机器上挂多个服务商时，用专属变量更清晰。

> 预设一旦选定，`base_url` 与 `model` 以预设为准，`config.yaml` 里这两项只在 `preset: custom` 时生效——
> 避免两处配置打架。

### 12.3 切换方式

三种方式等价，优先级从高到低：

1. **界面顶栏**：点服务商按钮 → 选一个 → 立即生效，选择写入 `<数据目录>/provider.json`（不动 config.yaml）。
2. **环境变量**：`LLM_PRESET=deepseek LLM_PROVIDER=openai python -m server.main`
3. **config.yaml**：`llm.preset` / `llm.provider`

`provider.json` 单独存一份的原因：不改用户配置里的注释排版，且打包环境下 config.yaml 可能只读，数据目录一定可写。

### 12.4 网页版

不需要 API Key：程序拉起一个**独立 profile 的 Edge**，把消息打进网页版对话框，再把回复抓回来。

```bat
:: 什么都不用装，exe 自带 CDP 驱动；系统里有 Edge 即可
```

使用步骤：

1. 顶栏 → `网页版` → 选 DeepSeek / 通义 / 豆包 / Kimi
2. 点 **「打开网页登录」**，会弹出一个 Edge 窗口（**这是程序自己的窗口，不是你日常用的那个**，
   profile 存在 `<数据目录>/webprofile`，登录一次就够了）
3. 在窗口里正常登录，看到对话输入框后，回到面板点 **「我已完成登录」**
4. 面板显示「已登录」即可直接发消息，模型回复从网页抓取

实现要点（为什么不是 Playwright）：

| | Playwright | 现在的 CDP 方案 |
|---|---|---|
| 依赖 | 自带 node 内核，解包上百 MB | `websockets`，约 200 KB |
| 打包 | 路径在 PyInstaller 下极易断 | 纯 Python，打进 exe 正常 |
| 控制粒度 | 高层 API | 直接 `Runtime.evaluate` 执行 JS |

具体做法：`--remote-debugging-port` 启动 Edge → `GET /json/list` 拿页面 target →
WebSocket 连 CDP → 用 JS 找输入框、写值（走原生 setter 以兼容 React）、点发送 → 轮询读最后一条回复。

**每轮都新开一个对话**。这是多轮可靠性的关键：网页版会复用同一个消息节点渲染新回复
（DOM 条数不变），新旧内容在页面上无法可靠区分；新开对话后页面上只有一个回复节点，
判定没有歧义。代价是每轮多带一次系统提示（网页版免费），换来的是可复现。

三个必须踩过才知道的坑（都已在代码里处理）：

| 坑 | 现象 | 处理 |
|---|---|---|
| 后台标签页被浏览器降频 | 回复只出半句就"稳定" | `--disable-background-timer-throttling` 等三个开关 |
| 新页面焦点在 body 上 | 字写进去了，回车发不出去 | `Page.bringToFront` + 原生鼠标点击输入框 |
| JS 合成事件 `isTrusted=false` | 点"发送"毫无反应 | 改用 `Input.insertText` / `dispatchKeyEvent` / `dispatchMouseEvent` |

限制要说清楚：

- **页面结构一变就可能失效**。每个站点配多个候选选择器，小改版能自愈；大改需更新
  `web_agent.py` 里的 `SITE_ADAPTERS`。
- **发消息时 Edge 窗口会跳到前台**——原生输入事件需要标签页处于活动状态。
- 首次登录必须有窗口（`web.headless: false`）；登录后可以改 `true` 后台跑（但输入可能因失焦失败，建议保持 false）。
- 不读 cookie、不导出会话、不注入跨站脚本——只是模拟人在页面里打字。
- 网页版不改动权限模型：Guard / ConfirmLayer / 审计照旧。

实测记录（真实 DeepSeek 账号，两轮全自动）：

```
第一轮 14.0s：Get-PSDrive -Name D → exit 0 → "D 盘剩余 232.79 GB（44.6% 可用）"
第二轮  6.6s：模型基于上下文直接总结，无需重复查询
```

### 12.5 安全设计

- **密钥永不下发**：`/api/providers` 只返回 `api_key_configured: true/false`，
  `Selection.public()` 显式排除 `api_key` 字段（有回归测试盯着）。
- **切换留痕**：每次切换写审计 `provider_changed`（kind / preset / model / key 来源 / 是否可用），不记录密钥本身。
- **不支持的组合明确拒绝**：例如 `kind=web&preset=zhipu` 返回 400 并说明原因，而不是静默降级到别的模式。
- **网页版不改变权限模型**：Guard / ConfirmLayer / 审计完全照旧，模型换成谁都不影响 PowerShell 那一层的约束。

### 12.6 常见问题

| 现象 | 原因与处理 |
|---|---|
| 切过去后发消息报「未配置密钥」 | 该预设的 key 变量为空。按面板提示填 `.env` 后**重启服务**（key 只在启动时读入） |
| 面板里网页版是灰的 | 缺少 `websockets` 或找不到 Edge（面板会写明具体是哪一项） |
| 网页版一直「没有返回内容」 | 多半是未登录或触发人机验证；点「打开网页登录」在窗口里手动发一条试试 |
| 提示「该 profile 已被另一个 Edge 窗口占用」 | 上次的 Edge 没关干净，手动关掉那个窗口再重试 |
| 豆包报模型不存在 | 火山方舟需先创建**推理接入点**，`model` 填接入点 ID（形如 `ep-xxxx`） |

---

## 13. 验收自测清单

- [ ] `start.bat` 启动后，Edge 打开 `http://127.0.0.1:8765/?token=...`，顶部显示「已连接」
- [ ] 输入「列出 D:\AgentWork 下的文件」→ 逐字流式输出 → 命令卡片（low，绿色）→ 自动执行 → 结果卡片 → 模型总结（`done`）
- [ ] 输入「删除 D:\AgentWork 下所有临时文件」→ 限制模式：红色错误卡片「安全策略拒绝执行：受限模式白名单外的命令：remove-item」，任务继续由模型给出替代方案
- [ ] 切到「完全访问」（勾选风险声明）→ 重发同样的需求 → 弹出命令卡片 + 确认/拒绝按钮 → 点「确认执行」才落地
- [ ] 要求模型访问 `C:\Windows\System32` → 被路径校验拦截，`logs/audit-*.jsonl` 出现该条 `guard_verdict`（`allowed:false`）
- [ ] 模型返回非法 JSON → 界面提示「正在自动纠错重试 1/2」→ 服务不崩，第二轮拿到合法指令
- [ ] 让模型跑 `Start-Sleep -Seconds 60`（超时设 2s）→ 结果卡片显示超时、`timed_out` 标记、退出码 124
- [ ] 点「中止」→ 挂起确认被拒、进程树被杀、会话恢复可用
- [ ] 关闭代理 → 顶栏变「离线」+ 横幅提示；重启代理 → 5 秒内自动恢复「已连接」
- [ ] 完全访问模式闲置 15 分钟 → 自动回到限制模式并提示
- [ ] `python -m pytest tests -q` 全绿；`logs/audit-*.jsonl` 可逐条核对命令、校验结果、确认状态
- [ ] `.\verify.ps1` 一键跑完 19 组检查，通过率 ≥ 97%（`2.2` 在 mock 模式下为 skip），报告在 `reports/`
- [ ] 双击 `start.bat`：托盘图标出现，浏览器自动打开面板，地址栏 token 已自动带上
- [ ] 托盘右键「退出」后 `netstat -ano | findstr 8765` 无监听，任务管理器里没有残留 powershell.exe
- [ ] `python build.py` 生成 `dist\EdgeAgent.exe`，双击它同样起服务并打开面板（首次会生成 config.yaml / .env / logs）
- [ ] 让模型“把 D:\AgentWork\xx 解压到 D:\AgentWork\out”：弹确认卡片 → 确认后文件夹出现；若目标改成 `C:\Windows\temp` 则被拒绝
- [ ] 输入“根据 D:\AgentWork 里的规范总结一下要点”：聊天区出现「本地知识库命中 N 段」卡片，模型回答引用文件名
- [ ] 右侧状态面板能看到各盘符剩余百分比、Top 进程、网卡 IP/网关，且 5 秒内反复刷新不会卡顿
- [ ] 浏览器 F12 搜索页面源代码/网络响应，**找不到** `api_key`

---

## 14. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 401 | token 不对：重新粘贴 `.agent_token`；或服务重启后 token 变了 |
| 403 非法 Origin | 改了端口但没改 `allowed_origins`，或从非本机地址访问 |
| 「未配置大模型」 | `.env` 里没填 `LLM_API_KEY`，或变量名与 `llm.api_key_env` 不一致 |
| `llm_http_error 404` | `base_url` 写错：应是 `.../v1`，不要带 `/chat/completions` |
| 模型总是返回无法解析 | 换更强的模型；系统提示词已强约束，弱模型仍可能不守协议 |
| 中文输出乱码 | 执行器以 UTF-8 解码，失败回落 GBK；若仍乱码，检查终端代码页 `chcp 65001` |
| 端口被占用 | 改 `server.port` + `allowed_origins` |
| 命令一直「等待确认」 | 确认超时 300s 后自动视为拒绝；可点「中止」立即清理 |

---

## 15. 明确不做的事

- 不用 Electron（M11 的桌面外壳是 Python 原生方案：pywebview / pystray / tkinter，UI 仍是同一套 Web 页面）；
- 不做截图/键鼠模拟/OCR；
- 不使用 `shell=True`，不把模型文本拼接成命令行；
- 不写注册表（只在检测 Edge 版本时**读**注册表）；
- 不提供"先放开以后收紧"的隐藏开关——所有放宽都要经过模式切换 + 确认 + 审计。

> 注：M1 阶段曾约定「不做系统托盘」，M11 按新的产品需求（独立桌面客户端）放开了这条，
> 但安全架构（Guard / ConfirmLayer / 审计）没有做任何让步。
