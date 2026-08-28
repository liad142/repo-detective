from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_path: Path
    reports_dir: Path

    openai_api_key: str | None
    openai_base_url: str
    openai_model: str | None

    github_token: str | None
    github_api_url: str
    github_api_version: str

    http_timeout_seconds: int
    github_cache_ttl_seconds: int
    max_tool_items: int
    max_file_chars: int
    llm_timeout_seconds: int = 120
    openai_tool_choice: str = "required"
    osv_api_url: str = "https://api.osv.dev"

    @classmethod
    def from_env(cls, *, require_llm: bool = True) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL")

        if require_llm and not api_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is required. Copy .env.example and provide an API key."
            )
        if require_llm and not model:
            raise ConfigurationError(
                "OPENAI_MODEL is required. Model selection must remain configuration, not code."
            )

        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ConfigurationError("OPENAI_BASE_URL must be an HTTP(S) URL")

        github_api_url = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        if not github_api_url.startswith("https://"):
            raise ConfigurationError("GITHUB_API_URL must use HTTPS")

        osv_api_url = os.getenv("OSV_API_URL", "https://api.osv.dev").rstrip("/")
        if not osv_api_url.startswith("https://"):
            raise ConfigurationError("OSV_API_URL must use HTTPS")

        tool_choice = os.getenv("OPENAI_TOOL_CHOICE", "required").strip().lower()
        if tool_choice not in {"required", "auto"}:
            raise ConfigurationError("OPENAI_TOOL_CHOICE must be 'required' or 'auto'")

        reports_dir = data_dir / "reports"
        data_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            data_dir=data_dir,
            database_path=data_dir / "repo_detective.db",
            reports_dir=reports_dir,
            openai_api_key=api_key,
            openai_base_url=base_url,
            openai_model=model,
            github_token=os.getenv("GITHUB_TOKEN"),
            github_api_url=github_api_url,
            github_api_version=os.getenv("GITHUB_API_VERSION", "2026-03-10"),
            http_timeout_seconds=_positive_int("HTTP_TIMEOUT_SECONDS", 30),
            github_cache_ttl_seconds=_positive_int("GITHUB_CACHE_TTL_SECONDS", 900),
            max_tool_items=min(_positive_int("MAX_TOOL_ITEMS", 50), 100),
            max_file_chars=_positive_int("MAX_FILE_CHARS", 24_000),
            llm_timeout_seconds=_positive_int("LLM_TIMEOUT_SECONDS", 120),
            openai_tool_choice=tool_choice,
            osv_api_url=osv_api_url,
        )

