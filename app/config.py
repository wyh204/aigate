"""Configuration.

Everything comes from environment variables so a container needs no config file.
The route table has a sane default and can be replaced wholesale with
``AIGATE_ROUTES`` (JSON: alias -> ["provider:model", ...]).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app import __version__

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file(path: Path = _ENV_FILE) -> None:
    """Load ``KEY=VALUE`` pairs from .env without overriding real env vars.

    Zero dependency on purpose: a container needs no config file, and a developer
    who drops ``.env`` next to ``pyproject.toml`` gets it for free. Variables
    already present in the process environment always win, so ``$env:X = ...``
    in a shell still beats the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _env(name).split(",") if part.strip())


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    type: str = "openai_compat"
    base_url: str = ""
    api_key: str = ""
    timeout_s: float = 0.0


@dataclass(frozen=True)
class Settings:
    app_name: str = "aigate"
    version: str = "0.1.0"
    log_level: str = "INFO"
    api_keys: tuple[str, ...] = ()
    providers: tuple[ProviderConfig, ...] = ()
    routes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    default_route: str = "default"
    # "route" = an unknown model id is served by the default route (plug-and-play
    # with agents that hardcode gpt-4o/claude-*); "error" = strict 404.
    unknown_model: str = "route"
    request_timeout_s: float = 60.0
    mock_stream_delay_s: float = 0.0
    database_url: str = "sqlite+aiosqlite:///./aigate.db"
    admin_token: str = ""
    usage_flush_interval_s: float = 0.5
    usage_queue_size: int = 2000
    eval_data_dir: str = str(_DATA_DIR)
    cache_enabled: bool = True
    cache_ttl_s: int = 3600
    cache_max_entries: int = 2048
    semantic_cache: bool = True
    semantic_threshold: float = 0.92
    semantic_max_partition: int = 256
    redis_url: str = ""

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_token)


# name, env var, OpenAI-compatible base URL
_KNOWN_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1"),
    ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
    ("qwen", "DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
)


def _build_providers() -> list[ProviderConfig]:
    providers = [ProviderConfig(name="mock", type="mock")]
    raw = _env("AIGATE_PROVIDERS")
    if raw:
        for item in json.loads(raw):
            providers.append(ProviderConfig(**item))
        return providers
    for name, env_key, base_url in _KNOWN_PROVIDERS:
        api_key = _env(env_key)
        if api_key:
            providers.append(
                ProviderConfig(name=name, type="openai_compat", base_url=base_url, api_key=api_key)
            )
    return providers


def _build_routes(providers: list[ProviderConfig]) -> dict[str, tuple[str, ...]]:
    routes: dict[str, tuple[str, ...]] = {"mock": ("mock:mock-echo",)}
    raw = _env("AIGATE_ROUTES")
    if raw:
        for alias, targets in json.loads(raw).items():
            routes[alias] = tuple(targets)
        return routes

    names = {provider.name for provider in providers}
    if "deepseek" in names:
        routes["default"] = ("deepseek:deepseek-chat",)
        routes["smart"] = ("deepseek:deepseek-reasoner", "deepseek:deepseek-chat")
    elif "openai" in names:
        routes["default"] = ("openai:gpt-4o-mini", "openai:gpt-4o")
    elif "qwen" in names:
        routes["default"] = ("qwen:qwen-plus",)
    routes.setdefault("default", ("mock:mock-echo",))
    return routes


def build_settings() -> Settings:
    _load_env_file()
    providers = _build_providers()
    return Settings(
        version=__version__,
        log_level=_env("AIGATE_LOG_LEVEL", "INFO") or "INFO",
        api_keys=_csv("AIGATE_API_KEYS"),
        providers=tuple(providers),
        routes=_build_routes(providers),
        default_route=_env("AIGATE_DEFAULT_ROUTE", "default") or "default",
        unknown_model=(_env("AIGATE_UNKNOWN_MODEL", "route") or "route").lower(),
        request_timeout_s=float(_env("AIGATE_REQUEST_TIMEOUT_S", "60") or 60),
        mock_stream_delay_s=float(_env("AIGATE_MOCK_DELAY_S", "0") or 0),
        database_url=_env("AIGATE_DATABASE_URL", "sqlite+aiosqlite:///./aigate.db"),
        admin_token=_env("AIGATE_ADMIN_TOKEN"),
        usage_flush_interval_s=float(_env("AIGATE_USAGE_FLUSH_INTERVAL_S", "0.5") or 0.5),
        usage_queue_size=int(_env("AIGATE_USAGE_QUEUE_SIZE", "2000") or 2000),
        eval_data_dir=_env("AIGATE_EVAL_DATA_DIR", str(_DATA_DIR)),
        cache_enabled=_flag("AIGATE_CACHE_ENABLED", True),
        cache_ttl_s=int(_env("AIGATE_CACHE_TTL_S", "3600") or 3600),
        cache_max_entries=int(_env("AIGATE_CACHE_MAX_ENTRIES", "2048") or 2048),
        semantic_cache=_flag("AIGATE_SEMANTIC_CACHE", True),
        semantic_threshold=float(_env("AIGATE_SEMANTIC_THRESHOLD", "0.92") or 0.92),
        semantic_max_partition=int(_env("AIGATE_SEMANTIC_MAX_PARTITION", "256") or 256),
        redis_url=_env("AIGATE_REDIS_URL"),
    )
