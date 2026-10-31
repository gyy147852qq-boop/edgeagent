"""EdgeAgent 配置加载、校验与默认值。

设计原则：
1. 配置文件（config.yaml）负责非敏感参数；密钥只从环境变量 / .env 读取。
2. 所有安全相关配置在加载时即做收紧校验（例如 host 强制 127.0.0.1）。
3. 加载失败抛 :class:`ConfigError`，服务启动时应中断而不是降级运行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

try:  # python-dotenv 为可选依赖，缺失时退化为只读真实环境变量
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - 仅在没有安装 dotenv 时触发

    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:  # type: ignore[misc]
        """无 dotenv 时的兜底实现。"""
        return False


from .paths import data_dir, resource_path  # noqa: E402  (依赖上面的可选导入)

#: 只读资源根（源码模式=仓库根，打包模式=_MEIPASS）
BASE_DIR = resource_path()
#: 可写数据根（config.yaml / .env / logs / sessions / .agent_token 都落在这里）
DATA_DIR = data_dir()
DEFAULT_CONFIG_PATH = DATA_DIR / "config.yaml"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

from .providers import get_preset, resolve_api_key  # noqa: E402  (仅依赖标准库)


class ConfigError(RuntimeError):
    """配置非法时抛出，调用方应终止启动。"""


@dataclass
class ServerConfig:
    """HTTP 服务配置。host 只能是本机回环地址。"""

    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


@dataclass
class LLMConfig:
    """OpenAI 兼容接口配置。api_key 只存在于内存，不落盘。"""

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    api_key_env: str = "LLM_API_KEY"
    # openai = 走真实接口；mock = 使用内置确定性假模型（离线自测/无密钥时）；web = 浏览器驱动网页版
    provider: str = "openai"
    #: 具体服务商预设（deepseek / tongyi / doubao / kimi / zhipu / qianfan / hunyuan / openai / custom）
    preset: str = "deepseek"
    #: 网页版驱动相关
    web_headless: bool = True
    web_profile_dir: str = "webprofile"
    web_timeout: float = 120.0
    temperature: float = 0.2
    max_tokens: int = 1500
    timeout: float = 120.0
    max_retries: int = 2

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    @property
    def is_web(self) -> bool:
        return self.provider == "web"

    @property
    def chat_completions_url(self) -> str:
        """拼接 /chat/completions 端点，兼容用户多写或少写斜杠的情况。"""
        base = self.base_url.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @property
    def enabled(self) -> bool:
        """是否具备调用大模型的最低条件。"""
        if self.is_mock or self.is_web:
            return True
        return bool(self.api_key and self.base_url and self.model)


@dataclass
class SecurityConfig:
    """权限与来源校验配置。"""

    allowed_dirs: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    token_file: str = ".agent_token"
    confirm_timeout_seconds: int = 300
    confirm_risk_levels: list[str] = field(default_factory=lambda: ["medium", "high"])
    full_mode_confirm: str = "all"
    # 支持小数（例如 0.5 = 30 秒），便于自动化验证时缩短等待
    idle_downgrade_minutes: float = 15.0


@dataclass
class ExecutorConfig:
    """PowerShell 执行器配置。"""

    default_timeout: int = 30
    max_timeout: int = 120
    max_output_chars: int = 8000
    env_allowlist: list[str] = field(default_factory=list)


@dataclass
class SessionConfig:
    """会话持久化与上下文窗口配置。"""

    dir: str = "sessions"
    max_history_messages: int = 40
    max_steps: int = 25


@dataclass
class LoggingConfig:
    """日志配置。"""

    dir: str = "logs"
    level: str = "INFO"


@dataclass
class KnowledgeConfig:
    """本地知识库（RAG）配置。

    dirs 为空时退化为 allowed_dirs，保证「能读的范围」与「能操作的范围」一致。
    """

    enabled: bool = True
    dirs: list[str] = field(default_factory=list)
    max_files: int = 200
    max_file_mb: float = 5.0
    chunk_chars: int = 600
    chunk_overlap: int = 100
    top_k: int = 5
    cache_seconds: float = 60.0


@dataclass
class Config:
    """聚合配置对象，全局只加载一次并注入各组件。"""

    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    #: 可写数据根（sessions / logs / token 等）
    base_dir: Path = field(default_factory=data_dir)
    #: 只读资源根（web/ 等）
    resource_dir: Path = field(default_factory=resource_path)
    warnings: list[str] = field(default_factory=list)

    # ---- 派生路径 ----
    @property
    def sessions_dir(self) -> Path:
        return self._abs(self.session.dir)

    @property
    def logs_dir(self) -> Path:
        return self._abs(self.logging.dir)

    @property
    def web_dir(self) -> Path:
        """前端静态资源目录（打包后位于 ``_MEIPASS/web``）。"""
        return self.resource_dir / "web"

    @property
    def token_path(self) -> Path:
        return self._abs(self.security.token_file)

    def _abs(self, value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.base_dir / p)

    def ensure_dirs(self) -> None:
        """创建运行期需要的目录。"""
        for path in (self.sessions_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)

    def default_cwd(self) -> str:
        """会话默认工作目录：第一个允许目录（不存在则回落到项目根）。"""
        for d in self.security.allowed_dirs:
            if Path(d).is_dir():
                return d
        return str(self.base_dir)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    """把 list 或分号/逗号分隔的字符串统一成字符串列表。"""
    if isinstance(value, (list, tuple)):
        items: Iterable[Any] = value
        return [str(i).strip() for i in items if str(i).strip()]
    if isinstance(value, str):
        raw = value.replace(";", ",").split(",")
        return [i.strip() for i in raw if i.strip()]
    return []


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def load_config(config_path: str | Path | None = None) -> Config:
    """读取 config.yaml + .env，返回校验通过的 :class:`Config`。

    环境变量优先级高于 config.yaml，便于临时切换模型或目录。
    """
    base = data_dir()
    load_dotenv(base / ".env")

    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_absolute():
        path = base / path

    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:  # 语法错误直接失败，不做猜测
            raise ConfigError(f"config.yaml 解析失败: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError("config.yaml 顶层必须是映射（key: value）")
        raw = loaded or {}

    warnings: list[str] = []

    # ---------- server ----------
    srv_raw = _section(raw, "server")
    host_raw = str(srv_raw.get("host", "127.0.0.1")).strip()
    host = host_raw if host_raw in LOCAL_HOSTS else "127.0.0.1"
    if host != host_raw:
        warnings.append(f"server.host={host_raw!r} 被强制修正为 127.0.0.1（只允许本机监听）")
    port = _as_int(os.getenv("PORT", srv_raw.get("port", 8765)), 8765)
    if not 1024 <= port <= 65535:
        warnings.append(f"server.port={port} 非法，回退为 8765")
        port = 8765

    server = ServerConfig(
        host=host,
        port=port,
        open_browser=_as_bool(os.getenv("OPEN_BROWSER", srv_raw.get("open_browser", True)), True),
    )

    # ---------- llm ----------
    llm_raw = _section(raw, "llm")
    api_key_env = str(llm_raw.get("api_key_env", "LLM_API_KEY")).strip() or "LLM_API_KEY"

    preset_id = str(os.getenv("LLM_PRESET", llm_raw.get("preset", "deepseek"))).strip().lower() or "deepseek"
    preset = get_preset(preset_id)
    # 预设决定 base_url/model；只有「自定义」才完全听 config.yaml，避免两处配置打架
    if preset.id == "custom":
        base_url = str(os.getenv("LLM_BASE_URL", llm_raw.get("base_url", ""))).strip()
        model = str(os.getenv("LLM_MODEL", llm_raw.get("model", ""))).strip()
    else:
        base_url = str(os.getenv("LLM_BASE_URL") or preset.base_url).strip()
        model = str(os.getenv("LLM_MODEL") or preset.default_model).strip()

    api_key, key_source = resolve_api_key(preset, fallback_env=api_key_env)

    provider_raw = str(os.getenv("LLM_PROVIDER", llm_raw.get("provider", "openai"))).strip().lower()
    if provider_raw == "mock":
        provider = "mock"
    elif provider_raw == "web":
        provider = "web"
    else:
        provider = "openai"

    web_raw = _section(raw, "web")
    llm = LLMConfig(
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_key_env=api_key_env,
        provider=provider,
        preset=preset.id,
        web_headless=_as_bool(os.getenv("WEB_HEADLESS", web_raw.get("headless", False)), False),
        web_profile_dir=str(web_raw.get("profile_dir", "webprofile")),
        web_timeout=_as_float(web_raw.get("timeout", 120), 120.0),
        temperature=_as_float(llm_raw.get("temperature", 0.2), 0.2),
        max_tokens=_as_int(llm_raw.get("max_tokens", 1500), 1500),
        timeout=_as_float(llm_raw.get("timeout", 120), 120.0),
        max_retries=max(0, min(5, _as_int(llm_raw.get("max_retries", 2), 2))),
    )
    if provider == "openai" and not api_key:
        warnings.append(
            f"未配置 {preset.label} 的密钥（{preset.api_key_env} 或 {api_key_env}），"
            "/api/chat 将返回配置错误；可在界面顶部切换为「网页版」或「内置模型」"
        )
    elif llm.is_mock:
        warnings.append("LLM_PROVIDER=mock：正在使用内置确定性假模型，仅用于离线自测")

    # ---------- security ----------
    sec_raw = _section(raw, "security")
    env_dirs = os.getenv("ALLOWED_DIRS")
    allowed_dirs = _as_str_list(env_dirs if env_dirs else sec_raw.get("allowed_dirs", []))
    normalized: list[str] = []
    for d in allowed_dirs:
        try:
            normalized.append(os.path.realpath(os.path.abspath(d)))
        except OSError:
            normalized.append(d)
    if not normalized:
        raise ConfigError("security.allowed_dirs 不能为空：至少配置一个允许操作的目录")

    origins = _as_str_list(sec_raw.get("allowed_origins", []))
    if not origins:
        origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]

    confirm_levels = [x.lower() for x in _as_str_list(sec_raw.get("confirm_risk_levels", ["medium", "high"]))]
    confirm_levels = [x for x in confirm_levels if x in {"low", "medium", "high"}] or ["medium", "high"]

    full_mode_confirm = str(sec_raw.get("full_mode_confirm", "all")).strip().lower()
    if full_mode_confirm not in {"all", "medium_high", "none"}:
        warnings.append(f"security.full_mode_confirm={full_mode_confirm!r} 非法，回退为 all")
        full_mode_confirm = "all"

    security = SecurityConfig(
        allowed_dirs=normalized,
        allowed_origins=origins,
        token_file=str(sec_raw.get("token_file", ".agent_token")),
        confirm_timeout_seconds=max(10, _as_int(sec_raw.get("confirm_timeout_seconds", 300), 300)),
        confirm_risk_levels=confirm_levels,
        full_mode_confirm=full_mode_confirm,
        idle_downgrade_minutes=max(0.1, _as_float(os.getenv("IDLE_DOWNGRADE_MINUTES", sec_raw.get("idle_downgrade_minutes", 15)), 15.0)),
    )

    # ---------- executor ----------
    ex_raw = _section(raw, "executor")
    default_timeout = max(1, _as_int(ex_raw.get("default_timeout", 30), 30))
    max_timeout = max(default_timeout, _as_int(ex_raw.get("max_timeout", 120), 120))
    env_allow = _as_str_list(
        ex_raw.get(
            "env_allowlist",
            [
                "SystemRoot",
                "SystemDrive",
                "windir",
                "PATH",
                "PATHEXT",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "COMSPEC",
                "ProgramData",
                "NUMBER_OF_PROCESSORS",
                "PROCESSOR_ARCHITECTURE",
                "OS",
            ],
        )
    )
    executor = ExecutorConfig(
        default_timeout=default_timeout,
        max_timeout=max_timeout,
        max_output_chars=max(512, _as_int(ex_raw.get("max_output_chars", 8000), 8000)),
        env_allowlist=env_allow,
    )

    # ---------- session / logging ----------
    ses_raw = _section(raw, "session")
    session = SessionConfig(
        dir=str(ses_raw.get("dir", "sessions")),
        max_history_messages=max(4, _as_int(ses_raw.get("max_history_messages", 40), 40)),
        max_steps=max(1, min(60, _as_int(ses_raw.get("max_steps", 25), 25))),
    )

    log_raw = _section(raw, "logging")
    level = str(log_raw.get("level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"
    logging_cfg = LoggingConfig(dir=str(log_raw.get("dir", "logs")), level=level)

    # ---------- knowledge（RAG） ----------
    kn_raw = _section(raw, "knowledge")
    kn_dirs_raw = _as_str_list(os.getenv("KNOWLEDGE_DIRS") or kn_raw.get("dirs") or [])
    kn_dirs: list[str] = []
    for d in kn_dirs_raw:
        try:
            kn_dirs.append(os.path.realpath(os.path.abspath(d)))
        except OSError:
            kn_dirs.append(d)
    if not kn_dirs:
        # 未显式配置时只允许读 allowed_dirs，避免把整块磁盘纳入检索范围
        kn_dirs = list(security.allowed_dirs)

    knowledge = KnowledgeConfig(
        enabled=_as_bool(kn_raw.get("enabled", True), True),
        dirs=kn_dirs,
        max_files=max(1, min(5000, _as_int(kn_raw.get("max_files", 200), 200))),
        max_file_mb=max(0.1, _as_float(kn_raw.get("max_file_mb", 5), 5.0)),
        chunk_chars=max(200, min(4000, _as_int(kn_raw.get("chunk_chars", 600), 600))),
        chunk_overlap=max(0, min(1000, _as_int(kn_raw.get("chunk_overlap", 100), 100))),
        top_k=max(1, min(20, _as_int(kn_raw.get("top_k", 5), 5))),
        cache_seconds=max(1.0, _as_float(kn_raw.get("cache_seconds", 60), 60.0)),
    )

    cfg = Config(
        server=server,
        llm=llm,
        security=security,
        executor=executor,
        session=session,
        logging=logging_cfg,
        knowledge=knowledge,
        base_dir=base,
        resource_dir=resource_path(),
        warnings=warnings,
    )
    cfg.ensure_dirs()
    return cfg


def mask_secret(value: str, keep: int = 4) -> str:
    """脱敏展示密钥，用于日志与前端健康检查输出。"""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}***{value[-keep:]}"
