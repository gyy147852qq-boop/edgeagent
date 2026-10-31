"""模型指令解析器。

安全要点：
1. 模型输出永远只被当作"候选 JSON"处理，解析失败即报错，**绝不会**把裸文本当命令执行。
2. 解析过程容错（代码块缺失、多代码块、尾随逗号、智能引号、注释），但容错只针对 JSON 语法，
   不针对语义：action 不合法、command 为空一律拒绝。
3. 解析结果附带 warnings，便于前端与审计日志展示"模型输出不规范"的事实。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Final

VALID_ACTIONS: Final[set[str]] = {"run_powershell", "finish", "ask_user"}
VALID_RISKS: Final[set[str]] = {"low", "medium", "high"}

FENCE_RE = re.compile(r"```[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\r?\n(?P<body>.*?)```", re.DOTALL)
MAX_COMMAND_LENGTH: Final[int] = 4000


class ParseError(ValueError):
    """模型输出无法解析为合法指令。``hint`` 会被回传给模型做自我纠错。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint or message


@dataclass
class ParsedCommand:
    """解析后的单条指令。"""

    action: str
    command: str | None = None
    timeout: int = 30
    # risk 为 None 表示模型没给，交由 Guard 依据命令内容推断
    risk: str | None = None
    reason: str = ""
    question: str = ""
    summary: str = ""
    raw: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_public_dict(self) -> dict[str, Any]:
        """供 SSE / 审计使用的最小字段集合（不含模型原文）。"""
        return {
            "action": self.action,
            "command": self.command,
            "timeout": self.timeout,
            "risk": self.risk,
            "reason": self.reason,
        }


