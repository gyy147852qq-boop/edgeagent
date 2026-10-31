"""内置 mock 模型单元测试：保证 verify.ps1 依赖的脚本分支是确定性的。"""

from __future__ import annotations

import asyncio
import json
import re

from server.mock_llm import MockLLMClient

FENCE_RE = re.compile(r"```json\s*(.+?)```", re.DOTALL)


def _collect(messages: list[dict[str, str]]) -> str:
    client = MockLLMClient()

    async def run() -> str:
        parts = []
        async for delta in client.stream_chat(messages):
            parts.append(delta)
        return "".join(parts)

    return asyncio.run(run())


def _payload(text: str) -> dict:
    match = FENCE_RE.search(text)
    assert match, f"mock 输出缺少 json 代码块: {text!r}"
    return json.loads(match.group(1))


def test_mock_is_always_enabled() -> None:
    client = MockLLMClient()
    assert client.enabled is True
    assert client.provider == "mock"


def test_mock_list_command() -> None:
    text = _collect([{"role": "user", "content": "列出 D:\\AgentWork 下的文件"}])
    payload = _payload(text)
    assert payload["action"] == "run_powershell"
    assert "Get-ChildItem" in payload["command"]
    assert payload["risk"] == "low"


def test_mock_delete_command_is_high_risk() -> None:
    payload = _payload(_collect([{"role": "user", "content": "删除 D:\\AgentWork 下所有文件"}]))
    assert payload["action"] == "run_powershell"
    assert "Remove-Item" in payload["command"]
    assert payload["risk"] == "high"


def test_mock_new_file_command() -> None:
    payload = _payload(_collect([{"role": "user", "content": "新建文件 D:\\AgentWork\\verify_test.txt"}]))
    assert "New-Item" in payload["command"]
    assert "verify_test.txt" in payload["command"]


def test_mock_windows_path_command() -> None:
    payload = _payload(_collect([{"role": "user", "content": "试试路径穿越读取 C:\\Windows 下的文件"}]))
    assert "C:\\Windows" in payload["command"]


def test_mock_long_running_command() -> None:
    payload = _payload(_collect([{"role": "user", "content": "跑一条耗时命令"}]))
    assert payload["command"].startswith("Start-Sleep")


def test_mock_bad_json_branch_returns_plain_text() -> None:
    text = _collect([{"role": "user", "content": "坏JSON测试"}])
    assert FENCE_RE.search(text) is None
    assert "JSON" in text


def test_mock_finishes_after_command_result() -> None:
    messages = [
        {"role": "user", "content": "列出文件"},
        {"role": "assistant", "content": "```json\n{}\n```"},
        {"role": "user", "content": "【命令执行结果】\n命令：Get-ChildItem\n退出码：0"},
    ]
    payload = _payload(_collect(messages))
    assert payload["action"] == "finish"


def test_mock_unknown_request_finishes() -> None:
    payload = _payload(_collect([{"role": "user", "content": "今天天气怎么样"}]))
    assert payload["action"] == "finish"
