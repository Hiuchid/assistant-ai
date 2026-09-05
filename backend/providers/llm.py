"""Groq chat completions with streaming and a fallthrough ladder.

INSTRUCTIONS.md §4: Groq only, laddered across models. Rate limits are
per-organization *and per model*, so each rung is its own quota bucket and they
stack. Order is by measured latency and cost (§4), configured in config.py.

Phase 1 falls through reactively -- send, and on failure try the next rung.
Phase 1.5 adds the local quota ledger so exhaustion is predicted before dispatch
rather than discovered by burning a request.

Kept behind a narrow interface so a second provider is a new file plus a config
line, never a refactor. Do not ship a second provider (§4).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Self, TypedDict

import httpx

from ..config import LadderRung, Settings

log = logging.getLogger("assistant.llm")


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Token:
    """A fragment of assistant text, already stripped of reasoning output."""

    text: str


@dataclass(frozen=True)
class Completed:
    """Emitted once, after the last token."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    first_token_ms: float
    total_ms: float


Event = Token | Completed


class LadderExhausted(RuntimeError):
    """Every rung failed before producing a token.

    The caller should fall back to the scripted no-LLM path (§6 degraded mode,
    built in Phase 1.5) rather than dropping the conversation.
    """


class StreamInterrupted(RuntimeError):
    """A rung failed *after* it had already emitted tokens.

    Deliberately not retried on the next rung: the client has already rendered
    partial text, and replaying a different model's answer over the top would
    produce a visibly incoherent reply. The turn fails and the caller decides.
    """


class GroqLadder:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """`transport` exists so tests can drive the ladder without a network.

        Nothing in production passes it.
        """
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.groq_base_url,
            transport=transport,
            timeout=httpx.Timeout(settings.llm_timeout_s, connect=10.0),
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
                # Groq is behind Cloudflare and rejects unusual User-Agents
                # with "error code: 1010". httpx's own default is fine, but be
                # explicit so this is never mistaken for optional.
                "User-Agent": settings.user_agent,
            },
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(self, rung: LadderRung, messages: Sequence[Message]) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": rung.model,
            "messages": list(messages),
            "temperature": self._settings.llm_temperature,
            "max_tokens": self._settings.llm_max_tokens,
            "stream": True,
            # Ask for the usage chunk so the Phase 1.5 ledger reconciles against
            # real numbers instead of estimates.
            "stream_options": {"include_usage": True},
        }
        if rung.reasoning_effort is not None:
            payload["reasoning_effort"] = rung.reasoning_effort
        return payload

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[Event]:
        """Stream a reply, falling through the ladder on failure.

        Yields Token events followed by exactly one Completed event.
        """
        failures: list[str] = []

        for rung in self._settings.ladder:
            emitted = False
            try:
                async for event in self._stream_one(rung, messages):
                    if isinstance(event, Token):
                        emitted = True
                    yield event
                return
            except _RungUnavailable as e:
                # Nothing was sent to the client, so the next rung can take over
                # cleanly. This is the normal, expected fallthrough path.
                failures.append(f"{rung.model}: {e}")
                log.warning(
                    "ladder rung unavailable, falling through",
                    extra={"model": rung.model, "reason": str(e)},
                )
                continue
            except Exception as e:
                if emitted:
                    # Mid-stream failure. Do not retry on another rung -- see
                    # StreamInterrupted.
                    log.error(
                        "stream failed after emitting tokens",
                        extra={"model": rung.model, "error": repr(e)},
                    )
                    raise StreamInterrupted(str(e)) from e
                failures.append(f"{rung.model}: {e!r}")
                log.warning(
                    "ladder rung errored before emitting, falling through",
                    extra={"model": rung.model, "error": repr(e)},
                )
                continue

        raise LadderExhausted("; ".join(failures))

    async def _stream_one(
        self, rung: LadderRung, messages: Sequence[Message]
    ) -> AsyncIterator[Event]:
        started = time.perf_counter()
        first_token_ms = 0.0
        prompt_tokens = completion_tokens = 0

        async with self._client.stream(
            "POST", "/chat/completions", json=self._payload(rung, messages)
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")[:300]
                raise _RungUnavailable(
                    _describe_http_error(response.status_code, response.headers, body)
                )

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    # A malformed frame mid-stream is not recoverable by
                    # switching models; surface it rather than swallowing it.
                    log.error("malformed SSE frame", extra={"model": rung.model})
                    raise

                if usage := chunk.get("usage"):
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)

                for choice in chunk.get("choices", ()):
                    # `reasoning` is deliberately ignored: gpt-oss models emit
                    # it before content and it is not part of the reply.
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        if first_token_ms == 0.0:
                            first_token_ms = (time.perf_counter() - started) * 1000
                        yield Token(text)

        total_ms = (time.perf_counter() - started) * 1000
        log.info(
            "llm turn complete",
            extra={
                "model": rung.model,
                "llm_first_token_ms": round(first_token_ms),
                "llm_total_ms": round(total_ms),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        )
        yield Completed(
            model=rung.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            first_token_ms=first_token_ms,
            total_ms=total_ms,
        )


class _RungUnavailable(RuntimeError):
    """This rung cannot serve the request; try the next one."""


def _describe_http_error(status: int, headers: httpx.Headers, body: str) -> str:
    if status == 429:
        retry_after = headers.get("retry-after", "?")
        return f"429 rate limited (retry-after={retry_after})"
    if status == 401:
        # Not a quota problem and the next rung will fail identically, but
        # falling through costs one request and makes the cause obvious in the
        # logs rather than hanging the turn.
        return "401 unauthorized -- check GROQ_API_KEY"
    return f"HTTP {status}: {body}"