def _strip_comments(text: str) -> str:
    """去掉 JSON 里的 // 与 /* */ 注释（字符串内部不动）。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    quote = ""
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
            i += 1
            continue
        if ch in {'"', "'"}:
            in_string = True
            quote = ch
            out.append('"' if ch == "'" else ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_json(text: str) -> str:
    """常见模型 JSON 瑕疵修复：智能引号、BOM、尾随逗号、单引号包裹的键。"""
    s = text.strip().lstrip("\ufeff")
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    s = _strip_comments(s)
    # 末尾多余逗号
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def _balanced_objects(text: str) -> list[str]:
    """从任意文本中抽取所有顶层平衡的 {...} 片段（忽略字符串内的括号）。"""
    results: list[str] = []
    depth = 0
    start = -1
    in_string = False
    quote = ""
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
            continue
        if ch == '"':
            in_string = True
            quote = ch
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    results.append(text[start : i + 1])
                    start = -1
    return results


def _candidate_blocks(text: str) -> list[tuple[str, str]]:
    """按优先级收集候选 JSON 文本：json 代码块 → 任意代码块 → 裸文本中的平衡对象。"""
    fenced_json: list[tuple[str, str]] = []
    fenced_other: list[tuple[str, str]] = []
    for m in FENCE_RE.finditer(text):
        lang = (m.group("lang") or "").strip().lower()
        body = m.group("body")
        if lang in {"json", "json5", "javascript", "js", ""}:
            fenced_json.append((body, "codeblock"))
        else:
            fenced_other.append((body, "codeblock"))
    if fenced_json or fenced_other:
        return fenced_json + fenced_other
    return [(chunk, "inline") for chunk in _balanced_objects(text)]


def _load_first_object(text: str) -> tuple[dict[str, Any], list[str]]:
    """解析出第一个"像指令"的 JSON 对象，返回 (对象, 警告列表)。"""
    warnings: list[str] = []
    blocks = _candidate_blocks(text)
    if not blocks:
        raise ParseError(
            "模型输出中找不到 JSON 指令块",
            hint="必须把指令放在 ```json 代码块里，例如 {\"action\":\"run_powershell\", ...}",
        )

    parsed: list[dict[str, Any]] = []
    last_error = ""
    for body, _kind in blocks:
        candidate = _repair_json(body)
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = f"{exc.msg} (line {exc.lineno} col {exc.colno})"
            continue
        if isinstance(obj, dict) and ("action" in obj or "command" in obj):
            parsed.append(obj)
        elif isinstance(obj, dict):
            last_error = "JSON 对象缺少 action 字段"
            continue
        else:
            last_error = "JSON 顶层不是对象"

    if not parsed:
        raise ParseError(
            f"JSON 解析失败：{last_error or '没有可用的 JSON 对象'}",
            hint="只输出一个合法 JSON 对象，键用双引号，不要加注释和尾随逗号。",
        )

    if len(parsed) > 1:
        warnings.append(f"模型一次返回了 {len(parsed)} 个 JSON 指令，已只取第一个：一次只允许发一条指令")
    return parsed[0], warnings


def parse_model_output(raw: str, *, default_timeout: int = 30, max_timeout: int = 120) -> ParsedCommand:
    """把模型原始输出解析成 :class:`ParsedCommand`。

    :raises ParseError: 无法解析或语义非法（调用方应把 hint 回传模型重试）。
    """
    if not raw or not raw.strip():
        raise ParseError("模型返回为空", hint="请返回一个包含 action 字段的 JSON 代码块。")

    obj, warnings = _load_first_object(raw)

    action_raw = obj.get("action")
    if not isinstance(action_raw, str):
        raise ParseError("缺少字符串字段 action", hint=f"action 只能是 {sorted(VALID_ACTIONS)} 之一。")
    action = action_raw.strip().lower()
    if action not in VALID_ACTIONS:
        raise ParseError(
            f"未知 action: {action_raw!r}",
            hint=f"action 只能是 {sorted(VALID_ACTIONS)} 之一，其它动作不允许。",
        )

    # 兼容模型把 action 写成 run_powershell 但字段名用 cmd/script 的情况
    if action == "run_powershell":
        command = obj.get("command") or obj.get("cmd") or obj.get("script") or obj.get("powershell")
        if not isinstance(command, str) or not command.strip():
            raise ParseError(
                "action=run_powershell 但 command 缺失或为空",
                hint="必须提供非空字符串 command 字段，例如 \"Get-ChildItem D:\\\\AgentWork\"。",
            )
        command = command.strip()
        if len(command) > MAX_COMMAND_LENGTH:
            raise ParseError(
                f"command 长度 {len(command)} 超过上限 {MAX_COMMAND_LENGTH}",
                hint="请拆分成更小的单条命令。",
            )
        if "\x00" in command:
            raise ParseError("command 含非法空字符")

        timeout = obj.get("timeout", default_timeout)
        try:
            timeout_int = int(str(timeout).strip())
        except (TypeError, ValueError):
            timeout_int = default_timeout
            warnings.append(f"timeout={timeout!r} 非法，已使用默认 {default_timeout}s")
        if timeout_int < 1:
            timeout_int = 1
        if timeout_int > max_timeout:
            warnings.append(f"timeout={timeout_int}s 超过上限，已收敛为 {max_timeout}s")
            timeout_int = max_timeout

        risk_raw = obj.get("risk")
        risk: str | None
        if isinstance(risk_raw, str) and risk_raw.strip().lower() in VALID_RISKS:
            risk = risk_raw.strip().lower()
        elif risk_raw is None:
            risk = None
            warnings.append("模型未给出 risk，改由权限校验器推断")
        else:
            risk = None
            warnings.append(f"risk={risk_raw!r} 非法，改由权限校验器推断")

        reason = obj.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""

        return ParsedCommand(
            action=action,
            command=command,
            timeout=timeout_int,
            risk=risk,
            reason=reason or "模型未说明原因",
            raw=raw,
            warnings=warnings,
        )

    if action == "finish":
        summary = obj.get("summary") or obj.get("message") or ""
        summary = summary.strip() if isinstance(summary, str) else ""
        return ParsedCommand(
            action=action,
            summary=summary or "任务结束",
            reason="",
            raw=raw,
            warnings=warnings,
        )

    # ask_user
    question = obj.get("question") or obj.get("message") or obj.get("prompt") or ""
    question = question.strip() if isinstance(question, str) else ""
    if not question:
        raise ParseError(
            "action=ask_user 但 question 为空",
            hint="ask_user 必须带非空 question 字段，向用户提出一个明确问题。",
        )
    return ParsedCommand(
        action=action,
        question=question,
        summary=question,
        reason="",
        raw=raw,
        warnings=warnings,
    )
