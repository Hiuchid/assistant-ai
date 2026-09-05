"""Configuration loaded from the environment.

Rule from INSTRUCTIONS.md §3.4: no defaults for secrets. Every secret declared
here is required, and startup fails loudly if it is missing. Fields are added
per phase rather than declared up front with placeholder values -- a config
that lies about what it needs is worse than one that fails fast.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .providers.tts.base import Prosody, VoiceProfile

ReasoningEffort = Literal["low", "medium", "high"]


class LadderRung(BaseModel):
    """One model in the fallthrough ladder (§4)."""

    model: str
    # gpt-oss models emit reasoning tokens before content. Left unset they burn
    # the completion budget and return an empty string with
    # finish_reason=length -- silently. "low" is required on those; "none" is
    # rejected by the API. qwen and compound are not reasoning models.
    reasoning_effort: ReasoningEffort | None = None

    # Limits feeding the quota ledger (§6). requests_per_day and
    # tokens_per_minute were read from live response headers on this account;
    # requests_per_minute and tokens_per_day come from Groq's published table
    # and are NOT exposed in the headers, so treat them as unverified. They are
    # deliberately conservative -- the ledger errs toward refusing early.
    requests_per_minute: int = 30
    requests_per_day: int
    tokens_per_minute: int
    tokens_per_day: int = 200_000
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
        # Not published for this model. Held low on purpose: the ~470-token
        # preamble makes every call expensive, and this is the last rung
        # standing between a caller and the degraded script.
        tokens_per_day=100_000,
        note="last resort: ~470-token agentic preamble, only 250 req/day",
    ),
)


# §1 gives owner and visitor modes different voices, chosen by ear in Phase 2.
#
# No pitch shifting: it made edge-tts sound robotic, and measurement showed why
# -- each -4Hz of parameter moves real F0 by only ~2.2Hz, so reaching a properly
# deep voice needs roughly -56Hz, far past where the artefacts start. Rate is a
# time-stretch and does not touch timbre, so -8% is safe.
#
# Voice ids are per backend and not interchangeable. The chain can move
# synthesis between backends at any time, so every profile names one for each.
OWNER_VOICE = VoiceProfile(
    label="owner",
    voices={
        # Community Jarvis model. A third-party clone of a real actor's voice,
        # used here for a private assistant only -- which is why visitor mode
        # below deliberately uses a generic voice instead.
        "fish": "14129c3e320149449d6bada6862f7338",
        "edge": "en-GB-RyanNeural",
        "piper": "",  # single-voice backend; the model file is the voice
    },
    prosody=Prosody(rate="-8%"),
)

VISITOR_VOICE = VoiceProfile(
    label="visitor",
    voices={
        "fish": "2e5038d2022e4e4a89142dddae45a284",  # "Elderly British Butler"
        "edge": "en-GB-ThomasNeural",
        "piper": "",
    },
    prosody=Prosody(rate="-8%"),
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

    # ---- Phase 2: TTS ----
    # Optional, unlike the Groq key. Absent, the Fish backend is simply not
    # added to the chain and synthesis starts at edge-tts -- the service still
    # boots and still speaks. This is "no default for a secret" (§3.4), not a
    # placeholder: None means genuinely absent.
    fish_api_key: str | None = None
    fish_model: str = "s2.1-pro-free"
    fish_timeout_s: float = 30.0

    tts_cache_dir: Path = Path("audio_cache")
    # 100 GB of disk is available; 2 GB is far more than the fixed phrases plus
    # a long tail of repeats will ever need (§4).
    tts_cache_max_bytes: int = 2 * 1024 * 1024 * 1024

    # Automatic, not manual (§4). This flag is for forcing a backend during
    # benchmarking only -- the circuit breaker is what protects production.
    tts_force_backend: str | None = None
    tts_breaker_failures: int = 3
    tts_breaker_cooldown_s: float = 300.0

    # ---- Phase 3: voice input ----
    stt_model: str = "whisper-large-v3-turbo"
    # Pinned rather than auto-detected: Whisper hallucinates translations on
    # short clips otherwise. §14 revisits this for Arabic, where mid-sentence
    # code-switching makes a single forced language the wrong answer.
    stt_language: str = "en"
    stt_timeout_s: float = 30.0

    # §6: derived from Groq's 20 requests/minute STT ceiling, not from CPU.
    # An engaged speaker produces ~9 utterances/minute, so two concurrent voice
    # sessions is the real ceiling. Re-derive if Groq's limits change, then
    # check CPU -- it should not be tighter, but measure rather than assume.
    max_concurrent_voice: int = 2

    piper_binary: Path = Path(".venv/bin/piper")
    piper_model: Path = Path("voices/en_GB-alan-medium.onnx")
    piper_timeout_s: float = 60.0

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
