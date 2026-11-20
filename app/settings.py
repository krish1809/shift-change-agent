"""All runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

DEV_JWT_SECRET = "dev-only-insecure-secret-change-me-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "prod", "test"] = "dev"
    database_url: str = "postgresql://shift:shift@db:5432/shift"
    jwt_secret: str = DEV_JWT_SECRET
    jwt_ttl_minutes: int = 480

    llm_provider: Literal["groq", "gemini", "fake", "rule_based"] = "groq"
    groq_api_key: str = ""
    groq_model: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    gemini_api_key: str = ""
    gemini_model: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_fallback_provider: Literal["groq", "gemini", ""] = "gemini"

    llm_timeout_ms: int = 4000
    max_llm_calls_per_turn: int = 4
    tool_timeout_ms: int = 1000
    turn_deadline_ms: int = 8000
    thread_lock_wait_ms: int = 2000
    sweeper_interval_s: float = 15.0

    # "if_empty" seeds only a fresh database, so a restart never wipes pending approvals.
    seed_on_start: Literal["if_empty", "always", "never", "true", "false"] = "if_empty"
    seed_base_date: str = ""
    clock_override: str = ""
    faults: str = ""
    log_level: str = "INFO"

    @field_validator("llm_fallback_provider", "seed_base_date", "clock_override", "faults", mode="before")
    @classmethod
    def _blank_is_empty(cls, value: object) -> object:
        return "" if value is None else str(value).strip()

    def provider_configured(self, provider: str) -> bool:
        if provider == "groq":
            return bool(self.groq_api_key and self.groq_model)
        if provider == "gemini":
            return bool(self.gemini_api_key and self.gemini_model)
        return provider in ("fake", "rule_based")

    def effective_llm_provider(self) -> str:
        """The provider we will really use: without a key we degrade to the rule-based interpreter."""
        if self.provider_configured(self.llm_provider):
            return self.llm_provider
        log.warning(
            "LLM provider %r has no API key/model configured; using the rule_based interpreter",
            self.llm_provider,
        )
        return "rule_based"

    @property
    def seed_mode(self) -> str:
        return {"true": "if_empty", "false": "never"}.get(self.seed_on_start, self.seed_on_start)


@lru_cache
def get_settings() -> Settings:
    return Settings()
