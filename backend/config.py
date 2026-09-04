"""Configuration loaded from the environment.

Rule from INSTRUCTIONS.md §3.4: no defaults for secrets. Every secret declared
here is required, and startup fails loudly if it is missing. Fields are added
per phase rather than declared up front with placeholder values -- a config
that lies about what it needs is worse than one that fails fast.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ReasoningEffort = Literal["low", "medium", "high"]


class LadderRung(BaseModel):
    """One model in the fallthrough ladder (§4)."""

    model: str
    # gpt-oss models emit reasoning tokens before content. Left unset they burn
    # the completion budget and return an empty string with
    # finish_reason=length -- silently. "low" is required on those; "none" is
    # rejected by the API. qwen and compound are not reasoning models.
    reasoning_effort: ReasoningEffort | None = None
    requests_per_day: int
    tokens_per_minute: int
    note: str = ""


# Ordered by measured latency and cost, not by presumed quality -- see §4.
# This is the config point: reorder, add or drop rungs here. Phase 1.5 adds the
# quota ledger that consults these limits before dispatch; Phase 1 only falls
# through reactively on 429.
DEFAULT_LADDER: tuple[LadderRung, ...] = (
    LadderRung(
        model="qwen/qwen3.8-27b",
        requests_per_day=1000,
        tokens_per_minute=8000,
        note="87ms, 87 tok/turn measured -- fastest and cheapest",
    ),
    LadderRung(
        model="openai/gpt-oss-20b",
        reasoning_effort="low",
        requests_per_day=1000,
        tokens_per_minute=8000,
        note="188ms, 153 tok/turn; separate quota bucket",
    ),
    LadderRung(
        model="openai/gpt-oss-120b",
        reasoning_effort="low",
        requests_per_day=1000,
        tokens_per_minute=8000,
        note="323ms, 157 tok/turn; best quality of the set",
    ),
    LadderRung(
        model="groq/compound-mini",
        requests_per_day=250,
        tokens_per_minute=70000,
        note="last resort: ~470-token agentic preamble, only 250 req/day",
    ),
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Phase 0: service ----
    env: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000

    # Exact origins allowed to call the API and open a WebSocket.
    # §3.5: no wildcards, ever. Enforced by the validator below.
    #
    # NoDecode is required: without it pydantic-settings tries json.loads() on
    # the raw env value before any validator runs, so a comma-separated list --
    # or an empty value -- raises SettingsError. NoDecode hands us the raw
    # string and lets _split_csv do the parsing.
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Reverse-proxy addresses whose X-Forwarded-For we trust.
    # §4.2 / Phase 0: without this, per-IP rate limiting collapses into a
    # single bucket keyed on the proxy and the first visitor locks everyone out.
    trusted_proxies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.1"]
    )

    # ---- Phase 1: LLM ----
    groq_api_key: str  # no default: required, fails loudly if absent
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Groq sits behind Cloudflare and rejects unusual User-Agents with
    # "error code: 1010". Python's default urllib UA is blocked outright.
    user_agent: str = "assistant-ai/0.1"

    llm_temperature: float = 0.3
    llm_max_tokens: int = 400
    llm_timeout_s: float = 30.0

    # §6: derived from Groq's 20 RPM STT ceiling, not from CPU. Text costs no
    # STT, so it runs higher. Voice arrives in Phase 3.
    max_concurrent_text: int = 4

    # §12: the public WebSocket is unauthenticated, on the open internet, and
    # spends finite quota. Per-IP limits, not global.
    ws_max_connections_per_ip: int = 3
    ws_max_messages_per_minute: int = 20

    # How long a conversation may sit idle before its in-memory state is
    # dropped. Phase 4 replaces this with the database + sweeper.
    session_idle_timeout_s: int = 900

    @field_validator("allowed_origins", "trusted_proxies", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept comma-separated strings from .env as well as real lists."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("allowed_origins")
    @classmethod
    def _no_wildcards(cls, v: list[str]) -> list[str]:
        for origin in v:
            if "*" in origin:
                raise ValueError(
                    f"wildcard origin {origin!r} rejected: INSTRUCTIONS.md §3.5 "
                    "requires exact origins on both HTTP and WebSocket"
                )
            if not origin.startswith(("http://", "https://")):
                raise ValueError(
                    f"origin {origin!r} must include a scheme, e.g. https://example.com"
                )
        return v

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def ladder(self) -> tuple[LadderRung, ...]:
        return DEFAULT_LADDER


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the .env file is read once per process."""
    return Settings()
