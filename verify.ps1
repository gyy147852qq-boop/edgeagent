#requires -Version 5.1
<#
.SYNOPSIS
    EdgeAgent 一键验证脚本：启动本地代理，跑完 15 组检查，并生成 HTML 测试报告。

.DESCRIPTION
    覆盖：环境检查 / 配置检查 / 服务启动 / 单元测试 / API 鉴权 / 会话创建 /
    限制模式自动执行 / 限制模式拒绝高危 / 完全模式人工确认 / 模式自动降级 /
    中止与进程树终止 / JSON 纠错重试 / 路径穿越拦截 / 审计日志 / 服务关闭。

    默认使用内置 mock 模型（LLM_PROVIDER=mock），保证结果可重复、不消耗 API 额度。
    加 -RealLlm 则改用 .env 里的真实模型跑流程（结果非确定性，且会产生费用）。

.PARAMETER Quick
    只跑「环境检查 + 服务启动 + 健康检查」（约 10 秒）

.PARAMETER Full
    跑全部 19 组检查（默认行为，约 1-2 分钟）

.PARAMETER RealLlm
    使用真实大模型而不是 mock（需要 .env 中配置 LLM_API_KEY，缺省时自动回退 mock）

.PARAMETER Port
    代理端口，默认 8765（需与 config.yaml 一致）

.PARAMETER IdleDowngradeSeconds
    自动降级测试等待秒数，默认 30（通过环境变量注入，不修改 config.yaml）

.PARAMETER KeepServer
    验证结束后不关闭服务（调试用）

.EXAMPLE
    .\verify.ps1 -Quick
    .\verify.ps1
    .\verify.ps1 -RealLlm -Port 8765

.NOTES
    报告输出：.\reports\verify_report_YYYYMMDD_HHMMSS.html
#>
[CmdletBinding()]
param(
    [switch]$Quick,
    [switch]$Full,
    [switch]$RealLlm,
    [int]$Port = 8765,
    [int]$IdleDowngradeSeconds = 30,
    [string]$PythonPath = '',
    [switch]$KeepServer
)

# 注意：Set-StrictMode 没有 -ErrorAction 参数，错误处理由 $ErrorActionPreference 控制。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Quick -and $Full) { throw '参数冲突：-Quick 与 -Full 不能同时使用' }
if (-not $Quick) { $Full = $true }

# ============================================================================
# 全局状态
# ============================================================================
$script:Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:BaseUrl = "http://127.0.0.1:$Port"
$script:Results = New-Object 'System.Collections.Generic.List[object]'
$script:Notes = New-Object 'System.Collections.Generic.List[string]'
$script:ServerProc = $null
$script:ServerReady = $false
$script:Token = ''
$script:AuthHeaders = @{}
$script:SessionId = ''
$script:UseMock = $true
$script:HasApiKey = $false
$script:ApiKeyValue = ''
$script:PythonExe = 'python'
$script:PythonSource = 'PATH'
$script:PythonVersion = ''
$script:EnvCreated = $false
$script:PortWasFree = $false
$script:AuditFile = ''
$script:StartedAt = Get-Date
$script:LastAbortStatus = 0
$script:LastAbortKilled = $false
$script:ReadySeconds = 0
$script:Events7 = $null
$script:Events8 = $null
$script:Events9 = $null
$script:Events11 = $null
$script:ClientProc = $null
$script:ClientReady = $false
$script:ServerOutLog = Join-Path $script:Root 'logs\verify_server.out.log'
$script:ServerErrLog = Join-Path $script:Root 'logs\verify_server.err.log'
$script:ReportsDir = Join-Path $script:Root 'reports'
$script:SavedEnv = @{}

# ============================================================================
# 控制台输出
# ============================================================================
function Write-Banner {
    param([string]$Text)
    Write-Host ("`n" + ('=' * 78)) -ForegroundColor DarkCyan
    Write-Host ("  $Text") -ForegroundColor Cyan
    Write-Host ('=' * 78) -ForegroundColor DarkCyan
}

function Write-Info {
    param([string]$Text)
    Write-Host ("    $Text") -ForegroundColor DarkGray
}

function Add-Result {
    param(
        [string]$Id,
        [string]$Name,
        [string]$Expected,
        [string]$Actual,
        [string]$Status,
        [double]$DurationMs,
        [string]$Detail = ''
    )
    $script:Results.Add([pscustomobject]@{
            Id         = $Id
            Name       = $Name
            Expected   = $Expected
            Actual     = $Actual
            Status     = $Status
            DurationMs = [math]::Round($DurationMs, 0)
            Detail     = $Detail
        })

    $color = switch ($Status) {
        'pass' { 'Green' }
        'fail' { 'Red' }
        'skip' { 'Yellow' }
        default { 'Gray' }
    }
    $label = switch ($Status) {
        'pass' { 'PASS' }
        'fail' { 'FAIL' }
        'skip' { 'SKIP' }
        default { 'INFO' }
    }
    Write-Host ("  [{0}] {1,-5} {2}" -f $Id, $label, $Name) -ForegroundColor $color -NoNewline
    Write-Host ("  ({0} ms)  {1}" -f [math]::Round($DurationMs, 0), $Actual) -ForegroundColor DarkGray
}

function Get-Prop {
    <# StrictMode 安全的属性读取 #>
    param($Object, [string]$Name, $Default = $null)
    if ($null -eq $Object) { return $Default }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $Default }
    return $prop.Value
}

function Step {
    <# 执行一个检查项：自动计时、捕获异常、记录结果，绝不中断整体流程 #>
    param(
        [string]$Id,
        [string]$Name,
        [string]$Expected,
        [scriptblock]$Action
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $result = & $Action
        $sw.Stop()
        $status = 'pass'
        $actual = '通过'
        $detail = ''
        if ($result -is [hashtable]) {
            if ($result.ContainsKey('Status')) { $status = [string]$result['Status'] }
            if ($result.ContainsKey('Actual')) { $actual = [string]$result['Actual'] }
            if ($result.ContainsKey('Detail')) { $detail = [string]$result['Detail'] }
        }
        Add-Result -Id $Id -Name $Name -Expected $Expected -Actual $actual -Status $status -DurationMs $sw.Elapsed.TotalMilliseconds -Detail $detail
        return $status
    }
    catch {
        $sw.Stop()
        Add-Result -Id $Id -Name $Name -Expected $Expected -Actual ("异常：" + $_.Exception.Message) -Status 'fail' -DurationMs $sw.Elapsed.TotalMilliseconds -Detail $_.ScriptStackTrace
        return 'fail'
    }
}

function Skip-Step {
    param([string]$Id, [string]$Name, [string]$Expected, [string]$Reason)
    Add-Result -Id $Id -Name $Name -Expected $Expected -Actual $Reason -Status 'skip' -DurationMs 0
}

# ============================================================================
# HTTP 工具
# 说明：只用 .NET Framework 自带的 System.Net.HttpWebRequest（无需额外程序集），
#       既能拿到真实状态码（401/403 不会抛异常退出），又能流式读取 SSE。
# ============================================================================
function New-ApiRequest {
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Headers = @{},
        [string]$BaseUrl = ''
    )
    $root = if ($BaseUrl) { $BaseUrl } else { $script:BaseUrl }
    $request = [System.Net.HttpWebRequest]::Create("$root$Path")
    $request.Method = $Method
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 180000
    try { $request.ServicePoint.Expect100Continue = $false } catch { }
    foreach ($key in $Headers.Keys) {
        $request.Headers.Add([string]$key, [string]$Headers[$key])
    }
    return $request
}

function Write-RequestBody {
    param($Request, $Body)
    if ($null -eq $Body) { return }
    $json = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Depth 8 -Compress }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $Request.ContentType = 'application/json; charset=utf-8'
    $Request.ContentLength = $bytes.Length
    $stream = $Request.GetRequestStream()
    try { $stream.Write($bytes, 0, $bytes.Length) }
    finally { $stream.Close() }
}

function Read-ResponseText {
    param($Response)
    $stream = $Response.GetResponseStream()
    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
    try { return $reader.ReadToEnd() }
    finally { $reader.Close(); $stream.Close() }
}

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Headers = @{},
        $Body = $null,
        [string]$BaseUrl = ''
    )
    $request = New-ApiRequest -Method $Method -Path $Path -Headers $Headers -BaseUrl $BaseUrl
    Write-RequestBody -Request $request -Body $Body

    $status = 0
    $text = ''
    try {
        $response = $request.GetResponse()
        try {
            $status = [int]$response.StatusCode
            $text = Read-ResponseText -Response $response
        }
        finally { $response.Close() }
    }
    catch [System.Net.WebException] {
        $webResponse = $_.Exception.Response
        if ($null -eq $webResponse) { throw }
        try {
            $status = [int]$webResponse.StatusCode
            $text = Read-ResponseText -Response $webResponse
        }
        finally { $webResponse.Close() }
    }

    $json = $null
    if ($text -and $text.TrimStart().StartsWith('{')) {
        try { $json = $text | ConvertFrom-Json } catch { $json = $null }
    }
    return [pscustomobject]@{ Status = $status; Text = $text; Json = $json }
}

function Invoke-SseChat {
    <#
      调用 POST /api/chat 并按 SSE 协议逐事件解析。
      -AutoApprove       : 收到 await_confirm 立即 POST /api/confirm 批准
      -AbortOnCommandMs  : 收到第一条 command 事件后等待 N 毫秒再 POST /api/abort
      返回事件数组（PSCustomObject）
    #>
    param(
        [string]$SessionId,
        [string]$Message,
        [string]$Mode = 'restricted',
        [bool]$ConfirmFullAccess = $false,
        [switch]$AutoApprove,
        [int]$AbortOnCommandMs = -1
    )
    $payload = @{
        session_id          = $SessionId
        message             = $Message
        mode                = $Mode
        confirm_full_access = $ConfirmFullAccess
    }
    $request = New-ApiRequest -Method 'POST' -Path '/api/chat' -Headers $script:AuthHeaders
    Write-RequestBody -Request $request -Body $payload

    $response = $null
    try {
        $response = $request.GetResponse()
    }
    catch [System.Net.WebException] {
        $webResponse = $_.Exception.Response
        $code = 0
        $errText = ''
        if ($null -ne $webResponse) {
            $code = [int]$webResponse.StatusCode
            $errText = Read-ResponseText -Response $webResponse
            $webResponse.Close()
        }
        throw "POST /api/chat 返回 $code：$errText"
    }

    $stream = $response.GetResponseStream()
    $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
    $events = New-Object 'System.Collections.Generic.List[object]'
    $aborted = $false
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    try {
        while ($true) {
            $line = $reader.ReadLine()
            if ($null -eq $line) { break }   # 流结束
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            if (-not $line.StartsWith('data:')) { continue }
            $raw = $line.Substring(5).Trim()
            if (-not $raw) { continue }
            $evt = $null
            try { $evt = $raw | ConvertFrom-Json } catch { continue }
            if ($null -eq $evt) { continue }
            $events.Add($evt)

            $type = [string](Get-Prop $evt 'type' '')
            $data = Get-Prop $evt 'data' $null

            if ($type -eq 'await_confirm' -and $AutoApprove) {
                $commandId = [string](Get-Prop $data 'id' '')
                if ($commandId) {
                    $confirmBody = @{ session_id = $SessionId; command_id = $commandId; approved = $true }
                    $null = Invoke-Api -Method 'POST' -Path '/api/confirm' -Headers $script:AuthHeaders -Body $confirmBody
                }
            }

            if ($type -eq 'command' -and $AbortOnCommandMs -ge 0 -and -not $aborted) {
                Start-Sleep -Milliseconds $AbortOnCommandMs
                $abortBody = @{ session_id = $SessionId; kill_process = $true }
                $abortResp = Invoke-Api -Method 'POST' -Path '/api/abort' -Headers $script:AuthHeaders -Body $abortBody
                $script:LastAbortStatus = $abortResp.Status
                $killed = Get-Prop $abortResp.Json 'killed' $false
                $script:LastAbortKilled = [bool]$killed
                $aborted = $true
            }

            if ($type -eq 'done') { break }
            if ($sw.Elapsed.TotalSeconds -gt 180) { break }
        }
    }
    finally {
        $reader.Close()
        $stream.Close()
        $response.Close()
    }
    return $events
}

