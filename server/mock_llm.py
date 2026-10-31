"""内置确定性假模型（``LLM_PROVIDER=mock``）。

用途：在没有 API Key / 完全离线的环境下，验证 EdgeAgent 的完整闭环
（解析 → 权限校验 → 人工确认 → 执行 → 结果回传 → 纠错重试 → 中止）。
它不联网、不计费，按对话里的关键词返回固定脚本，因此自动化测试可以写死断言。

脚本规则（按优先级匹配，只看 role=user 的消息）：
1. 出现 【命令执行结果】/【系统回执】 → ``finish``
2. 会话里出现 "坏JSON" → 始终返回纯文本（用于验证纠错重试 2 次后报错）
3. "耗时"             → ``Start-Sleep -Seconds 30``（low，自动执行，供中止测试）
4. "穿越" / "C:\\Windows" → 读取 C:\\Windows 下的文件（会被路径校验拒绝）
5. "删除"             → ``Remove-Item``（受限被拒，完全模式需确认）
6. "新建文件"         → ``New-Item ... verify_test.txt``
7. "列出"             → ``Get-ChildItem D:\\AgentWork``
8. "压缩"/"解压"/"校验"/"网络" → M10 新增能力（压缩包、哈希、端口探测）
9. 其它               → ``finish``
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Final

MOCK_MODEL_NAME: Final[str] = "edgeagent-mock"

TARGET_DIR: Final[str] = "D:\\AgentWork"
VERIFY_FILE: Final[str] = "D:\\AgentWork\\verify_test.txt"
ZIP_SRC_DIR: Final[str] = "D:\\AgentWork\\verify_zip_src"
ZIP_FILE: Final[str] = "D:\\AgentWork\\verify_src.zip"
ZIP_OUT_DIR: Final[str] = "D:\\AgentWork\\verify_zip_out"

FEEDBACK_MARKERS: Final[tuple[str, ...]] = ("【命令执行结果】", "【系统回执】")


def _fence(payload: dict[str, Any]) -> str:
    """把指令包装成模型约定的 ```json 代码块。"""
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


class MockLLMClient:
    """与 :class:`~server.llm_client.LLMClient` 接口一致的离线假模型。"""

    def __init__(self) -> None:
        self.provider = "mock"
        self.model = MOCK_MODEL_NAME

    @property
    def enabled(self) -> bool:
        return True

    async def stream_chat(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        """按脚本产出文本，并模拟流式分片。"""
        text = self.script(messages)
        step = 16
        for index in range(0, len(text), step):
            yield text[index : index + step]
            await asyncio.sleep(0.004)

    async def chat(self, messages: list[dict[str, Any]]) -> str:
        return self.script(messages)

    # ---------------- 脚本 ----------------
    def script(self, messages: list[dict[str, Any]]) -> str:
        """根据对话内容生成确定性的模型输出。"""
        user_text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        all_text = "\n".join(str(m.get("content", "")) for m in messages)

        if any(marker in user_text for marker in FEEDBACK_MARKERS):
            return "好的，本轮到此结束。\n" + _fence({"action": "finish", "summary": "任务已完成（mock 模型）。"})

        # 纠错场景：整个会话里只要出现过“坏JSON”，就坚持输出自然语言，逼出 2 次重试后的失败
        if "坏JSON" in all_text:
            return "我认为这个需求不需要执行任何命令，直接回答你就好，所以我不输出 JSON。"

        if "耗时" in user_text:
            return "这条命令会运行较久，用于验证中止能力。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": "Start-Sleep -Seconds 30",
                    "timeout": 60,
                    "risk": "low",
                    "reason": "模拟一个耗时命令，供中止测试使用",
                }
            )

        if "穿越" in user_text or "C:\\Windows" in user_text:
            return "我尝试读取系统目录中的文件。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": "Get-Content -LiteralPath 'C:\\Windows\\System32\\drivers\\etc\\hosts' -TotalCount 3",
                    "timeout": 15,
                    "risk": "low",
                    "reason": "尝试访问允许目录之外的系统文件（预期被拦截）",
                }
            )

        if "删除" in user_text and not ("扫描" in user_text or "5MB" in user_text or "列出" in user_text):
            return "这一步需要删除文件。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": f"Remove-Item -LiteralPath '{TARGET_DIR}\\*' -Force",
                    "timeout": 20,
                    "risk": "high",
                    "reason": "删除目标目录下的文件",
                }
            )

        # ---- M12：真机验收场景 ----
        if "桌面" in user_text and ("log" in user_text or "5MB" in user_text or "扫描" in user_text):
            # 故意指向 allowed_dirs 之外的桌面，用于验证 Guard 的路径拦截
            return "我先扫描桌面上的大日志文件。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": (
                        "Get-ChildItem -LiteralPath \"$env:USERPROFILE\\Desktop\" -Filter *.log -Recurse "
                        "-ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 5MB } | "
                        "Sort-Object Length -Descending | Select-Object FullName,@{n='MB';e={[math]::Round($_.Length/1MB,2)}}"
                    ),
                    "timeout": 30,
                    "risk": "low",
                    "reason": "扫描桌面大于 5MB 的 .log 文件（桌面不在允许目录内，预期被拦截）",
                }
            )

        if "5MB" in user_text or ("扫描" in user_text and "log" in user_text):
            # 同上但落在允许目录内，用于验证「允许 → 自动执行 → 返回清单」
            return "我在允许目录内扫描大日志文件。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": (
                        f"Get-ChildItem -LiteralPath '{TARGET_DIR}\\logs' -Filter *.log -Recurse "
                        "-ErrorAction SilentlyContinue | Where-Object { $_.Length -gt 5MB } | "
                        "Sort-Object Length -Descending | Select-Object FullName,@{n='MB';e={[math]::Round($_.Length/1MB,2)}}"
                    ),
                    "timeout": 30,
                    "risk": "low",
                    "reason": "在允许目录内扫描大于 5MB 的 .log 文件",
                }
            )

        if "占用 CPU" in user_text or "CPU 最高" in user_text or "进程" in user_text:
            return "我查一下 CPU 占用最高的进程。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": (
                        "Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 3 "
                        "Name,CPU,@{n='MemMB';e={[math]::Round($_.WorkingSet64/1MB,1)}}"
                    ),
                    "timeout": 20,
                    "risk": "low",
                    "reason": "列出 CPU 占用最高的进程（只读）",
                }
            )

        if "C 盘" in user_text or "空间" in user_text:
            return "我看看 C 盘剩余空间。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": (
                        "Get-PSDrive -Name C | Select-Object Name,"
                        "@{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}},"
                        "@{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}}"
                    ),
                    "timeout": 20,
                    "risk": "low",
                    "reason": "读取 C 盘容量与剩余空间（只读）",
                }
            )

        if "压缩" in user_text:
            return "把测试目录打包成 zip。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": (
                        f"Compress-Archive -Path '{ZIP_SRC_DIR}\\*' -DestinationPath '{ZIP_FILE}' -Force; "
                        f"Get-Item -LiteralPath '{ZIP_FILE}' | Select-Object Name,Length"
                    ),
                    "timeout": 30,
                    "risk": "medium",
                    "reason": "压缩目标目录，用于验证磁盘写入类命令",
                }
            )

        if "解压" in user_text:
            return "把刚才的 zip 解压到新目录。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": (
                        f"Expand-Archive -Path '{ZIP_FILE}' -DestinationPath '{ZIP_OUT_DIR}' -Force; "
                        f"Get-ChildItem -LiteralPath '{ZIP_OUT_DIR}' | Select-Object Name,Length"
                    ),
                    "timeout": 30,
                    "risk": "medium",
                    "reason": "解压到允许目录内，用于验证解压能力与路径约束",
                }
            )

        if "校验" in user_text or "哈希" in user_text:
            return "计算一下压缩包的 SHA256。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": f"Get-FileHash -LiteralPath '{ZIP_FILE}' -Algorithm SHA256 | Select-Object Algorithm,Hash",
                    "timeout": 20,
                    "risk": "low",
                    "reason": "校验文件完整性（只读）",
                }
            )

        if "网络" in user_text or "端口" in user_text:
            return "探测一下本地服务端口。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": "Test-NetConnection -ComputerName 127.0.0.1 -Port 8765 -InformationLevel Quiet",
                    "timeout": 25,
                    "risk": "low",
                    "reason": "检测本地端口连通性（只读网络探测）",
                }
            )

        if any(hint in user_text for hint in ("根据文档", "总结", "知识库", "检索一下")):
            return "我已经参考了本地知识库中的资料。\n" + _fence(
                {"action": "finish", "summary": "（mock）已根据本地资料完成总结。"}
            )

        if "新建文件" in user_text:
            return "我在目标目录里新建一个测试文件。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": f"New-Item -ItemType File -Path '{VERIFY_FILE}' -Force | Select-Object FullName",
                    "timeout": 20,
                    "risk": "medium",
                    "reason": "创建验证用的测试文件",
                }
            )

        if "列出" in user_text:
            return "我先看看目录里有什么。\n" + _fence(
                {
                    "action": "run_powershell",
                    "command": f"Get-ChildItem -LiteralPath '{TARGET_DIR}' | Select-Object Name,Length",
                    "timeout": 15,
                    "risk": "low",
                    "reason": "列出目标目录中的文件",
                }
            )

        return "收到，这个需求不需要操作电脑。\n" + _fence({"action": "finish", "summary": "无需执行命令（mock 模型）。"})
