"""网页版驱动的纯逻辑测试（不启动浏览器）。

浏览器行为依赖真实登录态，无法在 CI 里稳定验证；这里只覆盖
容易写错又影响很大的部分：站点解析、增量差分、请求拼装。
"""

from __future__ import annotations

import pytest

from server.web_agent import (
    SITE_ADAPTERS,
    WebAgentError,
    WebChatClient,
    _diff_text,
    get_adapter,
    list_sites,
)


def test_four_sites_are_registered() -> None:
    """M13 约定优先支持的四家。"""
    for site in ("deepseek", "tongyi", "doubao", "kimi"):
        assert site in SITE_ADAPTERS


def test_adapter_has_usable_selectors() -> None:
    for adapter in SITE_ADAPTERS.values():
        assert adapter.input_selectors, f"{adapter.id} 缺少输入框选择器"
        assert adapter.reply_selectors, f"{adapter.id} 缺少回复选择器"
        assert adapter.url.startswith("https://"), f"{adapter.id} URL 非法"


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("moonshot", "kimi"),
        ("qwen", "tongyi"),
        ("aliyun", "tongyi"),
        ("ark", "doubao"),
        ("DEEPSEEK", "deepseek"),
    ],
)
def test_aliases_resolve(alias: str, expected: str) -> None:
    assert get_adapter(alias).id == expected


def test_unknown_site_raises_with_code() -> None:
    with pytest.raises(WebAgentError) as exc:
        get_adapter("some-unknown-site")
    assert exc.value.code == "site_unsupported"
    assert "some-unknown-site" in exc.value.message


def test_list_sites_has_no_secret() -> None:
    items = list_sites()
    assert len(items) == len(SITE_ADAPTERS)
    for item in items:
        assert set(item) == {"id", "label", "url", "login_hint"}


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        ("你好", "你好世界", "世界"),
        ("", "abc", "abc"),
        ("abc", "abc", ""),
        ("第一段", "第一段\n第二段", "\n第二段"),
    ],
)
def test_diff_text_incremental(previous: str, current: str, expected: str) -> None:
    assert _diff_text(previous, current) == expected


def test_diff_text_handles_full_rewrite() -> None:
    """Markdown 重渲染会导致整段变化，此时不能丢内容。"""
    assert _diff_text("旧内容", "全新内容") == "全新内容"


def test_compose_includes_system_and_task() -> None:
    client = WebChatClient(site_id="deepseek", profile_dir="/tmp/x")
    messages = [
        {"role": "system", "content": "你是自动化助手"},
        {"role": "user", "content": "列出文件"},
    ]
    text = client._compose(messages)
    assert "【系统规则】" in text
    assert "你是自动化助手" in text
    assert "【当前任务】" in text
    assert "列出文件" in text
    assert "run_powershell" in text


def test_compose_always_carries_recent_context() -> None:
    """每轮都是新对话，所以必须自带上下文，不能指望网页侧记忆。"""
    client = WebChatClient(site_id="deepseek", profile_dir="/tmp/x")
    messages = [
        {"role": "system", "content": "系统规则"},
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "执行结果如下"},
    ]
    text = client._compose(messages)
    assert "【系统规则】" in text
    assert "【此前对话】" in text
    assert "第一轮问题" in text
    assert "【当前任务】" in text
    assert "执行结果如下" in text


def test_compose_truncates_long_history() -> None:
    """执行结果可能上千行，必须截断，否则会超出网页输入框上限。"""
    client = WebChatClient(site_id="deepseek", profile_dir="/tmp/x")
    messages = [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "结果" * 3000},
        {"role": "user", "content": "下一步"},
    ]
    text = client._compose(messages)
    assert "已截断" in text
    assert len(text) < 3000


def test_model_name_reflects_site() -> None:
    assert WebChatClient(site_id="kimi", profile_dir="/tmp/x").model == "web:kimi"


# ---------------------------------------------------------------- CDP 驱动层
def test_js_array_escapes_quotes() -> None:
    """选择器里可能带单双引号，拼进 JS 时必须安全。"""
    from server.web_agent import _js_array

    literal = _js_array(("div[contenteditable='true']", 'a"b'))
    assert literal.startswith("[")
    assert "contenteditable='true'" in literal
    assert '\\"b' in literal or "a" in literal


def test_driver_status_shape() -> None:
    from server.web_agent import driver_status

    status = driver_status()
    assert set(status) == {"available", "websockets", "websockets_version", "edge_path"}
    assert isinstance(status["available"], bool)
    # available 必须同时满足两个条件
    assert status["available"] == (status["websockets"] and bool(status["edge_path"]))


def test_free_port_is_usable() -> None:
    """调试端口必须是当时可绑定的空闲端口。"""
    import socket

    from server.cdp import free_port

    port = free_port()
    assert 1024 < port < 65536
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))  # 能绑定说明确实空闲
    finally:
        sock.close()


def test_pick_target_prefers_site_over_internal_pages() -> None:
    """首次启动会冒出 edge://sync-confirmation-dialog，不能连错窗口。"""
    client = WebChatClient(site_id="deepseek", profile_dir="/tmp/x")
    targets = [
        {"url": "edge://sync-confirmation-dialog/", "webSocketDebuggerUrl": "ws://a"},
        {"url": "https://chat.deepseek.com/sign_in", "webSocketDebuggerUrl": "ws://b"},
        {"url": "about:blank", "webSocketDebuggerUrl": "ws://c"},
    ]
    assert client._pick_target(targets)["webSocketDebuggerUrl"] == "ws://b"


def test_pick_target_skips_internal_when_site_absent() -> None:
    client = WebChatClient(site_id="deepseek", profile_dir="/tmp/x")
    targets = [
        {"url": "edge://settings/", "webSocketDebuggerUrl": "ws://a"},
        {"url": "about:blank", "webSocketDebuggerUrl": "ws://b"},
    ]
    assert client._pick_target(targets)["webSocketDebuggerUrl"] == "ws://b"


def test_cdp_error_carries_code() -> None:
    from server.cdp import CDPError

    err = CDPError("找不到 Edge", code="edge_missing")
    assert err.code == "edge_missing"
    assert "Edge" in err.message


# ---------------------------------------------------------------- EdgeProcess
def test_stable_port_is_deterministic() -> None:
    """同一 profile 必须算出同一端口，否则无法复用已开实例。"""
    from server.cdp import stable_port

    a = stable_port(r"D:/data/webprofile")
    b = stable_port(r"D:/data/webprofile")
    c = stable_port(r"D:/data/other")
    assert a == b
    assert a != c
    assert 9300 <= a < 9400


def test_probe_returns_false_when_nothing_listening() -> None:
    from server.cdp import EdgeProcess, free_port

    edge = EdgeProcess("C:/tmp/ea_test_profile")
    edge.port = free_port()  # 保证没人监听
    assert edge.probe() is False


def test_pid_alive_detects_self() -> None:
    import os

    from server.cdp import _pid_alive

    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999_999) is False


def test_kill_tree_on_missing_pid_is_silent() -> None:
    """清理不存在的进程不能抛异常，否则退出路径会二次失败。"""
    from server.cdp import kill_tree

    kill_tree(999_999)  # 不抛即通过


def test_stop_without_start_is_safe() -> None:
    from server.cdp import EdgeProcess

    EdgeProcess("C:/tmp/ea_test_profile").stop()