function New-TestSession {
    <# 每个用例组使用独立会话，避免上一组的对话历史影响模型（尤其是 mock）下一步判断 #>
    $resp = Invoke-Api -Method 'POST' -Path '/api/session/create' -Headers $script:AuthHeaders -Body @{}
    if ($resp.Status -ne 200) { throw "创建会话失败：HTTP $($resp.Status) $($resp.Text)" }
    $sid = [string](Get-Prop $resp.Json 'session_id' '')
    if (-not $sid) { throw '创建会话失败：响应中没有 session_id' }
    return $sid
}

function Find-Event {    param($Events, [string]$Type)
    foreach ($evt in $Events) {
        if ([string](Get-Prop $evt 'type' '') -eq $Type) { return $evt }
    }
    return $null
}

function Get-Events {
    param($Events, [string]$Type)
    $list = New-Object 'System.Collections.Generic.List[object]'
    foreach ($evt in $Events) {
        if ([string](Get-Prop $evt 'type' '') -eq $Type) { $list.Add($evt) }
    }
    return $list.ToArray()
}

# ============================================================================
# 环境/配置工具
# ============================================================================
function Invoke-Native {
    <#
      运行外部命令并收集输出。
      必须临时把 $ErrorActionPreference 降为 Continue：脚本全局是 Stop 时，
      原生命令向 stderr 写任何内容（警告/traceback）都会被 PowerShell 当成异常抛出。
    #>
    param(
        [string]$Exe,
        [string[]]$Arguments = @(),
        [string]$WorkDir = ''
    )
    $oldEa = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $pushed = $false
    try {
        if ($WorkDir) { Push-Location $WorkDir; $pushed = $true }
        $output = & $Exe @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    catch {
        $output = @($_.Exception.Message)
        $exitCode = -1
    }
    finally {
        if ($pushed) { Pop-Location }
        $ErrorActionPreference = $oldEa
    }
    $lines = @()
    foreach ($item in $output) {
        if ($item -is [System.Management.Automation.ErrorRecord]) { $lines += $item.Exception.Message }
        else { $lines += [string]$item }
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = ($lines -join "`n"); Lines = $lines }
}

function Get-PythonVersionText {
    $result = Invoke-Native -Exe $script:PythonExe -Arguments @('--version')
    return $result.Output.Trim()
}

function Test-PythonDeps {
    param([string]$Exe)
    $result = Invoke-Native -Exe $Exe -Arguments @('-c', 'import fastapi, uvicorn, httpx, yaml, dotenv')
    return ($result.ExitCode -eq 0)
}

function Resolve-PythonInterpreter {
    <#
      按优先级解析 Python 解释器：-PythonPath > PATH 中的 python > py -3 > 常见安装目录。
      在多个候选中优先选择「已安装本项目依赖」的那个，避免选中空环境导致后续检查全红。
    #>
    param([string]$Preferred)

    $found = New-Object 'System.Collections.Generic.List[object]'
    $addCandidate = {
        param([string]$Exe, [string]$Label)
        if ([string]::IsNullOrWhiteSpace($Exe)) { return }
        $full = $Exe.Trim().Trim('"')
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { return }
        foreach ($item in $found) { if ($item.Exe -ieq $full) { return } }
        $found.Add([pscustomobject]@{ Exe = $full; Label = $Label })
    }

    if ($Preferred) { & $addCandidate $Preferred '-PythonPath' }

    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $cmd -and $cmd.Source) { & $addCandidate $cmd.Source 'PATH' }

    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        $probe = Invoke-Native -Exe $pyLauncher.Source -Arguments @('-3', '-c', 'import sys;print(sys.executable)')
        if ($probe.ExitCode -eq 0 -and $probe.Lines.Count -gt 0) {
            & $addCandidate ([string]$probe.Lines[0]) 'py -3'
        }
    }

    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python313", 'C:\Python313', 'C:\Python312', 'C:\Python311')) {
        if (-not $root) { continue }
        $pattern = Join-Path $root 'python.exe'
        if (Test-Path -LiteralPath $pattern) { & $addCandidate $pattern '常见安装目录' }
        foreach ($exe in @(Get-ChildItem -Path (Join-Path $root 'Python3*\python.exe') -ErrorAction SilentlyContinue)) {
            & $addCandidate $exe.FullName '常见安装目录'
        }
    }

    if ($found.Count -eq 0) { return $null }
    foreach ($item in $found) {
        if (Test-PythonDeps -Exe $item.Exe) { return $item }
    }
    return $found[0]
}

function Test-PortInUse {
    param([int]$PortNumber)
    try {
        $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue
        if ($null -ne $conn) { return $true }
        return $false
    }
    catch {
        $lines = & netstat.exe -ano 2>$null | Select-String -Pattern ":$PortNumber\s" -SimpleMatch
        return ($null -ne $lines -and @($lines).Count -gt 0)
    }
}

function Read-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed -match "^\s*$([regex]::Escape($Key))\s*=\s*(.*)$") {
            $value = $Matches[1].Trim().Trim('"').Trim("'")
            return $value
        }
    }
    return ''
}

# ============================================================================
# HTML 报告
# ============================================================================
function ConvertTo-HtmlSafe {
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    $s = $Text -replace '&', '&amp;'
    $s = $s -replace '<', '&lt;'
    $s = $s -replace '>', '&gt;'
    $s = $s -replace '"', '&quot;'
    return $s
}

function Get-AuditSnippet {
    param([int]$MaxLines = 24)
    if (-not $script:AuditFile -or -not (Test-Path -LiteralPath $script:AuditFile)) {
        return '（本次运行未找到审计日志文件）'
    }
    try {
        $lines = Get-Content -LiteralPath $script:AuditFile -Encoding UTF8 -Tail $MaxLines
        $text = ($lines -join "`n")
        # 脱敏：密钥、Bearer、agent token
        $text = $text -replace 'sk-[A-Za-z0-9_\-]{8,}', 'sk-***'
        $text = $text -replace '(?i)bearer\s+[A-Za-z0-9._\-]{8,}', 'Bearer ***'
        if ($script:Token) { $text = $text.Replace($script:Token, '***') }
        if ($script:ApiKeyValue) { $text = $text.Replace($script:ApiKeyValue, '***') }
        return $text
    }
    catch {
        return "（读取审计日志失败：$($_.Exception.Message)）"
    }
}

