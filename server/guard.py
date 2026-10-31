"""权限校验器（PermissionGuard）。

三层防线：
1. 高危黑名单：任何模式下命中即拒绝，**不可**通过人工确认绕过。
2. 模式白名单：限制访问模式只允许只读 + 有限写操作指令集。
3. 路径校验：所有路径 realpath 规范化后必须落在 allowed_dirs 内，且命中敏感目录直接拒绝，
   以此阻断 ``..\\..\\Windows`` 与符号链接逃逸。

校验结果包含最终风险等级与是否需要人工确认，供 ConfirmLayer 使用。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Final, Iterable

from .config import Config

RISK_RANK: Final[dict[str, int]] = {"low": 0, "medium": 1, "high": 2}
RANK_RISK: Final[dict[int, str]] = {0: "low", 1: "medium", 2: "high"}


@dataclass
class Verdict:
    """单条命令的校验结论。"""

    allowed: bool
    risk: str
    require_confirm: bool
    reason: str
    violations: list[str] = field(default_factory=list)
    # 规范化后参与校验的路径，便于审计追溯
    checked_paths: list[str] = field(default_factory=list)

    @property
    def risk_rank(self) -> int:
        return RISK_RANK.get(self.risk, 1)


# ---------------------------------------------------------------------------
# 命令 / 别名表
# ---------------------------------------------------------------------------

#: 限制访问模式白名单（需求指定 + 少量必需的管道辅助命令）
WHITELIST_RESTRICTED: Final[frozenset[str]] = frozenset(
    {
        # 需求指定白名单
        "get-childitem",
        "get-item",
        "test-path",
        "get-content",
        "select-string",
        "get-process",
        "get-location",
        "set-location",
        "measure-object",
        "sort-object",
        "group-object",
        "copy-item",
        "new-item",
        "rename-item",
        "write-output",
        "get-date",
        # 管道/格式化辅助（无副作用，缺了它们白名单几乎不可用）
        "select-object",
        "where-object",
        "foreach-object",
        "format-table",
        "format-list",
        "out-string",
        "out-host",
        "convertto-json",
        "convertfrom-json",
        "join-path",
        "split-path",
        "resolve-path",
        "compare-object",
        "get-command",
        "get-member",
        "get-help",
        "start-sleep",
        # ---- M10 扩展：办公高频能力（全部仍需过路径校验与风险分级）----
        "expand-archive",       # 解压（medium，需确认；目标路径必须在 allowed_dirs 内）
        "compress-archive",     # 压缩（medium，需确认）
        "get-filehash",         # 校验（low，只读）
        "set-content",          # 写文本（medium，需确认；禁止写可执行脚本后缀）
        "test-netconnection",   # 网络探测（low，替代被禁用的 curl/wget）
        "get-psdrive",          # 盘符用量（low，只读；回答"C 盘还剩多少"这类问题）
        "get-volume",           # 卷信息（low，只读）
    }
)

#: 只读命令集合（用于风险推断为 low）
READ_ONLY_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        "get-childitem",
        "get-item",
        "test-path",
        "get-content",
        "select-string",
        "get-process",
        "get-location",
        "measure-object",
        "sort-object",
        "group-object",
        "get-date",
        "write-output",
        "select-object",
        "where-object",
        "format-table",
        "format-list",
        "out-string",
        "out-host",
        "convertto-json",
        "convertfrom-json",
        "join-path",
        "split-path",
        "resolve-path",
        "compare-object",
        "get-command",
        "get-member",
        "get-help",
        "get-filehash",
        "get-acl",
        "get-service",
        "get-volume",
        "get-psdrive",
        "get-childitem",
        # Start-Sleep 无任何副作用，归为只读（风险 low，可自动执行）
        "start-sleep",
        # M10：网络探测为只读
        "test-netconnection",
    }
)

#: 写操作（风险至少 medium）
WRITE_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        "copy-item",
        "new-item",
        "rename-item",
        "move-item",
        "set-location",
        "set-content",
        "add-content",
        "out-file",
        "new-itemproperty",
        "set-item",
        "set-itemproperty",
        "clear-content",
        "stop-process",
        "start-process",
        "compress-archive",
        "expand-archive",
    }
)

#: 高风险（删除 / 下载 / 执行 / 系统配置），仅完全访问模式允许
HIGH_RISK_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        "remove-item",
        "remove-itemproperty",
        "clear-recyclebin",
        "invoke-webrequest",
        "invoke-restmethod",
        "start-bitstransfer",
        "stop-process",
        "start-process",
        "set-itemproperty",
        "new-service",
        "set-service",
        "set-mppreference",
        "add-mppreference",
        "invoke-expression",
        "invoke-command",
        "set-executionpolicy",
    }
)

#: 绝对黑名单：任何模式、任何确认都无法执行
HARD_DENY_CMDLETS: Final[frozenset[str]] = frozenset(
    {
        # 磁盘 / 分区 / 引导
        "format-volume",
        "clear-disk",
        "initialize-disk",
        "new-partition",
        "set-disk",
        "diskpart",
        "bcdedit",
        "bootrec",
        "repair-volume",
        # 关机 / 重启 / 注销
        "stop-computer",
        "restart-computer",
        "shutdown",
        "logoff",
        # 安全软件 / 策略
        "set-mppreference",
        "add-mppreference",
        "remove-mppreference",
        "update-mppreference",
        "set-executionpolicy",
        # 远程执行 / 反弹 / 计划任务 / 服务
        "invoke-expression",
        "invoke-command",
        "start-job",
        "register-scheduledtask",
        "unregister-scheduledtask",
        "new-service",
        "set-service",
        "remove-service",
        # 权限与账户
        "runas",
        "new-localuser",
        "add-localgroupmember",
        "enable-localuser",
        "set-acl",
        "takeown",
        "icacls",
        # 其它系统级工具
        "net",
        "reg",
        "sc",
        "schtasks",
        "wmic",
        "cipher",
        "vssadmin",
        "wevtutil",
        "netsh",
        "add-type",
    }
)

#: 命令别名 → 标准 cmdlet
ALIASES: Final[dict[str, str]] = {
    "gci": "get-childitem",
    "ls": "get-childitem",
    "dir": "get-childitem",
    "gi": "get-item",
    "cat": "get-content",
    "gc": "get-content",
    "type": "get-content",
    "sls": "select-string",
    "ps": "get-process",
    "gps": "get-process",
    "gl": "get-location",
    "pwd": "get-location",
    "cd": "set-location",
    "chdir": "set-location",
    "sl": "set-location",
    "pushd": "push-location",
    "popd": "pop-location",
    "measure": "measure-object",
    "sort": "sort-object",
    "group": "group-object",
    "select": "select-object",
    "where": "where-object",
    "?": "where-object",
    "%": "foreach-object",
    "foreach": "foreach-object",
    "cp": "copy-item",
    "copy": "copy-item",
    "cpi": "copy-item",
    "ni": "new-item",
    "mkdir": "new-item",
    "md": "new-item",
    "ren": "rename-item",
    "rni": "rename-item",
    "mv": "move-item",
    "move": "move-item",
    "mi": "move-item",
    "echo": "write-output",
    "write": "write-output",
    "sc": "set-content",
    "ac": "add-content",
    "rm": "remove-item",
    "del": "remove-item",
    "erase": "remove-item",
    "ri": "remove-item",
    "rd": "remove-item",
    "rmdir": "remove-item",
    "clc": "clear-recyclebin",
    "kill": "stop-process",
    "spps": "stop-process",
    "saps": "start-process",
    "start": "start-process",
    "iex": "invoke-expression",
    "irm": "invoke-restmethod",
    "iwr": "invoke-webrequest",
    "curl": "invoke-webrequest",
    "wget": "invoke-webrequest",
    "icm": "invoke-command",
    "sp": "set-itemproperty",
    "si": "set-item",
    "sleep": "start-sleep",
    "tnc": "test-netconnection",
    "epcsv": "export-csv",
}

#: 正则型硬拒绝规则（命中即拒绝，与模式无关）
HARD_DENY_PATTERNS: Final[list[tuple[str, str]]] = [
    (r"(?i)-enc(?:odedcommand)?\s+[A-Za-z0-9+/=]{16,}", "禁止使用 -EncodedCommand（编码命令可绕过所有可见规则）"),
    (r"(?i)\bfrombase64string\b", "禁止 Base64 解码后执行"),
    (r"(?i)\bdownloadstring\b|\bdownloadfile\b|\bdownloaddata\b", "禁止下载远程脚本内容"),
    (r"(?i)\bstart-bitstransfer\b", "禁止使用 BITS 传输（下载执行链的一环）"),
    (r"(?i)\b(?:invoke-expression|iex)\b", "命中黑名单：Invoke-Expression / iex"),
    (r"(?i)\b(?:invoke-command|icm)\b", "命中黑名单：Invoke-Command（远程执行）"),
    (r"(?i)\badd-type\b", "禁止动态编译/加载代码（Add-Type）"),
    (r"(?i)\bset-executionpolicy\b", "禁止修改 PowerShell 执行策略"),
    (r"(?i)\b(?:set|add|remove|update)-mppreference\b", "禁止修改 Windows Defender 配置"),
    (r"(?i)\bnet\s+(?:user|localgroup)\b", "禁止账户/组操作（net user / net localgroup）"),
    (r"(?i)\breg\s+(?:add|delete|import|copy)\b", "禁止注册表写入（reg add/delete/import）"),
    (r"(?i)\bvssadmin\b|\bbcdedit\b|\bcipher\s+/w\b", "禁止引导/卷影/擦除类命令"),
    (r"(?i)\bschtasks\b", "禁止创建计划任务（持久化）"),
    (r"(?i)(?:set|new|remove)-itemproperty[^\n]{0,200}(?:hklm:|hkcu:|registry::)", "禁止写注册表（Set-ItemProperty 指向注册表）"),
    (r"(?i)\bstart-process\b[^\n]{0,80}-verb\s+runas", "禁止提权启动（Start-Process -Verb RunAs）"),
    (r"(?i)\brunas(?:\.exe)?\b", "禁止提权执行（runas）"),
    (r"(?i)\bwmic\b", "禁止使用 wmic"),
    (r"(?i)system\.diagnostics\.process", "禁止直接操作进程对象（可绕过 Stop-Process 限制）"),
    (r"(?i)\\\.\\pipe\\", "禁止访问命名管道"),
    (r"(?i)\b(?:new|register)-scheduledtask\b", "禁止创建计划任务（持久化）"),
    (r"(?i)\bRemove-Item\b[^\n]{0,200}-recurse[^\n]{0,80}-force[^\n]{0,200}(?:c:\\windows|program files|appdata|system32|systemdrive|\$env:systemroot)", "禁止递归强删系统目录"),
    (r"(?i)\bRemove-Item\s+['\"]?[A-Za-z]:[\\/]?['\"]?\s*(?:$|[;|])", "禁止删除驱动器根目录"),
]

#: 敏感路径标记（规范化小写路径匹配）
SENSITIVE_PATH_PATTERNS: Final[tuple[str, ...]] = (
    r"^c:\\windows(?:\\|$)",
    r"^c:\\program files(?: \(x86\))?(?:\\|$)",
    r"^c:\\programdata(?:\\|$)",
    r"^c:\\\$recycle",
    r"\\appdata(?:\\|$)",
    r"\\\.ssh(?:\\|$)",
    r"\\\.aws(?:\\|$)",
    r"\\\.config(?:\\|$)",
    r"\\system32(?:\\|$)",
    r"\\syswow64(?:\\|$)",
    r"\\system volume information",
    r"\\windows\\system32\\config",
    r"^\\\\\?\\",
    r"^\\\\\.\\",
)

#: 命令原文里出现这些字样也算触碰敏感区域（防止路径未被解析出来）
SENSITIVE_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(c:\\windows\b|\\windows\\system32|c:\\program files|appdata|\\\.ssh\b|\\\.aws\b|\\\.config\b|"
    r"hklm:|hkcu:|registry::|\\\.\\pipe\\|sam\b\\system32)"
)

DRIVE_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/][^\"'|<>;,\r\n]*)")
UNC_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(\\\\[A-Za-z0-9_.$\-]+\\[^\"'|<>;,\r\n]*)")
REL_PATH_RE: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])(\.\.?[\\/][^\s\"'|<>;,]*)")
LOCATION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(?:set-location|push-location|cd|chdir|sl)\s+(?P<arg>[^\s;|&]+)"
)
CMDLET_RE: Final[re.Pattern[str]] = re.compile(r"\b([A-Za-z][A-Za-z0-9]*)-([A-Za-z][A-Za-z0-9]*)\b")
REDIRECT_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9>])>(?![>&])")

#: 环境变量引用：``$env:NAME`` / ``${env:NAME}`` / ``%NAME%``
#: 这类写法会让路径校验"看不见"真实路径（例如 "$env:USERPROFILE\Desktop"），必须先展开再校验
ENV_REF_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}|\$env:([A-Za-z_][A-Za-z0-9_]*)|%([A-Za-z_][A-Za-z0-9_]*)%"
)

#: 可执行/脚本类后缀：写入这类文件等价于"落地可执行代码"，属于持久化面
SCRIPT_EXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\.(ps1|psm1|psd1|bat|cmd|vbs|vbe|js|jse|wsf|wsh|hta|lnk|scr|msi|exe|dll)$"
)
#: 具有"写文件"语义的命令
WRITE_INTENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(set-content|add-content|out-file|tee-object|new-item|copy-item|move-item|rename-item|"
    r"expand-archive|compress-archive|export-csv|export-clixml)\b"
)


def expand_env_refs(text: str) -> tuple[str, list[str]]:
    """把命令里的环境变量引用展开成真实路径，返回 (展开后文本, 无法解析的变量名)。

    这是路径校验的前置步骤：``"$env:USERPROFILE\\Desktop"`` 如果不展开，
    ``extract_paths`` 一个路径都抽不到，allowed_dirs 限制形同虚设。
    """
    unknown: list[str] = []
    lowered = {key.lower(): value for key, value in os.environ.items()}

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or match.group(3) or ""
        value = os.environ.get(name) or lowered.get(name.lower())
        if value is None:
            unknown.append(name)
            return match.group(0)
        return value

    return ENV_REF_RE.sub(_replace, text), unknown


def _strip_quoted(text: str) -> str:
    """把引号内的内容替换为空格，仅用于 cmdlet 扫描，避免字符串里的词被误判。"""
    out: list[str] = []
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
                out.append(" ")
            else:
                out.append(" ")
            continue
        if ch in {'"', "'"}:
            quote = ch
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def normalize_cmdlet(token: str) -> str:
    """把别名/size 变体归一成小写标准 cmdlet 名。"""
    name = token.strip().strip("(){}$&.;|").lower()
    return ALIASES.get(name, name)


def _segments(command: str) -> list[str]:
    """按 ``;`` ``|`` ``&&`` ``||`` 换行切分语句，但跳过括号与字符串内部。

    只看"顶层"分隔符很关键：``Select-Object @{n='MB';e={[math]::Round($_.Length/1MB,2)}}``
    里的 ``;`` 是哈希表项分隔符，``"a|b"`` 里的 ``|`` 是字面量。若简单按字符切分，
    ``e={...}`` 会变成一条"语句"，被判成无法识别的命令而**误拒**正常的只读查询。
    括号内部的语句仍会被 :func:`all_cmdlets` 用正则扫到，因此不会漏检危险命令。
    """
    segments: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    total = len(command)

    def flush() -> None:
        text = "".join(buf).strip()
        if text:
            segments.append(text)
        buf.clear()

    while i < total:
        ch = command[i]

        if quote is not None:  # 字符串内部：原样保留，只找配对的引号
            buf.append(ch)
            if ch == "`" and i + 1 < total:  # 反引号转义下一个字符
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue

        if ch == "`" and i + 1 < total:  # 裸反引号转义
            buf.append(command[i : i + 2])
            i += 2
            continue

        if ch == "#" and (not buf or buf[-1].isspace()):  # 行注释
            while i < total and command[i] not in "\r\n":
                i += 1
            continue

        if ch in "([{":
            depth += 1
            buf.append(ch)
            i += 1
            continue

        if ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue

        if depth == 0 and ch in ";|\r\n":
            if command.startswith("||", i) or command.startswith("&&", i):
                i += 2
            else:
                i += 1
            flush()
            continue

        buf.append(ch)
        i += 1

    flush()
    return segments


#: 兜底：即便有片段漏网（例如哈希表项 ``e={...}``），也不能当作"未知命令"拒绝。
EXPR_FRAGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def leading_cmdlets(command: str) -> list[str]:
    """取每条语句的首个命令名（已归一化）。"""
    result: list[str] = []
    for seg in _segments(command):
        tokens = seg.strip().split()
        if not tokens:
            continue
        first = tokens[0]
        lower = first.lower().strip("(){}$")
        if lower in {"if", "foreach", "for", "while", "do", "switch", "try", "elseif"}:
            continue
        # 表达式片段（哈希表项 e={...}、闭合括号）不是命令
        if first.startswith(("}", "{")) or EXPR_FRAGMENT_RE.match(first):
            continue
        result.append(normalize_cmdlet(first))
    return result


def all_cmdlets(command: str) -> set[str]:
    """扫描全部 Verb-Noun 形式的命令名（可发现脚本块里嵌套的调用）。"""
    names: set[str] = set()
    for m in CMDLET_RE.finditer(command):
        names.add(f"{m.group(1)}-{m.group(2)}".lower())
    for name in leading_cmdlets(command):
        if "-" in name:
            names.add(name)
    return names


def extract_paths(command: str, cwd: str) -> list[str]:
    """从命令中抽取候选路径并返回 realpath 规范化后的绝对路径。"""
    raw_candidates: list[str] = []
    for regex in (DRIVE_PATH_RE, UNC_PATH_RE, REL_PATH_RE):
        raw_candidates.extend(m.group(1) for m in regex.finditer(command))
    for m in LOCATION_RE.finditer(command):
        arg = m.group("arg")
        if arg.startswith("-"):
            continue
        raw_candidates.append(arg)

    resolved: list[str] = []
    seen: set[str] = set()
    for cand in raw_candidates:
        cleaned = cand.strip().strip("\"'").rstrip(")").strip()
        if not cleaned or cleaned in {"-", "|"}:
            continue
        if cleaned.lower().startswith(("http://", "https://")):
            continue
        try:
            absolute = cleaned if os.path.isabs(cleaned) else os.path.join(cwd, cleaned)
            real = os.path.realpath(absolute)
        except OSError:
            continue
        key = os.path.normcase(real)
        if key not in seen:
            seen.add(key)
            resolved.append(real)
    return resolved


def is_sensitive_path(path: str) -> bool:
    """判断路径是否落在敏感目录（Windows / Program Files / AppData / .ssh ...）。"""
    lowered = os.path.normcase(os.path.normpath(path)).replace("/", "\\")
    for pattern in SENSITIVE_PATH_PATTERNS:
        if re.search(pattern, lowered):
            return True
    return False


def is_within(path: str, roots: Iterable[str]) -> bool:
    """判断路径是否位于任一允许目录之下（大小写不敏感，含路径分隔符边界）。"""
    target = os.path.normcase(os.path.normpath(path))
    for root in roots:
        base = os.path.normcase(os.path.normpath(root))
        if target == base or target.startswith(base + os.sep):
            return True
    return False


class PermissionGuard:
    """白名单 / 黑名单 / 路径 / 风险分级校验器。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    # ---------------- 对外接口 ----------------
    def evaluate(
        self,
        command: str,
        *,
        mode: str,
        cwd: str,
        declared_risk: str | None = None,
    ) -> Verdict:
        """校验一条命令，返回包含风险等级与确认要求的结论。"""
        mode = "full" if mode == "full" else "restricted"
        declarations: list[str] = []
        violations: list[str] = []

        stripped = _strip_quoted(command)
        cmdlets = all_cmdlets(stripped)
        leads = leading_cmdlets(stripped)
        # 同时检查 Verb-Noun 形式与单段命令（shutdown、diskpart、reg、net 等原生工具）
        inspect_names = cmdlets | set(leads)

        risk = self._compute_risk(command, inspect_names, declared_risk)

        # 1) 绝对黑名单
        for name in sorted(inspect_names):
            if name in HARD_DENY_CMDLETS:
                violations.append(f"命中高危黑名单命令：{name}")
        for pattern, reason in HARD_DENY_PATTERNS:
            if re.search(pattern, command):
                violations.append(reason)

        # 2) 路径校验（先展开 $env: / %VAR%，否则真实路径对校验器不可见）
        expanded, unknown_env = expand_env_refs(command)
        if unknown_env:
            names = ", ".join(sorted(set(unknown_env)))
            if mode == "restricted":
                violations.append(f"无法解析的环境变量（可能用于绕过路径限制）：{names}")
            else:
                risk = "high"
        paths = extract_paths(expanded, cwd)
        path_violations = self._check_paths(paths, expanded, mode=mode, risk=risk)
        violations.extend(path_violations)

        # 3) 写入可执行脚本后缀：受限模式直接拒绝，完全模式升级为 high 并强制确认
        if self._writes_script_file(expanded, paths):
            if mode == "restricted":
                violations.append("禁止写入可执行脚本文件（.ps1/.bat/.cmd/.vbs/.hta/.lnk 等，持久化风险）")
            else:
                risk = "high"

        # 4) 模式白名单
        if mode == "restricted":
            for name in leads:
                if "-" not in name:
                    # 非 Verb-Noun 形式（表达式、裸变量、别名未识别）一律拒绝
                    violations.append(f"受限模式无法识别的命令：{name!r}（仅允许白名单 cmdlet）")
                elif name not in WHITELIST_RESTRICTED:
                    violations.append(f"受限模式白名单外的命令：{name}")
            for name in sorted(cmdlets):
                if name not in WHITELIST_RESTRICTED:
                    violations.append(f"受限模式白名单外的命令：{name}")
            if RISK_RANK.get(risk, 1) >= 2:
                violations.append("高风险命令仅完全访问模式允许")

        # 4) 去重后给出结论
        unique: list[str] = []
        for item in violations:
            if item not in unique:
                unique.append(item)

        if unique:
            return Verdict(
                allowed=False,
                risk=risk,
                require_confirm=False,
                reason="；".join(unique[:4]),
                violations=unique,
                checked_paths=paths,
            )

        require_confirm = self._need_confirm(risk, mode)
        reason = self._explain(risk, mode, require_confirm)
        declarations.append(f"风险等级={risk}")
        return Verdict(
            allowed=True,
            risk=risk,
            require_confirm=require_confirm,
            reason=reason,
            violations=declarations,
            checked_paths=paths,
        )

    # ---------------- 内部实现 ----------------
    @staticmethod
    def _writes_script_file(command: str, paths: list[str]) -> bool:
        """判断这条命令是否在写可执行脚本文件（防止模型落地自启动脚本）。"""
        if not WRITE_INTENT_RE.search(command):
            return False
        return any(SCRIPT_EXT_RE.search(path) for path in paths)

    def _check_paths(self, paths: list[str], command: str, *, mode: str, risk: str) -> list[str]:
        """路径越界与敏感目录检查。"""
        issues: list[str] = []
        allowed = self._cfg.security.allowed_dirs
        risk_rank = RISK_RANK.get(risk, 1)

        for path in paths:
            if is_sensitive_path(path):
                # 受限模式：任何触碰都拒绝；完全模式：仅写类操作拒绝（只读仍可用）
                if mode == "restricted" or risk_rank >= 1:
                    issues.append(f"路径命中敏感目录，禁止访问：{path}")

        if mode == "restricted":
            for path in paths:
                if not is_within(path, allowed):
                    issues.append(f"路径超出允许目录：{path}")

        # 命令原文里出现敏感字样但没解析出路径（例如用变量拼接），同样拦截
        if SENSITIVE_TEXT_RE.search(command):
            if mode == "restricted" or risk_rank >= 1:
                if not any("敏感目录" in i for i in issues):
                    issues.append("命令文本引用了系统敏感路径/注册表 hive")
        return issues

    def _compute_risk(self, command: str, names: set[str], declared: str | None) -> str:
        """结合命令内容与模型自述，取更严格的风险等级。"""
        rank = 0
        for name in names:
            if name in HARD_DENY_CMDLETS or name in HIGH_RISK_CMDLETS:
                rank = max(rank, 2)
            elif name in WRITE_CMDLETS:
                rank = max(rank, 1)
            elif name not in READ_ONLY_CMDLETS:
                # 未知命令按写操作对待（保守策略）
                rank = max(rank, 1)

        if REDIRECT_RE.search(command):
            rank = max(rank, 1)
        if re.search(r"(?i)\b(?:copy-item|new-item|rename-item|move-item|set-content|add-content|out-file)\b", command):
            rank = max(rank, 1)
        if re.search(r"(?i)\b-newer\b|\b-recurse\b", command) and re.search(
            r"(?i)\b(?:remove-item|copy-item|move-item)\b", command
        ):
            rank = max(rank, 1)

        declared_name = (declared or "").strip().lower()
        if declared_name in RISK_RANK:
            rank = max(rank, RISK_RANK[declared_name])
        return RANK_RISK[rank]

    def _need_confirm(self, risk: str, mode: str) -> bool:
        """按模式与风险等级决定是否需要人工确认。"""
        if mode == "restricted":
            return risk in self._cfg.security.confirm_risk_levels
        policy = self._cfg.security.full_mode_confirm
        if policy == "all":
            return True
        if policy == "medium_high":
            return risk in {"medium", "high"}
        return False

    @staticmethod
    def _explain(risk: str, mode: str, require_confirm: bool) -> str:
        mode_text = "限制访问模式" if mode == "restricted" else "完全访问模式"
        if require_confirm:
            return f"{mode_text}：{risk} 风险，需人工确认后执行"
        return f"{mode_text}：{risk} 风险，可自动执行"
