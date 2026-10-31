"""Provider 注册表与切换逻辑的单元测试。

重点覆盖安全相关的不变量：
- 密钥绝不出现在给前端的结构里；
- 未知/不支持的站点要明确报错，而不是静默降级；
- 密钥查找有确定的优先级。
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from server.providers import (
    DEFAULT_PRESET,
    KIND_API,
    KIND_MOCK,
    KIND_WEB,
    PRESETS,
    PRESET_MAP,
    ProviderStore,
    Selection,
    build_selection,
    describe_presets,
    get_preset,
    resolve_api_key,
)


def test_presets_are_well_formed() -> None:
    """每个预设都要有 id/label/base_url/key 变量，且 id 不重复。"""
    ids = [p.id for p in PRESETS]
    assert len(ids) == len(set(ids)), "预设 id 重复"
    for preset in PRESETS:
        assert preset.id
        assert preset.label
        assert preset.api_key_env
        if preset.id != "custom":
            assert preset.base_url.startswith("http"), f"{preset.id} 缺少 base_url"
            assert preset.default_model, f"{preset.id} 缺少默认模型"


def test_default_preset_exists() -> None:
    assert DEFAULT_PRESET in PRESET_MAP


@pytest.mark.parametrize(
    "preset_id,expected",
    [
        ("deepseek", "DeepSeek"),
        ("tongyi", "通义千问"),
        ("doubao", "豆包"),
        ("kimi", "Kimi"),
    ],
)
def test_mainland_presets_are_registered(preset_id: str, expected: str) -> None:
    """国产四家必须在册（M13 的验收项）。"""
    assert get_preset(preset_id).label == expected


def test_unknown_preset_falls_back_to_default() -> None:
    """未知 id 不能抛异常，否则用户写错一个字母服务就起不来。"""
    assert get_preset("not-exist").id == DEFAULT_PRESET
    assert get_preset(None).id == DEFAULT_PRESET


def test_resolve_api_key_prefers_preset_specific_env(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = get_preset("deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("LLM_API_KEY", "sk-generic")
    key, source = resolve_api_key(preset)
    assert key == "sk-deepseek"
    assert source == "DEEPSEEK_API_KEY"


def test_resolve_api_key_falls_back_to_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = get_preset("deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-generic")
    key, source = resolve_api_key(preset)
    assert key == "sk-generic"
    assert source == "LLM_API_KEY"


def test_resolve_api_key_returns_empty_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = get_preset("kimi")
    for name in ("MOONSHOT_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    key, source = resolve_api_key(preset)
    assert key == ""
    assert source == ""


def test_build_selection_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-qwen")
    selection = build_selection(kind=KIND_API, preset_id="tongyi")
    assert selection.kind == KIND_API
    assert selection.model == "qwen-plus"
    assert selection.base_url.endswith("compatible-mode/v1")
    assert selection.api_key == "sk-qwen"
    assert selection.enabled is True


def test_build_selection_mock_has_no_key() -> None:
    selection = build_selection(kind=KIND_MOCK, preset_id="deepseek")
    assert selection.is_mock is True
    assert selection.api_key == ""
    assert selection.enabled is True


def test_build_selection_web_requires_supported_site() -> None:
    # 支持网页版的站点
    selection = build_selection(kind=KIND_WEB, preset_id="deepseek")
    assert selection.is_web is True
    assert selection.base_url.startswith("https://")
    # 未实现网页版的站点必须明确拒绝（openai 没有对应网页版适配器）
    with pytest.raises(ValueError):
        build_selection(kind=KIND_WEB, preset_id="openai")


def test_new_sites_are_registered() -> None:
    """智谱 / 文心 / 元宝 三家网页版必须已注册。"""
    from server.web_agent import SITE_ADAPTERS, get_adapter

    for site, label in (("zhipu", "智谱清言"), ("qianfan", "文心一言"), ("hunyuan", "腾讯元宝")):
        assert site in SITE_ADAPTERS
        assert get_adapter(site).label == label
    # 元宝的输入框实测是 Quill 编辑器
    assert "div.ql-editor" in get_adapter("hunyuan").input_selectors


def test_new_site_aliases() -> None:
    from server.web_agent import get_adapter

    for alias, expected in (
        ("chatglm", "zhipu"),
        ("wenxin", "qianfan"),
        ("yiyan", "qianfan"),
        ("yuanbao", "hunyuan"),
    ):
        assert get_adapter(alias).id == expected


def test_selection_public_never_leaks_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归测试：public() 一旦带上密钥就等于把它下发到浏览器。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    selection = build_selection(kind=KIND_API, preset_id="deepseek")
    payload = selection.public()
    assert "api_key" not in payload
    assert "sk-should-not-leak" not in json.dumps(payload, ensure_ascii=False)
    assert payload["api_key_configured"] is True


def test_describe_presets_marks_key_status(monkeypatch: pytest.MonkeyPatch) -> None:
    for preset in PRESETS:
        monkeypatch.delenv(preset.api_key_env, raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")

    items = describe_presets()
    by_id = {item["id"]: item for item in items}
    assert by_id["kimi"]["api_key_configured"] is True
    assert by_id["deepseek"]["api_key_configured"] is False
    assert all("api_key" not in item or item["api_key"] == "" for item in items)
    assert "sk-kimi" not in json.dumps(items, ensure_ascii=False)


def test_provider_store_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProviderStore(tmp)
        assert store.load() == {}
        store.save({"kind": "web", "preset": "doubao"})
        assert store.load()["preset"] == "doubao"
        assert os.path.isfile(store.path)
        # 内容必须是合法 JSON（原子写入不应留下半截文件）
        with open(store.path, encoding="utf-8") as handle:
            assert json.load(handle)["kind"] == "web"
        store.clear()
        assert store.load() == {}


def test_selection_disabled_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DEEPSEEK_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    selection = Selection(kind=KIND_API, preset="deepseek", model="deepseek-chat",
                          base_url="https://api.deepseek.com/v1", api_key="")
    assert selection.enabled is False