function New-HtmlReport {
    $finished = Get-Date
    $totalSeconds = [math]::Round(($finished - $script:StartedAt).TotalSeconds, 1)
    $pass = 0; $fail = 0; $skip = 0
    foreach ($r in $script:Results) {
        switch ($r.Status) {
            'pass' { $pass++ }
            'fail' { $fail++ }
            'skip' { $skip++ }
        }
    }
    $total = $script:Results.Count
    $rate = if ($total -gt 0) { [math]::Round(($pass / $total) * 100, 1) } else { 0 }
    $rateColor = if ($fail -eq 0) { '#1a8f5a' } elseif ($rate -ge 80) { '#a86400' } else { '#c22a2a' }
    $modelLabel = if ($script:UseMock) { 'mock（内置确定性假模型）' } else { '真实接口 ' + $script:EnvInfo['模型'] }
    $mode = if ($Quick) { 'Quick' } else { 'Full' }

    $sb = New-Object System.Text.StringBuilder
    $null = $sb.AppendLine('<!DOCTYPE html>')
    $null = $sb.AppendLine('<html lang="zh-CN"><head><meta charset="utf-8">')
    $null = $sb.AppendLine('<title>EdgeAgent 验证报告</title>')
    $null = $sb.AppendLine(@'
<style>
  :root{--bg:#f5f7fa;--card:#fff;--text:#1b2230;--dim:#64708a;--border:#dfe3ea;
        --pass:#1a8f5a;--passbg:#e6f6ec;--fail:#c22a2a;--failbg:#fdeaea;
        --skip:#a86400;--skipbg:#fdf1de;--accent:#2f6df6;}
  *{box-sizing:border-box}
  body{margin:0;padding:28px;background:var(--bg);color:var(--text);
       font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  .wrap{max-width:1180px;margin:0 auto}
  h1{margin:0 0 4px;font-size:22px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
  .card .k{color:var(--dim);font-size:12px}
  .card .v{font-size:24px;font-weight:600;margin-top:2px}
  .card.rate{border-color:var(--accent)}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border);
        border-radius:12px;overflow:hidden;font-size:13px}
  th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}
  th{background:#eef1f5;font-size:12px;color:var(--dim);font-weight:600;white-space:nowrap}
  tr:last-child td{border-bottom:0}
  td.id{white-space:nowrap;color:var(--dim);font-family:Consolas,monospace}
  .badge{display:inline-block;padding:1px 9px;border-radius:999px;font-size:11px;font-weight:600}
  .badge.pass{background:var(--passbg);color:var(--pass)}
  .badge.fail{background:var(--failbg);color:var(--fail)}
  .badge.skip{background:var(--skipbg);color:var(--skip)}
  tr.fail td{background:#fffafa}
  .mono{font-family:"Cascadia Mono",Consolas,monospace;font-size:12px}
  pre{background:#0f1319;color:#d7e0ee;padding:14px;border-radius:10px;overflow:auto;
      font:12px/1.55 "Cascadia Mono",Consolas,monospace;max-height:420px;white-space:pre-wrap;word-break:break-all}
  h2{font-size:15px;margin:26px 0 10px}
  .grid{display:grid;grid-template-columns:220px 1fr;gap:4px 16px;font-size:13px}
  .grid .k{color:var(--dim)}
  footer{margin-top:26px;color:var(--dim);font-size:12px}
  .ok{color:var(--pass)}.bad{color:var(--fail)}
</style></head><body><div class="wrap">
'@)
    $null = $sb.AppendLine("<h1>EdgeAgent 验证报告</h1>")
    $null = $sb.AppendLine("<div class='sub'>模式 $mode · 模型 $([System.Net.WebUtility]::HtmlEncode($modelLabel)) · 端口 $Port · 开始 $($script:StartedAt.ToString('yyyy-MM-dd HH:mm:ss')) · 耗时 ${totalSeconds}s</div>")

    $null = $sb.AppendLine('<div class="cards">')
    $null = $sb.AppendLine("<div class='card rate'><div class='k'>总体通过率</div><div class='v' style='color:$rateColor'>$rate%</div></div>")
    $null = $sb.AppendLine("<div class='card'><div class='k'>检查项</div><div class='v'>$total</div></div>")
    $null = $sb.AppendLine("<div class='card'><div class='k'>通过</div><div class='v ok'>$pass</div></div>")
    $null = $sb.AppendLine("<div class='card'><div class='k'>失败</div><div class='v bad'>$fail</div></div>")
    $null = $sb.AppendLine("<div class='card'><div class='k'>跳过</div><div class='v' style='color:#a86400'>$skip</div></div>")
    $null = $sb.AppendLine('</div>')

    $null = $sb.AppendLine('<h2>检查明细</h2><table><thead><tr>')
    $null = $sb.AppendLine('<th>编号</th><th>测试项</th><th>预期结果</th><th>实际结果</th><th>耗时</th><th>状态</th>')
    $null = $sb.AppendLine('</tr></thead><tbody>')
    foreach ($r in $script:Results) {
        $rowClass = if ($r.Status -eq 'fail') { " class='fail'" } else { '' }
        $badge = "<span class='badge $($r.Status)'>$($r.Status.ToUpper())</span>"
        $name = ConvertTo-HtmlSafe $r.Name
        $expected = ConvertTo-HtmlSafe $r.Expected
        $actual = ConvertTo-HtmlSafe $r.Actual
        $detail = ''
        if ($r.Detail) {
            $detail = "<div class='mono' style='color:#64708a;margin-top:4px'>" + (ConvertTo-HtmlSafe $r.Detail) + '</div>'
        }
        $null = $sb.AppendLine("<tr$rowClass><td class='id'>$($r.Id)</td><td>$name$detail</td><td>$expected</td><td>$actual</td><td class='id'>$($r.DurationMs) ms</td><td>$badge</td></tr>")
    }
    $null = $sb.AppendLine('</tbody></table>')

    $null = $sb.AppendLine('<h2>环境信息</h2><div class="grid">')
    foreach ($key in $script:EnvInfo.Keys) {
        $value = ConvertTo-HtmlSafe ([string]$script:EnvInfo[$key])
        $null = $sb.AppendLine("<div class='k'>$([System.Net.WebUtility]::HtmlEncode($key))</div><div>$value</div>")
    }
    $null = $sb.AppendLine('</div>')

    $null = $sb.AppendLine('<h2>审计日志片段（已脱敏）</h2>')
    $auditPath = if ($script:AuditFile) { $script:AuditFile } else { '（无）' }
    $null = $sb.AppendLine("<div class='mono' style='color:#64708a;margin-bottom:6px'>$([System.Net.WebUtility]::HtmlEncode($auditPath))</div>")
    $null = $sb.AppendLine('<pre>' + (ConvertTo-HtmlSafe (Get-AuditSnippet)) + '</pre>')

    if ($script:Notes.Count -gt 0) {
        $null = $sb.AppendLine('<h2>备注</h2><ul>')
        foreach ($note in $script:Notes) {
            $null = $sb.AppendLine('<li>' + (ConvertTo-HtmlSafe $note) + '</li>')
        }
        $null = $sb.AppendLine('</ul>')
    }

    $null = $sb.AppendLine("<footer>由 verify.ps1 自动生成于 $($finished.ToString('yyyy-MM-dd HH:mm:ss')) · 失败项不影响后续检查 · 报告仅记录脱敏信息</footer>")
    $null = $sb.AppendLine('</div></body></html>')

    if (-not (Test-Path -LiteralPath $script:ReportsDir)) {
        $null = New-Item -ItemType Directory -Path $script:ReportsDir -Force
    }
    $fileName = 'verify_report_' + $script:StartedAt.ToString('yyyyMMdd_HHmmss') + '.html'
    $path = Join-Path $script:ReportsDir $fileName
    [System.IO.File]::WriteAllText($path, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
    return $path
}

# ============================================================================
# 服务生命周期
# ============================================================================
function Start-EdgeAgent {
    if (-not (Test-Path -LiteralPath (Join-Path $script:Root 'logs'))) {
        $null = New-Item -ItemType Directory -Path (Join-Path $script:Root 'logs') -Force
    }

    # 环境变量注入（不修改 config.yaml；验证结束后还原）
    foreach ($name in @('LLM_PROVIDER', 'OPEN_BROWSER', 'IDLE_DOWNGRADE_MINUTES')) {
        if (Test-Path Env:$name) { $script:SavedEnv[$name] = (Get-Item Env:$name).Value }
        else { $script:SavedEnv[$name] = $null }
    }
    if ($script:UseMock) { $env:LLM_PROVIDER = 'mock' } else { $env:LLM_PROVIDER = 'openai' }
    $env:OPEN_BROWSER = '0'
    $minutes = [double]$IdleDowngradeSeconds / 60.0
    $env:IDLE_DOWNGRADE_MINUTES = $minutes.ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)

    # $args / $pid 是 PowerShell 自动变量，这里必须用自定义变量名
    $argList = @('-m', 'server.main')
    $startParams = @{
        FilePath         = $script:PythonExe
        ArgumentList     = $argList
        WorkingDirectory = $script:Root
        PassThru         = $true
        WindowStyle      = 'Hidden'
    }
    try {
        # 优先把 stdout/stderr 重定向到日志文件，便于排查启动失败
        $script:ServerProc = Start-Process @startParams -RedirectStandardOutput $script:ServerOutLog -RedirectStandardError $script:ServerErrLog
    }
    catch {
        # 少数环境的进程环境块存在重复键（如同时有 Path / PATH），重定向会失败，退化为不重定向
        $script:Notes.Add('服务日志未能重定向（' + $_.Exception.Message + '），诊断请查看 logs\edgeagent.log')
        $script:ServerProc = Start-Process @startParams
    }
    return $script:ServerProc
}

function Wait-ServerReady {
    param([int]$TimeoutSeconds = 15)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if ($null -ne $script:ServerProc -and $script:ServerProc.HasExited) {
            throw "服务进程已退出（exit=$($script:ServerProc.ExitCode)），请查看 $($script:ServerErrLog)"
        }
        try {
            $resp = Invoke-Api -Method 'GET' -Path '/healthz'
            if ($resp.Status -eq 200) {
                return [math]::Round($sw.Elapsed.TotalSeconds, 2)
            }
        }
        catch {
            # 还没起来，继续等
        }
        Start-Sleep -Milliseconds 400
    }
    throw "服务在 $TimeoutSeconds 秒内未就绪"
}

function Stop-EdgeAgent {
    if ($null -eq $script:ServerProc) { return }
    try {
        if (-not $script:ServerProc.HasExited) {
            $null = & taskkill.exe /T /F /PID $script:ServerProc.Id 2>&1
        }
    }
    catch {
        try { $script:ServerProc.Kill() } catch { }
    }
    try { $null = $script:ServerProc.WaitForExit(8000) } catch { }
}

function Restore-Environment {
    foreach ($name in @('LLM_PROVIDER', 'OPEN_BROWSER', 'IDLE_DOWNGRADE_MINUTES')) {
        if ($script:SavedEnv.ContainsKey($name)) {
            $value = $script:SavedEnv[$name]
            if ($null -eq $value) {
                Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
            }
            else {
                Set-Item -Path "Env:$name" -Value $value
            }
        }
    }
}

# ============================================================================
# 主流程
# ============================================================================
$modeText = if ($Quick) { 'Quick（环境 + 服务启动 + 健康检查）' } else { 'Full（全部 19 组检查）' }
Write-Banner "EdgeAgent 一键验证  ·  $modeText"
Write-Info "项目目录：$($script:Root)"
Write-Info "目标地址：$($script:BaseUrl)"
Write-Info "报告目录：$($script:ReportsDir)"

try {

    # 先解析 Python 解释器（后续所有步骤都依赖它）
    $pythonInfo = Resolve-PythonInterpreter -Preferred $PythonPath
    if ($null -ne $pythonInfo) {
        $script:PythonExe = $pythonInfo.Exe
        $script:PythonSource = $pythonInfo.Label
    }

    # ---------------------------------------------------------------- 组 1 环境
    Write-Banner '组 1/19  环境检查'

    $null = Step -Id '1.1' -Name 'Python 版本 >= 3.11' -Expected '显示 Python 3.11 及以上' -Action {
        if ($null -eq $pythonInfo) { return @{ Status = 'fail'; Actual = '未找到可用的 Python 解释器（可用 -PythonPath 指定）' } }
        $versionText = Get-PythonVersionText
        if (-not $versionText) { return @{ Status = 'fail'; Actual = '无法执行 Python 解释器' } }
        $script:PythonVersion = $versionText
        if ($versionText -match '(\d+)\.(\d+)') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            $ok = ($major -gt 3) -or ($major -eq 3 -and $minor -ge 11)
            $detail = "$versionText  |  $($script:PythonExe)  [$($script:PythonSource)]"
            return @{ Status = $(if ($ok) { 'pass' } else { 'fail' }); Actual = $versionText; Detail = $detail }
        }
        return @{ Status = 'fail'; Actual = "无法解析版本号：$versionText" }
    }

    $null = Step -Id '1.2' -Name 'pip 可用' -Expected 'pip 版本信息输出' -Action {
        $result = Invoke-Native -Exe $script:PythonExe -Arguments @('-m', 'pip', '--version')
        if ($result.ExitCode -ne 0) { return @{ Status = 'fail'; Actual = $result.Output.Trim() } }
        return @{ Status = 'pass'; Actual = $result.Output.Trim() }
    }

    $null = Step -Id '1.3' -Name 'powershell.exe 可用' -Expected '能在 PATH 中找到 powershell.exe' -Action {
        $cmd = Get-Command powershell.exe -ErrorAction SilentlyContinue
        if ($null -eq $cmd) { return @{ Status = 'fail'; Actual = '未找到 powershell.exe' } }
        return @{ Status = 'pass'; Actual = $cmd.Source }
    }

    $null = Step -Id '1.4' -Name "端口 $Port 空闲" -Expected '启动前端口未被占用' -Action {
        if (Test-PortInUse -PortNumber $Port) {
            return @{ Status = 'fail'; Actual = "端口 $Port 已被占用，请先关闭占用进程" }
        }
        $script:PortWasFree = $true
        return @{ Status = 'pass'; Actual = "端口 $Port 空闲" }
    }

    # ---------------------------------------------------------------- 组 2 配置
    Write-Banner '组 2/19  配置检查'

    $envPath = Join-Path $script:Root '.env'
    $configPath = Join-Path $script:Root 'config.yaml'
    $envExamplePath = Join-Path $script:Root '.env.example'
    # 从干净检出运行时，.env 往往还不存在：从模板生成一份，避免后续全部检查无谓失败
    if (-not (Test-Path -LiteralPath $envPath) -and (Test-Path -LiteralPath $envExamplePath)) {
        try {
            Copy-Item -LiteralPath $envExamplePath -Destination $envPath -Force
            $script:EnvCreated = $true
            $script:Notes.Add('未找到 .env，已从 .env.example 生成。使用真实模型前请填入 LLM_API_KEY。')
        }
        catch { }
    }
    $script:ApiKeyValue = Read-DotEnvValue -Path $envPath -Key 'LLM_API_KEY'
    $script:HasApiKey = [bool]$script:ApiKeyValue
    if (-not $RealLlm) {
        $script:UseMock = $true
    }
    elseif (-not $script:HasApiKey) {
        $script:UseMock = $true
        $script:Notes.Add('未检测到 LLM_API_KEY，已自动回退到 mock 模型继续验证。')
    }
    else {
        $script:UseMock = $false
    }
    Write-Info ("模型提供方：" + $(if ($script:UseMock) { 'mock（确定性）' } else { '真实接口' }))

    $null = Step -Id '2.1' -Name '.env 文件存在' -Expected '项目根目录存在 .env' -Action {
        if (Test-Path -LiteralPath $envPath) {
            $actual = if ($script:EnvCreated) { '已从 .env.example 生成' } else { '已找到 .env' }
            return @{ Status = 'pass'; Actual = $actual }
        }
        return @{ Status = 'fail'; Actual = '缺少 .env（可复制 .env.example）' }
    }

    if ($script:UseMock) {
        Skip-Step -Id '2.2' -Name 'LLM_API_KEY 已配置' -Expected 'LLM_API_KEY 非空' -Reason 'mock 模式跳过（加 -RealLlm 可校验真实密钥）'
    }
    else {
        $null = Step -Id '2.2' -Name 'LLM_API_KEY 已配置' -Expected 'LLM_API_KEY 非空' -Action {
            if ($script:HasApiKey) { return @{ Status = 'pass'; Actual = '已配置（长度 ' + $script:ApiKeyValue.Length + '）' } }
            return @{ Status = 'fail'; Actual = 'LLM_API_KEY 为空' }
        }
    }

    $null = Step -Id '2.3' -Name 'config.yaml 关键项完整' -Expected '包含 base_url / model / allowed_dirs' -Action {
        if (-not (Test-Path -LiteralPath $configPath)) { return @{ Status = 'fail'; Actual = '缺少 config.yaml' } }
        $text = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8
        $missing = @()
        foreach ($key in @('base_url', 'model', 'allowed_dirs')) {
            if ($text -notmatch "(?m)^\s*$key\s*:") { $missing += $key }
        }
        if ($missing.Count -gt 0) { return @{ Status = 'fail'; Actual = '缺少字段：' + ($missing -join ', ') } }
        return @{ Status = 'pass'; Actual = 'base_url / model / allowed_dirs 均存在' }
    }

    $null = Step -Id '2.4' -Name 'Python 依赖已安装' -Expected 'fastapi/uvicorn/httpx/yaml/dotenv 可导入' -Action {
        $result = Invoke-Native -Exe $script:PythonExe -Arguments @('-c', 'import fastapi, uvicorn, httpx, yaml, dotenv')
        if ($result.ExitCode -ne 0) {
            $tail = ($result.Lines | Select-Object -Last 1)
            return @{ Status = 'fail'; Actual = '依赖缺失：' + $tail; Detail = '请执行 pip install -r requirements.txt' }
        }
        return @{ Status = 'pass'; Actual = '依赖齐全' }
    }

    # ---------------------------------------------------------------- 组 3 服务启动
    Write-Banner '组 3/19  服务启动'

    $started = $false
    if ($script:PortWasFree -and $null -eq $script:ServerProc) {
        $status = Step -Id '3.1' -Name '后台启动 python -m server.main' -Expected '进程被创建并持续运行' -Action {
            $proc = Start-EdgeAgent
            Start-Sleep -Milliseconds 800
            if ($proc.HasExited) {
                return @{ Status = 'fail'; Actual = "进程立即退出（exit=$($proc.ExitCode)），见 logs\verify_server.err.log" }
            }
            return @{ Status = 'pass'; Actual = "PID $($proc.Id)" }
        }
        $started = ($status -eq 'pass')
    }
    else {
        Skip-Step -Id '3.1' -Name '后台启动 python -m server.main' -Expected '进程被创建并持续运行' -Reason '端口被占用或服务未清理'
    }

    if ($started) {
        $readySeconds = 0
        $readyStatus = Step -Id '3.2' -Name '服务就绪（轮询 /healthz，≤15s）' -Expected '15 秒内 /healthz 返回 200' -Action {
            $script:ReadySeconds = Wait-ServerReady -TimeoutSeconds 15
            return @{ Status = 'pass'; Actual = "就绪耗时 $($script:ReadySeconds)s" }
        }

        if ($readyStatus -eq 'pass') {
            $script:ServerReady = $true
            $null = Step -Id '3.3' -Name '读取 .agent_token' -Expected '存在 16 位以上 token' -Action {
                $tokenPath = Join-Path $script:Root '.agent_token'
                if (-not (Test-Path -LiteralPath $tokenPath)) { return @{ Status = 'fail'; Actual = '缺少 .agent_token' } }
                $script:Token = (Get-Content -LiteralPath $tokenPath -Raw -Encoding UTF8).Trim()
                if ($script:Token.Length -lt 16) { return @{ Status = 'fail'; Actual = "token 过短（$($script:Token.Length)）" } }
                $script:AuthHeaders = @{ 'Authorization' = "Bearer $($script:Token)" }
                return @{ Status = 'pass'; Actual = "token 长度 $($script:Token.Length)" }
            }

            $null = Step -Id '3.4' -Name '/api/health 返回 ok:true' -Expected '带 token 请求返回 {ok:true}' -Action {
                $resp = Invoke-Api -Method 'GET' -Path '/api/health' -Headers $script:AuthHeaders
                if ($resp.Status -ne 200) { return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)" } }
                $ok = Get-Prop $resp.Json 'ok' $false
                if (-not $ok) { return @{ Status = 'fail'; Actual = $resp.Text } }
                $provider = [string](Get-Prop $resp.Json 'provider' 'unknown')
                return @{ Status = 'pass'; Actual = "ok:true · provider=$provider"; Detail = $resp.Text }
            }
        }
        else {
            Skip-Step -Id '3.3' -Name '读取 .agent_token' -Expected '存在 16 位以上 token' -Reason '服务未就绪'
            Skip-Step -Id '3.4' -Name '/api/health 返回 ok:true' -Expected '带 token 请求返回 {ok:true}' -Reason '服务未就绪'
        }
    }
    else {
        Skip-Step -Id '3.2' -Name '服务就绪（轮询 /healthz，≤15s）' -Expected '15 秒内 /healthz 返回 200' -Reason '服务未启动'
        Skip-Step -Id '3.3' -Name '读取 .agent_token' -Expected '存在 16 位以上 token' -Reason '服务未启动'
        Skip-Step -Id '3.4' -Name '/api/health 返回 ok:true' -Expected '带 token 请求返回 {ok:true}' -Reason '服务未启动'
    }

    # ---------------------------------------------------------------- Quick 到此为止
    if ($Quick) {
        Write-Banner 'Quick 模式：跳过其余检查'
        $script:Notes.Add('Quick 模式仅验证环境、服务启动与健康检查；完整验证请运行 .\verify.ps1')
    }
    else {

        # ------------------------------------------------------------ 组 4 单元测试
        Write-Banner '组 4/19  单元测试复跑'
        $null = Step -Id '4.1' -Name 'pytest tests -q' -Expected '全部通过（0 failed）' -Action {
            $result = Invoke-Native -Exe $script:PythonExe -Arguments @('-m', 'pytest', 'tests', '-q') -WorkDir $script:Root
            $text = $result.Output
            $exitCode = $result.ExitCode
            $passed = 0; $failed = 0
            if ($text -match '(\d+) passed') { $passed = [int]$Matches[1] }
            if ($text -match '(\d+) failed') { $failed = [int]$Matches[1] }
            $tail = ($text.Trim() -split "`n" | Select-Object -Last 3) -join ' '
            if ($exitCode -eq 0 -and $failed -eq 0) {
                return @{ Status = 'pass'; Actual = "$passed passed / $failed failed"; Detail = $tail }
            }
            return @{ Status = 'fail'; Actual = "$passed passed / $failed failed（exit=$exitCode）"; Detail = $tail }
        }

        # ------------------------------------------------------------ 组 5 鉴权
        if (-not $script:ServerReady) {
            foreach ($id in @('5.1', '5.2', '5.3')) {
                Skip-Step -Id $id -Name 'API 鉴权检查' -Expected '按规范返回 401/403/200' -Reason '服务未就绪'
            }
        }
        else {
            Write-Banner '组 5/19  API 鉴权'
            $null = Step -Id '5.1' -Name '无 token 访问 /api/health' -Expected 'HTTP 401' -Action {
                $resp = Invoke-Api -Method 'GET' -Path '/api/health'
                if ($resp.Status -eq 401) { return @{ Status = 'pass'; Actual = 'HTTP 401' } }
                return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)（预期 401）" }
            }

            $null = Step -Id '5.2' -Name '伪造 Origin 访问' -Expected 'HTTP 403' -Action {
                $headers = @{ 'Authorization' = "Bearer $($script:Token)"; 'Origin' = 'http://evil.example.com' }
                $resp = Invoke-Api -Method 'GET' -Path '/api/health' -Headers $headers
                if ($resp.Status -eq 403) { return @{ Status = 'pass'; Actual = 'HTTP 403' } }
                return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)（预期 403）" }
            }

            $null = Step -Id '5.3' -Name '正确 token + 正确 Origin' -Expected 'HTTP 200' -Action {
                $headers = @{ 'Authorization' = "Bearer $($script:Token)"; 'Origin' = "http://127.0.0.1:$Port" }
                $resp = Invoke-Api -Method 'GET' -Path '/api/health' -Headers $headers
                if ($resp.Status -eq 200) { return @{ Status = 'pass'; Actual = 'HTTP 200' } }
                return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)（预期 200）" }
            }
        }

        # ------------------------------------------------------------ 组 6 会话
        Write-Banner '组 6/19  会话创建'
        if ($script:ServerReady) {
            $null = Step -Id '6.1' -Name 'POST /api/session/create' -Expected '返回 session_id' -Action {
                $resp = Invoke-Api -Method 'POST' -Path '/api/session/create' -Headers $script:AuthHeaders -Body @{}
                if ($resp.Status -ne 200) { return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)：$($resp.Text)" } }
                $sid = [string](Get-Prop $resp.Json 'session_id' '')
                if (-not $sid) { return @{ Status = 'fail'; Actual = '响应中没有 session_id' } }
                $script:SessionId = $sid
                return @{ Status = 'pass'; Actual = "session_id=$sid" }
            }
        }
        else {
            Skip-Step -Id '6.1' -Name 'POST /api/session/create' -Expected '返回 session_id' -Reason '服务未就绪'
        }

        $canRunFlow = $script:ServerReady -and [bool]$script:SessionId

        # ------------------------------------------------------------ 组 7 low 自动执行
        Write-Banner '组 7/19  限制模式：low 风险自动执行'
        if (-not $canRunFlow) {
            Skip-Step -Id '7.1' -Name '事件链 command → result → done' -Expected '三个事件依次出现' -Reason '服务或会话不可用'
            Skip-Step -Id '7.2' -Name '未出现 await_confirm' -Expected 'low 风险自动执行' -Reason '服务或会话不可用'
        }
        else {
            $events7 = $null
            $null = Step -Id '7.1' -Name '事件链 command → result → done' -Expected '收到 command、result、done 三个事件' -Action {
                $script:SessionId = New-TestSession
                $script:Events7 = Invoke-SseChat -SessionId $script:SessionId -Message '列出 D:\AgentWork 下的文件' -Mode 'restricted'
                $types = @($script:Events7 | ForEach-Object { [string](Get-Prop $_ 'type' '') })
                $hasCommand = $types -contains 'command'
                $hasResult = $types -contains 'result'
                $hasDone = $types -contains 'done'
                $cmdEvent = Find-Event $script:Events7 'command'
                $cmdText = [string](Get-Prop (Get-Prop $cmdEvent 'data' $null) 'command' '')
                $resEvent = Find-Event $script:Events7 'result'
                $exitCode = Get-Prop (Get-Prop $resEvent 'data' $null) 'exit_code' $null
                $detail = "command=$cmdText | exit_code=$exitCode"
                if ($hasCommand -and $hasResult -and $hasDone) {
                    return @{ Status = 'pass'; Actual = '三类事件齐全'; Detail = $detail }
                }
                return @{ Status = 'fail'; Actual = "缺少事件（command=$hasCommand result=$hasResult done=$hasDone）"; Detail = ($types -join ',') }
            }

            $null = Step -Id '7.2' -Name '未出现 await_confirm' -Expected 'low 风险命令自动执行，不做人工确认' -Action {
                $confirms = @(Get-Events $script:Events7 'await_confirm')
                if ($confirms.Count -eq 0) { return @{ Status = 'pass'; Actual = '未出现确认请求' } }
                return @{ Status = 'fail'; Actual = "出现了 $($confirms.Count) 次确认请求" }
            }
        }

        # ------------------------------------------------------------ 组 8 高危拒绝
        Write-Banner '组 8/19  限制模式：高危命令被拒绝'
        if (-not $canRunFlow) {
            Skip-Step -Id '8.1' -Name '返回 guard_denied' -Expected 'error 事件 code=guard_denied' -Reason '服务或会话不可用'
            Skip-Step -Id '8.2' -Name '未执行删除命令' -Expected '没有 result 事件 / 没有实际删除' -Reason '服务或会话不可用'
        }
        else {
            $null = Step -Id '8.1' -Name '返回 guard_denied' -Expected 'error 事件 code=guard_denied' -Action {
                # 每组用例独立会话，避免历史上下文影响模型判断；并确保处于受限模式
                $script:SessionId = New-TestSession
                $null = Invoke-Api -Method 'POST' -Path '/api/mode' -Headers $script:AuthHeaders -Body @{ session_id = $script:SessionId; mode = 'restricted' }
                $script:Events8 = Invoke-SseChat -SessionId $script:SessionId -Message '删除 D:\AgentWork 下所有文件' -Mode 'restricted'
                $errors = @(Get-Events $script:Events8 'error')
                $denied = @($errors | Where-Object { [string](Get-Prop (Get-Prop $_ 'data' $null) 'code' '') -eq 'guard_denied' })
                if ($denied.Count -ge 1) {
                    $msg = [string](Get-Prop (Get-Prop $denied[0] 'data' $null) 'message' '')
                    return @{ Status = 'pass'; Actual = '收到 guard_denied'; Detail = $msg }
                }
                if ($script:UseMock) {
                    return @{ Status = 'fail'; Actual = "未收到 guard_denied（error 数：$($errors.Count)）" }
                }
                return @{ Status = 'skip'; Actual = '真实模型未提交删除命令，无法断言（建议 -Mock 验证）' }
            }

            $null = Step -Id '8.2' -Name '未执行删除命令' -Expected '事件流中没有 result（未落地）' -Action {
                $results = @(Get-Events $script:Events8 'result')
                if ($results.Count -eq 0) { return @{ Status = 'pass'; Actual = '没有任何命令被执行' } }
                return @{ Status = 'fail'; Actual = "有 $($results.Count) 条命令被执行了" }
            }
        }

        # ------------------------------------------------------------ 组 9 完全模式确认
        Write-Banner '组 9/19  完全模式：人工确认后执行'
        if (-not $canRunFlow) {
            foreach ($id in @('9.1', '9.2', '9.3')) {
                Skip-Step -Id $id -Name '完全模式确认执行' -Expected '按规范执行' -Reason '服务或会话不可用'
            }
        }
        else {
            $null = Step -Id '9.1' -Name 'POST /api/mode 切换到 full' -Expected '返回 mode=full' -Action {
                $script:SessionId = New-TestSession
                $body = @{ session_id = $script:SessionId; mode = 'full'; confirm_full_access = $true }
                $resp = Invoke-Api -Method 'POST' -Path '/api/mode' -Headers $script:AuthHeaders -Body $body
                $mode = [string](Get-Prop $resp.Json 'mode' '')
                if ($resp.Status -eq 200 -and $mode -eq 'full') { return @{ Status = 'pass'; Actual = 'mode=full' } }
                return @{ Status = 'fail'; Actual = "HTTP $($resp.Status) mode=$mode" }
            }

            $script:Events9 = $null
            $null = Step -Id '9.2' -Name '写入命令弹出 await_confirm' -Expected '收到 await_confirm 事件' -Action {
                $script:Events9 = Invoke-SseChat -SessionId $script:SessionId `
                    -Message '新建文件 D:\AgentWork\verify_test.txt' -Mode 'full' -ConfirmFullAccess $true -AutoApprove
                $confirms = @(Get-Events $script:Events9 'await_confirm')
                if ($confirms.Count -ge 1) {
                    $cmdId = [string](Get-Prop (Get-Prop $confirms[0] 'data' $null) 'id' '')
                    return @{ Status = 'pass'; Actual = "收到确认请求（id=$cmdId）" }
                }
                if ($script:UseMock) { return @{ Status = 'fail'; Actual = '未收到 await_confirm' } }
                return @{ Status = 'skip'; Actual = '真实模型可能直接 finish，未产生命令' }
            }

            $null = Step -Id '9.3' -Name '批准后执行成功并结束' -Expected 'result.exit_code=0 且收到 done' -Action {
                if ($null -eq $script:Events9) { return @{ Status = 'skip'; Actual = '上一步未产生事件流' } }
                $results = @(Get-Events $script:Events9 'result')
                $done = Find-Event $script:Events9 'done'
                if ($results.Count -eq 0) { return @{ Status = 'fail'; Actual = '没有 result 事件' } }
                $exitCode = Get-Prop (Get-Prop $results[0] 'data' $null) 'exit_code' $null
                $fileExists = Test-Path -LiteralPath 'D:\AgentWork\verify_test.txt'
                if ($exitCode -eq 0 -and $null -ne $done) {
                    return @{ Status = 'pass'; Actual = "exit_code=0，文件已创建=$fileExists"; Detail = [string](Get-Prop (Get-Prop $done 'data' $null) 'summary' '') }
                }
                return @{ Status = 'fail'; Actual = "exit_code=$exitCode，done=$($null -ne $done)" }
            }
        }

        # ------------------------------------------------------------ 组 10 自动降级
        Write-Banner '组 10/19  完全访问模式自动降级'
        if (-not $canRunFlow) {
            Skip-Step -Id '10.1' -Name "闲置 $IdleDowngradeSeconds 秒自动降级" -Expected '模式自动回到 restricted' -Reason '服务或会话不可用'
        }
        else {
            $null = Step -Id '10.1' -Name "闲置 $IdleDowngradeSeconds 秒自动降级" -Expected "等待 ≤$($IdleDowngradeSeconds + 20) 秒后 mode=restricted" -Action {
                $script:SessionId = New-TestSession
                $body = @{ session_id = $script:SessionId; mode = 'full'; confirm_full_access = $true }
                $null = Invoke-Api -Method 'POST' -Path '/api/mode' -Headers $script:AuthHeaders -Body $body

                $sw = [System.Diagnostics.Stopwatch]::StartNew()
                $recovered = $false
                $lastMode = 'full'
                $limit = $IdleDowngradeSeconds + 20
                while ($sw.Elapsed.TotalSeconds -lt $limit) {
                    Start-Sleep -Seconds 3
                    $resp = Invoke-Api -Method 'GET' -Path "/api/state?session_id=$($script:SessionId)" -Headers $script:AuthHeaders
                    if ($resp.Status -eq 200) {
                        $lastMode = [string](Get-Prop $resp.Json 'mode' 'unknown')
                        if ($lastMode -eq 'restricted') { $recovered = $true; break }
                    }
                }
                $elapsed = [math]::Round($sw.Elapsed.TotalSeconds, 1)
                if ($recovered) {
                    return @{ Status = 'pass'; Actual = "已自动降级（$elapsed 秒）"; Detail = '通过轮询 /api/state 观察到 mode 由 full 变为 restricted；前端每 8 秒轮询同一接口，会同步刷新模式开关与提示。' }
                }
                return @{ Status = 'fail'; Actual = "等待 $elapsed 秒后 mode 仍为 $lastMode" }
            }
        }

        # ------------------------------------------------------------ 组 11 中止
        Write-Banner '组 11/19  中止与进程树终止'
        if (-not $canRunFlow) {
            foreach ($id in @('11.1', '11.2', '11.3')) {
                Skip-Step -Id $id -Name '中止测试' -Expected '按规范中止' -Reason '服务或会话不可用'
            }
        }
        else {
            $script:Events11 = $null
            $null = Step -Id '11.1' -Name 'POST /api/abort 返回 200' -Expected '中止接口受理，进程树被 kill' -Action {
                # 独立会话 + 受限模式（Start-Sleep 为 low 风险，无需确认即可自动执行）
                $script:SessionId = New-TestSession
                $null = Invoke-Api -Method 'POST' -Path '/api/mode' -Headers $script:AuthHeaders -Body @{ session_id = $script:SessionId; mode = 'restricted' }
                $script:LastAbortKilled = $false
                $script:Events11 = Invoke-SseChat -SessionId $script:SessionId `
                    -Message '跑一条耗时命令用于中止测试' -Mode 'restricted' -AbortOnCommandMs 1500
                if ($script:LastAbortStatus -eq 200 -and $script:LastAbortKilled) {
                    return @{ Status = 'pass'; Actual = 'abort 200 且 killed=true' }
                }
                return @{ Status = 'fail'; Actual = "abort HTTP=$($script:LastAbortStatus) killed=$($script:LastAbortKilled)" }
            }

            $null = Step -Id '11.2' -Name '命令进程被终止' -Expected 'result.exit_code 非 0 或标记 timed_out' -Action {
                if ($null -eq $script:Events11) { return @{ Status = 'skip'; Actual = '上一步未产生事件流' } }
                $results = @(Get-Events $script:Events11 'result')
                if ($results.Count -eq 0) { return @{ Status = 'fail'; Actual = '没有 result 事件（命令可能未启动）' } }
                $exitCode = Get-Prop (Get-Prop $results[0] 'data' $null) 'exit_code' 0
                $timedOut = Get-Prop (Get-Prop $results[0] 'data' $null) 'timed_out' $false
                if ([int]$exitCode -ne 0 -or $timedOut) {
                    return @{ Status = 'pass'; Actual = "exit_code=$exitCode timed_out=$timedOut" }
                }
                return @{ Status = 'fail'; Actual = "exit_code=$exitCode（进程可能已正常结束）" }
            }

            $null = Step -Id '11.3' -Name '收到 aborted 事件且会话复位' -Expected 'error.code=aborted，session.running=false' -Action {
                if ($null -eq $script:Events11) { return @{ Status = 'skip'; Actual = '上一步未产生事件流' } }
                $errors = @(Get-Events $script:Events11 'error')
                $aborted = @($errors | Where-Object { [string](Get-Prop (Get-Prop $_ 'data' $null) 'code' '') -eq 'aborted' })
                Start-Sleep -Milliseconds 800
                $stateResp = Invoke-Api -Method 'GET' -Path "/api/state?session_id=$($script:SessionId)" -Headers $script:AuthHeaders
                $running = Get-Prop $stateResp.Json 'running' $true
                if ($aborted.Count -ge 1 -and -not $running) {
                    return @{ Status = 'pass'; Actual = 'aborted 已推送，running=false' }
                }
                return @{ Status = 'fail'; Actual = "aborted=$($aborted.Count) running=$running" }
            }
        }

        # ------------------------------------------------------------ 组 12 纠错重试
        Write-Banner '组 12/19  JSON 纠错重试'
        if (-not $canRunFlow) {
            Skip-Step -Id '12.1' -Name '非法 JSON 纠错重试' -Expected '重试 2 次后返回 error' -Reason '服务或会话不可用'
        }
        elseif (-not $script:UseMock) {
            Skip-Step -Id '12.1' -Name '非法 JSON 纠错重试' -Expected '重试 2 次后返回 error' -Reason '该用例需要 mock 模型（去掉 -RealLlm 重跑）'
        }
        else {
            $null = Step -Id '12.1' -Name '非法 JSON 纠错重试' -Expected '出现 parse_retry，最多 2 次后返回 error' -Action {
                $script:SessionId = New-TestSession
                $events = Invoke-SseChat -SessionId $script:SessionId -Message '坏JSON测试：请随便说点什么' -Mode 'restricted'
                $errors = @(Get-Events $events 'error')
                $retries = @($errors | Where-Object { [string](Get-Prop (Get-Prop $_ 'data' $null) 'code' '') -eq 'parse_retry' })
                $final = @($errors | Where-Object { [string](Get-Prop (Get-Prop $_ 'data' $null) 'code' '') -eq 'parse_failed' })
                $done = Find-Event $events 'done'
                if ($retries.Count -ge 1 -and $retries.Count -le 2 -and $final.Count -eq 1 -and $null -ne $done) {
                    return @{ Status = 'pass'; Actual = "重试 $($retries.Count) 次后返回 parse_failed，服务未崩溃" }
                }
                return @{ Status = 'fail'; Actual = "retry=$($retries.Count) parse_failed=$($final.Count) done=$($null -ne $done)" }
            }
        }

        # ------------------------------------------------------------ 组 13 路径穿越
        Write-Banner '组 13/19  路径穿越拦截'
        if (-not $canRunFlow) {
            Skip-Step -Id '13.1' -Name '访问 C:\Windows 被拒绝' -Expected 'guard_denied' -Reason '服务或会话不可用'
            Skip-Step -Id '13.2' -Name '拒绝原因写入审计日志' -Expected '审计含该次 guard_verdict(allowed=false)' -Reason '服务或会话不可用'
        }
        else {
            $null = Step -Id '13.1' -Name '访问 C:\Windows 被拒绝' -Expected '返回 guard_denied，未执行命令' -Action {
                $script:SessionId = New-TestSession
                $events = Invoke-SseChat -SessionId $script:SessionId -Message '路径穿越测试：读取 C:\Windows\System32\drivers\etc\hosts' -Mode 'restricted'
                $errors = @(Get-Events $events 'error')
                $denied = @($errors | Where-Object { [string](Get-Prop (Get-Prop $_ 'data' $null) 'code' '') -eq 'guard_denied' })
                $results = @(Get-Events $events 'result')
                if ($denied.Count -ge 1 -and $results.Count -eq 0) {
                    $msg = [string](Get-Prop (Get-Prop $denied[0] 'data' $null) 'message' '')
                    return @{ Status = 'pass'; Actual = '被拒绝且未执行'; Detail = $msg }
                }
                if ($script:UseMock) { return @{ Status = 'fail'; Actual = "denied=$($denied.Count) executed=$($results.Count)" } }
                return @{ Status = 'skip'; Actual = '真实模型未尝试该路径，无法断言' }
            }

            $null = Step -Id '13.2' -Name '拒绝原因写入审计日志' -Expected '审计含该次 guard_verdict(allowed=false)' -Action {
                Start-Sleep -Milliseconds 300
                $logDir = Join-Path $script:Root 'logs'
                $file = Get-ChildItem -LiteralPath $logDir -Filter 'audit-*.jsonl' -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime | Select-Object -Last 1
                if ($null -eq $file) { return @{ Status = 'fail'; Actual = '未找到 audit-*.jsonl' } }
                $script:AuditFile = $file.FullName
                $text = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
                # json.dumps 默认分隔符是 ", " 和 ": "，这里用 \s* 兼容两种写法
                $hasVerdict = $text -match '"event":\s*"guard_verdict"'
                $hasDenied = $text -match '"allowed":\s*false'
                $hasWindows = $text -like '*C:\\Windows*'
                if ($hasVerdict -and $hasDenied -and $hasWindows) {
                    return @{ Status = 'pass'; Actual = '审计中记录了该次拒绝'; Detail = $file.Name }
                }
                return @{ Status = 'fail'; Actual = "guard_verdict=$hasVerdict allowed:false=$hasDenied C:\Windows=$hasWindows" }
            }
        }

        # ------------------------------------------------------------ 组 14 审计日志
        Write-Banner '组 14/19  审计日志检查'
        $null = Step -Id '14.1' -Name '审计日志包含关键事件' -Expected '含 guard_verdict / command_executed / user_confirm' -Action {
            $logDir = Join-Path $script:Root 'logs'
            $file = Get-ChildItem -LiteralPath $logDir -Filter 'audit-*.jsonl' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime | Select-Object -Last 1
            if ($null -eq $file) { return @{ Status = 'fail'; Actual = '未找到 audit-*.jsonl（logs 目录）' } }
            $script:AuditFile = $file.FullName
            $text = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
            $required = @('guard_verdict', 'command_executed', 'user_confirm', 'mode_changed')
            $missing = @()
            foreach ($needle in $required) {
                if ($text -notmatch [regex]::Escape('"' + $needle + '"')) { $missing += $needle }
            }
            if ($missing.Count -eq 0) {
                $count = ($text -split "`n" | Where-Object { $_.Trim() }).Count
                return @{ Status = 'pass'; Actual = "事件齐全（$count 条记录）"; Detail = $file.Name }
            }
            return @{ Status = 'fail'; Actual = '缺少事件：' + ($missing -join ', ') }
        }

        $null = Step -Id '14.2' -Name '审计日志不含密钥明文' -Expected '不含 api_key / Bearer 明文' -Action {
            $logDir = Join-Path $script:Root 'logs'
            $file = Get-ChildItem -LiteralPath $logDir -Filter 'audit-*.jsonl' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime | Select-Object -Last 1
            if ($null -eq $file) { return @{ Status = 'skip'; Actual = '无审计日志可检查' } }
            $text = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
            $leaks = @()
            if ($script:ApiKeyValue -and $text.Contains($script:ApiKeyValue)) { $leaks += 'LLM_API_KEY 明文' }
            if ($text -match 'sk-[A-Za-z0-9]{20,}') { $leaks += 'sk- 形态密钥' }
            if ($text -match '(?i)Bearer\s+[A-Za-z0-9._\-]{20,}') { $leaks += 'Bearer 令牌' }
            if ($script:Token -and $text.Contains($script:Token)) { $leaks += 'agent token' }
            if ($leaks.Count -eq 0) { return @{ Status = 'pass'; Actual = '未发现密钥明文' } }
            return @{ Status = 'fail'; Actual = '发现泄露：' + ($leaks -join ', ') }
        }

        # ------------------------------------------------------------ 组 16 扩展能力
        Write-Banner '组 16/19  扩展能力：压缩 / 解压 / 校验'
        if (-not $canRunFlow) {
            foreach ($id in @('16.1', '16.2', '16.3')) {
                Skip-Step -Id $id -Name '压缩/解压/校验能力' -Expected '按规范执行' -Reason '服务或会话不可用'
            }
        }
        else {
            $zipSrc = 'D:\AgentWork\verify_zip_src'
            $zipFile = 'D:\AgentWork\verify_src.zip'
            $zipOut = 'D:\AgentWork\verify_zip_out'

            $null = Step -Id '16.1' -Name 'Compress-Archive 压缩成功' -Expected 'zip 生成且命令退出码 0' -Action {
                # 夹具：准备源目录与文件（直接落盘，不经过 Agent）
                Remove-Item -LiteralPath $zipFile -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $zipSrc -Recurse -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $zipOut -Recurse -Force -ErrorAction SilentlyContinue
                $null = New-Item -ItemType Directory -Path $zipSrc -Force
                Set-Content -LiteralPath (Join-Path $zipSrc 'hello.txt') -Value 'EdgeAgent zip fixture' -Encoding UTF8

                $script:SessionId = New-TestSession
                $events = Invoke-SseChat -SessionId $script:SessionId -Message '压缩测试：把目录打包' -Mode 'restricted' -AutoApprove
                $results = @(Get-Events $events 'result')
                if ($results.Count -eq 0) { return @{ Status = 'fail'; Actual = '没有 result 事件' } }
                $exitCode = Get-Prop (Get-Prop $results[0] 'data' $null) 'exit_code' -1
                $exists = Test-Path -LiteralPath $zipFile
                if ($exitCode -eq 0 -and $exists) {
                    $sizeKb = [math]::Round((Get-Item -LiteralPath $zipFile).Length / 1KB, 1)
                    return @{ Status = 'pass'; Actual = "zip 已生成（$sizeKb KB）" }
                }
                return @{ Status = 'fail'; Actual = "exit_code=$exitCode zipExists=$exists" }
            }

            $null = Step -Id '16.2' -Name 'Expand-Archive 解压成功' -Expected '解压目录出现 hello.txt' -Action {
                $script:SessionId = New-TestSession
                $events = Invoke-SseChat -SessionId $script:SessionId -Message '解压测试：把 zip 解开' -Mode 'restricted' -AutoApprove
                $results = @(Get-Events $events 'result')
                if ($results.Count -eq 0) { return @{ Status = 'fail'; Actual = '没有 result 事件' } }
                $exitCode = Get-Prop (Get-Prop $results[0] 'data' $null) 'exit_code' -1
                $extracted = Join-Path $zipOut 'hello.txt'
                $exists = Test-Path -LiteralPath $extracted
                if ($exitCode -eq 0 -and $exists) {
                    return @{ Status = 'pass'; Actual = 'hello.txt 已解压到 verify_zip_out' }
                }
                return @{ Status = 'fail'; Actual = "exit_code=$exitCode 解压文件存在=$exists" }
            }

            $null = Step -Id '16.3' -Name 'Get-FileHash 返回 SHA256' -Expected '输出 64 位十六进制摘要' -Action {
                $script:SessionId = New-TestSession
                $events = Invoke-SseChat -SessionId $script:SessionId -Message '校验测试：算一下哈希' -Mode 'restricted'
                $results = @(Get-Events $events 'result')
                if ($results.Count -eq 0) { return @{ Status = 'fail'; Actual = '没有 result 事件' } }
                $stdout = [string](Get-Prop (Get-Prop $results[0] 'data' $null) 'stdout' '')
                if ($stdout -match '\b[0-9A-Fa-f]{64}\b') {
                    return @{ Status = 'pass'; Actual = '已返回 64 位 SHA256'; Detail = $stdout.Trim() }
                }
                return @{ Status = 'fail'; Actual = '输出中没有 64 位摘要'; Detail = $stdout.Trim() }
            }

            # 清理夹具（保留 verify_zip_out 供排查时可手动查看，这里统一清掉）
            Remove-Item -LiteralPath $zipSrc -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $zipOut -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $zipFile -Force -ErrorAction SilentlyContinue
        }

        # ------------------------------------------------------------ 组 17 RAG
        Write-Banner '组 17/19  本地知识库检索（RAG）'
        if (-not $script:ServerReady) {
            foreach ($id in @('17.1', '17.2', '17.3', '17.4')) {
                Skip-Step -Id $id -Name '知识库检索' -Expected '按规范返回' -Reason '服务未就绪'
            }
        }
        else {
            $kbDir = 'D:\AgentWork\verify_docs'
            $kbFile = Join-Path $kbDir 'verify_kb.txt'
            $null = New-Item -ItemType Directory -Path $kbDir -Force -ErrorAction SilentlyContinue
            Set-Content -LiteralPath $kbFile -Encoding UTF8 -Value @'
EdgeAgent 知识库验证文档。
唯一标记：ZQ-MARKER-8848。
内容约定：本文件用于验证本地 TF-IDF 检索是否命中，与业务无关。
'@

            $null = Step -Id '17.1' -Name 'POST /api/knowledge/reindex' -Expected '索引完成且文件数 >= 1' -Action {
                $resp = Invoke-Api -Method 'POST' -Path '/api/knowledge/reindex' -Headers $script:AuthHeaders -Body @{}
                if ($resp.Status -ne 200) { return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)：$($resp.Text)" } }
                $stats = Get-Prop $resp.Json 'stats' $null
                $files = [int](Get-Prop $stats 'indexed_files' 0)
                $chunks = [int](Get-Prop $stats 'chunks' 0)
                if ($files -ge 1 -and $chunks -ge 1) {
                    return @{ Status = 'pass'; Actual = "索引 $files 个文件 / $chunks 块" }
                }
                return @{ Status = 'fail'; Actual = "indexed_files=$files chunks=$chunks" }
            }

            $null = Step -Id '17.2' -Name '检索命中验证文档' -Expected 'results 含 verify_kb.txt，score > 0' -Action {
                $resp = Invoke-Api -Method 'POST' -Path '/api/knowledge/search' -Headers $script:AuthHeaders -Body @{ query = 'ZQ-MARKER-8848'; top_k = 3 }
                if ($resp.Status -ne 200) { return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)：$($resp.Text)" } }
                $results = @(Get-Prop $resp.Json 'results' @())
                if ($results.Count -lt 1) { return @{ Status = 'fail'; Actual = '没有任何命中' } }
                $hit = $results[0]
                $source = [string](Get-Prop $hit 'source' '')
                $score = [double](Get-Prop $hit 'score' 0)
                if ($source -like '*verify_kb.txt' -and $score -gt 0) {
                    return @{ Status = 'pass'; Actual = "命中 $(Split-Path $source -Leaf)（score=$score）" }
                }
                return @{ Status = 'fail'; Actual = "source=$source score=$score" }
            }

            $null = Step -Id '17.3' -Name '无关查询不误报' -Expected 'results 为空' -Action {
                $resp = Invoke-Api -Method 'POST' -Path '/api/knowledge/search' -Headers $script:AuthHeaders -Body @{ query = 'qqzzxx998877xyzzy'; top_k = 3 }
                if ($resp.Status -ne 200) { return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)" } }
                $results = @(Get-Prop $resp.Json 'results' @())
                if ($results.Count -eq 0) { return @{ Status = 'pass'; Actual = '未命中（符合预期）' } }
                return @{ Status = 'fail'; Actual = "误命中 $($results.Count) 条" }
            }

            $null = Step -Id '17.4' -Name '知识库接口受 Bearer 保护' -Expected '无 token 返回 401' -Action {
                $resp = Invoke-Api -Method 'POST' -Path '/api/knowledge/search' -Body @{ query = 'ZQ-MARKER-8848' }
                if ($resp.Status -eq 401) { return @{ Status = 'pass'; Actual = 'HTTP 401' } }
                return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)（预期 401）" }
            }

            $null = Step -Id '17.5' -Name '聊天时自动注入知识库上下文' -Expected 'SSE 出现 knowledge 事件后正常 done' -Action {
                $script:SessionId = New-TestSession
                $events = Invoke-SseChat -SessionId $script:SessionId -Message '请根据文档总结一下 ZQ-MARKER-8848 相关内容' -Mode 'restricted'
                $knowledge = @(Get-Events $events 'knowledge')
                $done = Find-Event $events 'done'
                if ($knowledge.Count -ge 1 -and $null -ne $done) {
                    $hits = @(Get-Prop (Get-Prop $knowledge[0] 'data' $null) 'hits' @())
                    $first = if ($hits.Count -gt 0) { Split-Path ([string](Get-Prop $hits[0] 'source' '')) -Leaf } else { '' }
                    return @{ Status = 'pass'; Actual = "注入 $($hits.Count) 段（首个：$first）" }
                }
                if (-not $script:UseMock) { return @{ Status = 'skip'; Actual = '真实模型未触发检索意图' } }
                return @{ Status = 'fail'; Actual = "knowledge=$($knowledge.Count) done=$($null -ne $done)" }
            }

            Remove-Item -LiteralPath $kbDir -Recurse -Force -ErrorAction SilentlyContinue
        }

        # ------------------------------------------------------------ 组 18 状态增强
        Write-Banner '组 18/19  系统状态感知增强'
        if (-not $canRunFlow) {
            foreach ($id in @('18.1', '18.2')) {
                Skip-Step -Id $id -Name '状态增强' -Expected '按规范返回' -Reason '服务或会话不可用'
            }
        }
        else {
            $null = Step -Id '18.1' -Name '状态含盘符/进程/网卡' -Expected 'system.disks、top_processes、network 存在' -Action {
                $resp = Invoke-Api -Method 'GET' -Path "/api/state?session_id=$($script:SessionId)" -Headers $script:AuthHeaders
                if ($resp.Status -ne 200) { return @{ Status = 'fail'; Actual = "HTTP $($resp.Status)" } }
                $system = Get-Prop $resp.Json 'system' $null
                $disks = @(Get-Prop $system 'disks' @())
                $procs = Get-Prop $system 'top_processes' $null
                $byCpu = @(Get-Prop $procs 'by_cpu' @())
                $net = @(Get-Prop $system 'network' @())
                if ($disks.Count -ge 1 -and $byCpu.Count -ge 1) {
                    $driveText = ($disks | ForEach-Object { [string](Get-Prop $_ 'drive' '') }) -join '/'
                    return @{ Status = 'pass'; Actual = "盘符 $driveText，进程 $($byCpu.Count) 条，网卡 $($net.Count) 条" }
                }
                return @{ Status = 'fail'; Actual = "disks=$($disks.Count) by_cpu=$($byCpu.Count)" }
            }

            $null = Step -Id '18.2' -Name '5 秒采集节流生效' -Expected '第二次调用 system.cached=true' -Action {
                Start-Sleep -Milliseconds 400
                $resp = Invoke-Api -Method 'GET' -Path "/api/state?session_id=$($script:SessionId)" -Headers $script:AuthHeaders
                $system = Get-Prop $resp.Json 'system' $null
                $cached = Get-Prop $system 'cached' $false
                if ($cached -eq $true) { return @{ Status = 'pass'; Actual = '命中缓存（5 秒窗口内不重复采集）' } }
                return @{ Status = 'fail'; Actual = '未命中缓存，可能重复采集' }
            }
        }

        # ------------------------------------------------------------ 组 19 桌面客户端
        Write-Banner '组 19/19  桌面客户端（launcher / 打包）'
        $launcherPath = Join-Path $script:Root 'launcher.py'
        $distExe = Join-Path $script:Root 'dist\EdgeAgent.exe'
        $clientPort = 8766

        $null = Step -Id '19.1' -Name 'launcher.py --check 自检通过' -Expected '退出码 0，JSON ok=true' -Action {
            if (-not (Test-Path -LiteralPath $launcherPath)) { return @{ Status = 'fail'; Actual = '缺少 launcher.py' } }
            # 用 8766 做端口自检（8765 正被本次验证的服务占用）
            $reportPath = Join-Path $script:Root 'logs\check_report.json'
            Remove-Item -LiteralPath $reportPath -Force -ErrorAction SilentlyContinue
            $result = Invoke-Native -Exe $script:PythonExe -Arguments @($launcherPath, '--check', '--port', "$clientPort") -WorkDir $script:Root

            # 以 launcher 落盘的 check_report.json 为准（windowed exe 没有 stdout，同样适用）
            if (-not (Test-Path -LiteralPath $reportPath)) {
                $tail = ($result.Output -split "`n" | Select-Object -Last 2) -join ' '
                return @{ Status = 'fail'; Actual = "未生成 check_report.json（exit=$($result.ExitCode)）"; Detail = $tail }
            }
            $json = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $ok = Get-Prop $json 'ok' $false
            $edge = Get-Prop $json 'edge' $null
            $edgeVersion = [string](Get-Prop $edge 'version' '')
            if ($ok -eq $true -and $result.ExitCode -eq 0) {
                $detail = "frozen=$(Get-Prop $json 'frozen' $false) python=$(Get-Prop $json 'python' '') edge=$edgeVersion data=$(Get-Prop $json 'data_dir' '')"
                return @{ Status = 'pass'; Actual = "自检通过（Edge $edgeVersion）"; Detail = $detail }
            }
            return @{ Status = 'fail'; Actual = "ok=$ok exit=$($result.ExitCode)"; Detail = [string](Get-Prop $json 'error' '') }
        }

        $clientProc = $null
        $clientReady = $false
        $null = Step -Id '19.2' -Name '客户端启动并在 5s 内就绪' -Expected '/healthz 返回 200（端口 8766）' -Action {
            # 清理可能残留的实例
            $existing = Get-NetTCPConnection -LocalPort $clientPort -State Listen -ErrorAction SilentlyContinue
            if ($null -ne $existing) {
                foreach ($conn in @($existing)) { $null = & taskkill.exe /F /T /PID $conn.OwningProcess 2>&1 }
                Start-Sleep -Milliseconds 800
            }
            Remove-Item -LiteralPath (Join-Path $script:Root 'logs\launcher_status.json') -Force -ErrorAction SilentlyContinue

            $launcherArgs = @($launcherPath, '--headless', '--port', "$clientPort", '--simulate-browser')
            $startParams = @{
                FilePath = $script:PythonExe
                ArgumentList = $launcherArgs
                WorkingDirectory = $script:Root
                PassThru = $true
                WindowStyle = 'Hidden'
            }
            try {
                $script:ClientProc = Start-Process @startParams -RedirectStandardOutput (Join-Path $script:Root 'logs\launcher.out.log') -RedirectStandardError (Join-Path $script:Root 'logs\launcher.err.log')
            }
            catch {
                $script:ClientProc = Start-Process @startParams
            }

            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            $ok = $false
            while ($sw.Elapsed.TotalSeconds -lt 5) {
                try {
                    $resp = Invoke-Api -Method 'GET' -Path '/healthz' -Headers @{} -BaseUrl "http://127.0.0.1:$clientPort"
                }
                catch {
                    $resp = $null
                }
                if ($null -ne $resp -and $resp.Status -eq 200) { $ok = $true; break }
                Start-Sleep -Milliseconds 250
            }
            $elapsed = [math]::Round($sw.Elapsed.TotalSeconds, 2)
            if ($ok) {
                $script:ClientReady = $true
                return @{ Status = 'pass'; Actual = "就绪耗时 ${elapsed}s"; Detail = "PID $($script:ClientProc.Id)" }
            }
            return @{ Status = 'fail'; Actual = "5 秒内未就绪（PID $($script:ClientProc.Id)）" }
        }

        # 注意：Invoke-Api 走的是 $script:BaseUrl（8765），客户端在 8766，因此用独立探测
        if ($clientReady) {
            $null = Step -Id '19.3' -Name 'UI 打开动作与状态文件' -Expected 'launcher_status.json 中 browser_opened=true' -Action {
                $statusPath = Join-Path $script:Root 'logs\launcher_status.json'
                $sw = [System.Diagnostics.Stopwatch]::StartNew()
                while (-not (Test-Path -LiteralPath $statusPath) -and $sw.Elapsed.TotalSeconds -lt 8) {
                    Start-Sleep -Milliseconds 300
                }
                if (-not (Test-Path -LiteralPath $statusPath)) { return @{ Status = 'fail'; Actual = '未生成 launcher_status.json' } }
                $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $opened = Get-Prop $status 'browser_opened' $false
                $mode = [string](Get-Prop $status 'mode' '')
                $portInStatus = Get-Prop $status 'port' 0
                if ($opened -eq $true -and $portInStatus -eq $clientPort) {
                    return @{ Status = 'pass'; Actual = "browser_opened=true（mode=$mode, port=$portInStatus）"; Detail = '模拟模式只记录动作，不真的拉起浏览器' }
                }
                return @{ Status = 'fail'; Actual = "browser_opened=$opened port=$portInStatus" }
            }

            $null = Step -Id '19.4' -Name '退出后端口释放' -Expected '终止客户端后 8766 不再监听' -Action {
                if ($null -eq $script:ClientProc) { return @{ Status = 'fail'; Actual = '没有客户端进程句柄' } }
                $clientPid = $script:ClientProc.Id
                $null = & taskkill.exe /F /T /PID $clientPid 2>&1
                $null = $script:ClientProc.WaitForExit(8000)
                $sw = [System.Diagnostics.Stopwatch]::StartNew()
                $released = $false
                while ($sw.Elapsed.TotalSeconds -lt 10) {
                    $conn = Get-NetTCPConnection -LocalPort $clientPort -State Listen -ErrorAction SilentlyContinue
                    if ($null -eq $conn) { $released = $true; break }
                    Start-Sleep -Milliseconds 300
                }
                $script:ClientProc = $null
                if ($released) { return @{ Status = 'pass'; Actual = "PID $clientPid 已退出，端口 $clientPort 已释放" } }
                return @{ Status = 'fail'; Actual = "端口 $clientPort 仍被占用" }
            }
        }
        else {
            Skip-Step -Id '19.3' -Name 'UI 打开动作与状态文件' -Expected 'browser_opened=true' -Reason '客户端未就绪'
            Skip-Step -Id '19.4' -Name '退出后端口释放' -Expected '端口释放' -Reason '客户端未启动'
        }

        if (Test-Path -LiteralPath $distExe) {
            $null = Step -Id '19.5' -Name '打包产物 EdgeAgent.exe 自检' -Expected 'exe --check 退出码 0' -Action {
                $result = Invoke-Native -Exe $distExe -Arguments @('--check') -WorkDir (Split-Path $distExe -Parent)
                $reportPath = Join-Path (Split-Path $distExe -Parent) 'logs\check_report.json'
                $ok = $false
                if (Test-Path -LiteralPath $reportPath) {
                    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
                    $ok = (Get-Prop $report 'ok' $false) -eq $true
                }
                if ($ok -and $result.ExitCode -eq 0) {
                    $sizeMb = [math]::Round((Get-Item -LiteralPath $distExe).Length / 1MB, 1)
                    return @{ Status = 'pass'; Actual = "exe 自检通过（$sizeMb MB）" }
                }
                return @{ Status = 'fail'; Actual = "exit=$($result.ExitCode) reportOk=$ok" }
            }
        }
        else {
            Skip-Step -Id '19.5' -Name '打包产物 EdgeAgent.exe 自检' -Expected 'exe --check 退出码 0' -Reason '未找到 dist\EdgeAgent.exe（先运行 python build.py）'
        }
    }
    # ---------------------------------------------------------------- 组 15 关闭
    Write-Banner '组 15/19  服务关闭'
    if ($null -ne $script:ServerProc) {
        $null = Step -Id '15.1' -Name '终止服务进程并释放端口' -Expected '进程退出，端口 $Port 释放' -Action {
            $serverPid = $script:ServerProc.Id
            Stop-EdgeAgent
            $released = $false
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            while ($sw.Elapsed.TotalSeconds -lt 10) {
                if (-not (Test-PortInUse -PortNumber $Port)) { $released = $true; break }
                Start-Sleep -Milliseconds 400
            }
            $exited = $true
            try { $exited = $script:ServerProc.HasExited } catch { $exited = $true }
            $script:ServerProc = $null
            if ($exited -and $released) {
                return @{ Status = 'pass'; Actual = "PID $serverPid 已退出，端口 $Port 已释放" }
            }
            return @{ Status = 'fail'; Actual = "exited=$exited released=$released" }
        }
    }
    else {
        Skip-Step -Id '15.1' -Name '终止服务进程并释放端口' -Expected '进程退出，端口释放' -Reason '本次未启动服务'
    }
}
catch {
    # 顶层兜底：任何意外都不吞掉报告
    Write-Host ''
    Write-Host ('脚本异常：' + $_.Exception.Message) -ForegroundColor Red
    Add-Result -Id '0.1' -Name '脚本执行未中断' -Expected '无未捕获异常' -Actual ('异常：' + $_.Exception.Message) -Status 'fail' -DurationMs 0 -Detail $_.ScriptStackTrace
}
finally {
    if (-not $KeepServer) {
        try { Stop-EdgeAgent } catch { }
    }
    try { Restore-Environment } catch { }

    # 清理验证过程中创建的文件
    $verifyFile = 'D:\AgentWork\verify_test.txt'
    if (Test-Path -LiteralPath $verifyFile) {
        try { Remove-Item -LiteralPath $verifyFile -Force -ErrorAction Stop } catch { }
    }

    # 填充报告用的环境信息
    $script:EnvInfo = [ordered]@{
        '项目目录'   = $script:Root
        '服务地址'   = $script:BaseUrl
        'Python'     = $script:PythonVersion
        '解释器'     = "$($script:PythonExe)  [$($script:PythonSource)]"
        'PowerShell' = $PSVersionTable.PSVersion.ToString()
        '模型提供方' = $(if ($script:UseMock) { 'mock（内置假模型）' } else { 'openai 兼容接口' })
        '模型'       = $(if ($script:UseMock) { 'edgeagent-mock' } else { '见 config.yaml' })
        '端口'       = [string]$Port
        '降级阈值'   = "$IdleDowngradeSeconds 秒（仅本次运行通过环境变量注入）"
        '会话 ID'    = $script:SessionId
        '审计日志'   = $script:AuditFile
    }

    $reportPath = ''
    try {
        $reportPath = New-HtmlReport
    }
    catch {
        Write-Host ('生成报告失败：' + $_.Exception.Message) -ForegroundColor Red
    }

    $pass = @($script:Results | Where-Object { $_.Status -eq 'pass' }).Count
    $fail = @($script:Results | Where-Object { $_.Status -eq 'fail' }).Count
    $skip = @($script:Results | Where-Object { $_.Status -eq 'skip' }).Count
    $total = $script:Results.Count
    $rate = if ($total -gt 0) { [math]::Round(($pass / $total) * 100, 1) } else { 0 }

    Write-Banner '验证完成'
    Write-Host ("  通过 $pass · 失败 $fail · 跳过 $skip · 通过率 $rate%") -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Yellow' })
    if ($reportPath) { Write-Host ("  HTML 报告：" + $reportPath) -ForegroundColor Cyan }
    if ($fail -gt 0) {
        Write-Host '  失败项：' -ForegroundColor Red
        foreach ($r in $script:Results) {
            if ($r.Status -eq 'fail') {
                Write-Host ("    [$($r.Id)] $($r.Name) -> $($r.Actual)") -ForegroundColor Red
            }
        }
    }
    if ($KeepServer -and $null -ne $script:ServerProc) {
        Write-Host ("  服务仍在运行（PID $($script:ServerProc.Id)），端口 $Port") -ForegroundColor Yellow
    }
}
