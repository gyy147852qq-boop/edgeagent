"""AI 服务商注册表：国产主流模型与网页版聊天机器人的统一接入层。

设计目标
--------
1. **一个选择器覆盖两类接入**：API 模式（OpenAI 兼容）与网页版模式（浏览器驱动），
   前端只需展示一个列表，用户点一下即可切换，不必改 config.yaml。
2. **密钥永不下发**：这里只暴露"是否已配置"，密钥只存在于服务端进程内存。
3. **切换可持久化**：选择写入 ``<data_dir>/provider.json``，重启后保持；不污染 config.yaml。

两类 provider 的区别
--------------------
- ``openai``：走标准 ``/chat/completions``，稳定、可流式，需要 API Key。
- ``web``   ：用 Playwright 驱动用户已登录的浏览器操作网页版对话框，不需要 Key，
  但依赖页面结构，站点改版会失效（见 :mod:`server.web_agent`）。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 预设
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """一个可选的服务商。

    ``api_key_env`` 是推荐的专属环境变量名；若用户只配了通用的 ``LLM_API_KEY``，
    也会作为兜底使用（见 :func:`resolve_api_key`）。
    """

    id: str
    label: str
    vendor: str
    base_url: str
    default_model: str
    models: tuple[str, ...]
    api_key_env: str
    web_url: str = ""
    note: str = ""
    #: 网页版适配器是否已实现（未实现的站点会在前端标灰）
    web_supported: bool = False

    def to_public(self, *, api_key_configured: bool) -> dict[str, Any]:
        """给前端的公开结构，绝不包含密钥。"""
        return {
            "id": self.id,
            "label": self.label,
            "vendor": self.vendor,
            "default_model": self.default_model,
            "models": list(self.models),
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_configured": api_key_configured,
            "web_url": self.web_url,
            "web_supported": self.web_supported,
            "note": self.note,
        }


#: 顺序即前端的展示顺序：国产优先。
PRESETS: tuple[Preset, ...] = (
    Preset(
        id="deepseek",
        label="DeepSeek",
        vendor="深度求索",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
        api_key_env="DEEPSEEK_API_KEY",
        web_url="https://chat.deepseek.com/",
        note="性价比高，中文指令遵循好",
        web_supported=True,
    ),
    Preset(
        id="tongyi",
        label="通义千问",
        vendor="阿里云 DashScope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        models=("qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"),
        api_key_env="DASHSCOPE_API_KEY",
        web_url="https://tongyi.aliyun.com/qianwen/",
        note="DashScope 兼容模式，long 上下文可选",
        web_supported=True,
    ),
    Preset(
        id="doubao",
        label="豆包",
        vendor="字节跳动 火山方舟",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-pro-32k",
        models=("doubao-pro-32k", "doubao-pro-128k", "doubao-lite-32k"),
        api_key_env="ARK_API_KEY",
        web_url="https://www.doubao.com/chat/",
        note="方舟需先创建推理接入点，模型名可填接入点 ID",
        web_supported=True,
    ),
    Preset(
        id="kimi",
        label="Kimi",
        vendor="月之暗面 Moonshot",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k",
        models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
        api_key_env="MOONSHOT_API_KEY",
        web_url="https://kimi.moonshot.cn/",
        note="长文本见长，128k 版本适合读文档",
        web_supported=True,
    ),
    Preset(
        id="zhipu",
        label="智谱 GLM",
        vendor="智谱 AI",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-flash",
        models=("glm-4-flash", "glm-4-plus", "glm-4-air"),
        api_key_env="ZHIPU_API_KEY",
        web_url="https://chatglm.cn/",
        note="flash 版本免费额度较多",
        web_supported=True,
    ),
    Preset(
        id="qianfan",
        label="文心一言",
        vendor="百度智能云千帆",
        base_url="https://qianfan.baidubce.com/v2",
        default_model="ernie-4.0-8k",
        models=("ernie-4.0-8k", "ernie-speed-8k", "ernie-lite-8k"),
        api_key_env="QIANFAN_API_KEY",
        web_url="https://yiyan.baidu.com/",
        note="千帆 v2 接口使用 Bearer Token",
        web_supported=True,
    ),
    Preset(
        id="hunyuan",
        label="腾讯混元",
        vendor="腾讯云",
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        default_model="hunyuan-lite",
        models=("hunyuan-lite", "hunyuan-standard", "hunyuan-pro"),
        api_key_env="HUNYUAN_API_KEY",
        web_url="https://yuanbao.tencent.com/",
        note="元宝网页版对应 hunyuan 系列",
        web_supported=True,
    ),
    Preset(
        id="openai",
        label="OpenAI",
        vendor="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4-turbo"),
        api_key_env="OPENAI_API_KEY",
        web_url="https://chatgpt.com/",
        note="需要可访问的网络环境",
    ),
    Preset(
        id="custom",
        label="自定义接口",
        vendor="OpenAI 兼容",
        base_url="",
        default_model="",
        models=(),
        api_key_env="LLM_API_KEY",
        note="任意 OpenAI 兼容网关，请填写 base_url 与模型名",
    ),
)

PRESET_MAP: dict[str, Preset] = {p.id: p for p in PRESETS}

#: 未指定预设时使用的默认项
DEFAULT_PRESET: str = "deepseek"

#: provider 类型
KIND_API = "openai"
KIND_WEB = "web"
KIND_MOCK = "mock"


def get_preset(preset_id: str | None) -> Preset:
    """按 id 取预设，未知 id 回退到默认项（不抛异常，保证启动不中断）。"""
    if preset_id and preset_id in PRESET_MAP:
        return PRESET_MAP[preset_id]
    return PRESET_MAP[DEFAULT_PRESET]


def resolve_api_key(preset: Preset, *, fallback_env: str = "LLM_API_KEY") -> tuple[str, str]:
    """按优先级找出这个预设可用的密钥。

    顺序：预设专属变量 → 通用 ``LLM_API_KEY`` → ``OPENAI_API_KEY``。
    返回 ``(key, 来源变量名)``，都找不到时返回 ``("", "")``。
    """
    for env_name in (preset.api_key_env, fallback_env, "OPENAI_API_KEY"):
        if not env_name:
            continue
        value = (os.getenv(env_name) or "").strip()
        if value:
            return value, env_name
    return "", ""


# ---------------------------------------------------------------------------
# 运行时选择
# ---------------------------------------------------------------------------


@dataclass
class Selection:
    """当前生效的选择（不含密钥明文）。"""

    kind: str = KIND_API
    preset: str = DEFAULT_PRESET
    model: str = ""
    base_url: str = ""
    #: 密钥只放在这里，序列化时必须排除
    api_key: str = field(default="", repr=False)
    key_source: str = ""
    updated_at: float = field(default_factory=time.time)

    @property
    def is_mock(self) -> bool:
        return self.kind == KIND_MOCK

    @property
    def is_web(self) -> bool:
        return self.kind == KIND_WEB

    @property
    def enabled(self) -> bool:
        """能否发起真实请求。"""
        if self.kind == KIND_MOCK:
            return True
        if self.kind == KIND_WEB:
            return True
        return bool(self.api_key and self.base_url and self.model)

    def public(self) -> dict[str, Any]:
        """可安全下发给前端的字段。"""
        preset = get_preset(self.preset)
        return {
            "kind": self.kind,
            "preset": self.preset,
            "preset_label": preset.label,
            "vendor": preset.vendor,
            "model": self.model or preset.default_model,
            "base_url": self.base_url,
            "api_key_configured": bool(self.api_key),
            "api_key_env": preset.api_key_env,
            "enabled": self.enabled,
            "web_url": preset.web_url,
            "web_supported": preset.web_supported,
            "updated_at": int(self.updated_at),
        }


class ProviderStore:
    """把用户的选择持久化到 ``<data_dir>/provider.json``。

    单独存一个文件而不是写回 config.yaml，是为了两件事：
    1. 不改用户的配置注释与排版；
    2. 打包环境下 config.yaml 可能只读，而数据目录一定可写。
    """

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self._path = os.path.join(str(data_dir), "provider.json")

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, payload: dict[str, Any]) -> None:
        """原子写入，避免断电留下半个 JSON。"""
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def clear(self) -> None:
        try:
            os.remove(self._path)
        except OSError:
            pass


def build_selection(
    *,
    kind: str,
    preset_id: str,
    model: str | None = None,
    base_url: str | None = None,
    fallback_env: str = "LLM_API_KEY",
) -> Selection:
    """根据类型与预设构造一份生效配置。"""
    preset = get_preset(preset_id)
    kind = kind if kind in {KIND_API, KIND_WEB, KIND_MOCK} else KIND_API

    if kind == KIND_MOCK:
        return Selection(kind=KIND_MOCK, preset="mock", model="edgeagent-mock", base_url="")

    if kind == KIND_WEB:
        if not preset.web_supported:
            raise ValueError(f"{preset.label} 的网页版驱动尚未实现，请改用 API 模式")
        return Selection(kind=KIND_WEB, preset=preset.id, model="网页版", base_url=preset.web_url)

    key, source = resolve_api_key(preset, fallback_env=fallback_env)
    return Selection(
        kind=KIND_API,
        preset=preset.id,
        model=(model or preset.default_model).strip(),
        base_url=(base_url or preset.base_url).strip(),
        api_key=key,
        key_source=source,
    )


def describe_presets(fallback_env: str = "LLM_API_KEY") -> list[dict[str, Any]]:
    """列出全部预设，附带"是否已配密钥"的布尔标记（不含密钥本身）。"""
    items: list[dict[str, Any]] = []
    for preset in PRESETS:
        key, _ = resolve_api_key(preset, fallback_env=fallback_env)
        items.append(preset.to_public(api_key_configured=bool(key)))
    return items


def presets_as_dicts() -> list[dict[str, Any]]:
    """调试/测试用：预设的完整结构（不含密钥，因为预设本身就不存密钥）。"""
    return [asdict(preset) for preset in PRESETS]
