"""Configuration loaded from the environment.

Rule from INSTRUCTIONS.md §3.4: no defaults for secrets. Every secret declared
here is required, and startup fails loudly if it is missing. Fields are added
per phase rather than declared up front with placeholder values -- a config
that lies about what it needs is worse than one that fails fast.

Phase 0 needs no secrets at all.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so the .env file is read once per process."""
    return Settings()
