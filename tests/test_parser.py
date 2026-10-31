"""Parser 单元测试：覆盖正常解析与各种"模型不听话"的容错场景。"""

from __future__ import annotations

import json

import pytest

from server.parser import ParseError, parse_model_output


def _block(payload: dict, lang: str = "json") -> str:
    return f"```{lang}\n{json.dumps(payload, ensure_ascii=False)}\n```"


def test_parse_plain_json_block() -> None:
    raw = (
        "先看看目录里有什么。\n"
        + _block(
            {
                "action": "run_powershell",
                "command": "Get-ChildItem D:\\AgentWork -Filter *.pdf",
                "timeout": 15,
                "risk": "low",
                "reason": "列出目标目录中的 PDF 文件",
            }
        )
    )
    parsed = parse_model_output(raw)
    assert parsed.action == "run_powershell"
    assert parsed.command == "Get-ChildItem D:\\AgentWork -Filter *.pdf"
    assert parsed.timeout == 15
    assert parsed.risk == "low"
    assert parsed.reason == "列出目标目录中的 PDF 文件"
    assert parsed.warnings == []


def test_parse_uses_first_of_multiple_blocks_and_warns() -> None:
    raw = _block({"action": "run_powershell", "command": "Get-Location", "risk": "low"}) + "\n" + _block(
        {"action": "finish", "summary": "完成"}
    )
    parsed = parse_model_output(raw)
    assert parsed.action == "run_powershell"
    assert parsed.command == "Get-Location"
    assert any("一次只允许发一条指令" in w for w in parsed.warnings)


def test_parse_without_code_fence_falls_back_to_inline_object() -> None:
    raw = '好的，我执行：{"action": "run_powershell", "command": "Get-Date", "risk": "low"} 请稍候。'
    parsed = parse_model_output(raw)
    assert parsed.action == "run_powershell"
    assert parsed.command == "Get-Date"


def test_parse_repairs_trailing_comma_and_smart_quotes() -> None:
    raw = '```json\n{\u201caction\u201d: \u201crun_powershell\u201d, \u201ccommand\u201d: \u201cGet-Location\u201d, \u201crisk\u201d: \u201clow\u201d,}\n```'
    parsed = parse_model_output(raw)
    assert parsed.action == "run_powershell"
    assert parsed.risk == "low"


def test_parse_raises_when_no_json_present() -> None:
    with pytest.raises(ParseError) as excinfo:
        parse_model_output("我建议你直接运行 Get-ChildItem D:\\AgentWork")
    assert "JSON" in excinfo.value.message
    assert excinfo.value.hint


def test_parse_rejects_unknown_action() -> None:
    with pytest.raises(ParseError):
        parse_model_output(_block({"action": "run_bash", "command": "ls"}))


def test_parse_rejects_empty_command() -> None:
    with pytest.raises(ParseError):
        parse_model_output(_block({"action": "run_powershell", "command": "   "}))


def test_parse_clamps_timeout() -> None:
    parsed = parse_model_output(
        _block({"action": "run_powershell", "command": "Get-Date", "timeout": 9999, "risk": "low"}),
        max_timeout=120,
    )
    assert parsed.timeout == 120
    assert any("timeout" in w for w in parsed.warnings)


def test_parse_missing_risk_is_deferred_to_guard() -> None:
    parsed = parse_model_output(_block({"action": "run_powershell", "command": "Get-Date"}))
    assert parsed.risk is None
    assert any("risk" in w for w in parsed.warnings)


def test_parse_finish_action() -> None:
    parsed = parse_model_output(_block({"action": "finish", "summary": "已整理 8 个 PDF"}))
    assert parsed.action == "finish"
    assert parsed.summary == "已整理 8 个 PDF"


def test_parse_finish_defaults_summary() -> None:
    parsed = parse_model_output(_block({"action": "finish"}))
    assert parsed.action == "finish"
    assert parsed.summary


def test_parse_ask_user_requires_question() -> None:
    with pytest.raises(ParseError):
        parse_model_output(_block({"action": "ask_user"}))
    parsed = parse_model_output(_block({"action": "ask_user", "question": "删除 .tmp 还是 .bak？"}))
    assert parsed.question == "删除 .tmp 还是 .bak？"


def test_parse_rejects_overlong_command() -> None:
    with pytest.raises(ParseError):
        parse_model_output(_block({"action": "run_powershell", "command": "Get-Date " + "x" * 5000}))


def test_parse_tolerates_comment_and_codeblock_without_language() -> None:
    raw = "```\n{\n  // 说明\n  \"action\": \"run_powershell\",\n  \"command\": \"Get-Date\"\n}\n```"
    parsed = parse_model_output(raw)
    assert parsed.command == "Get-Date"
