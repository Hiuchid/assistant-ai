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
from typing import Any, Literal, Protocol, Self, TypedDict

import httpx

from ..config import LadderRung, Settings
from ..quota import QuotaLedger, estimate_tokens, parse_reset

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
        self.ledger = QuotaLedger(settings.ladder)
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
        estimated = estimate_tokens(
            [m["content"] for m in messages], max_output=self._settings.llm_max_tokens
        )

        for rung in self._settings.ladder:
            # §6: consult the ledger before dispatch. A rung we already know is
            # exhausted costs a full round-trip to rediscover, and the 429 that
            # comes back lands on the caller's latency, not ours.
            if (refusal := self.ledger.refusal(rung, estimated)) is not None:
                failures.append(f"{rung.model}: {refusal}")
                log.info(
                    "skipping rung on predicted quota",
                    extra={"model": rung.model, "reason": refusal},
                )
                continue

            emitted = False
            try:
                async for event in self._stream_one(rung, messages, estimated):
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
        self, rung: LadderRung, messages: Sequence[Message], estimated: int
    ) -> AsyncIterator[Event]:
        started = time.perf_counter()
        first_token_ms = 0.0
        prompt_tokens = completion_tokens = 0
        quota = self.ledger.get(rung)

        # Charge the estimate up front. If two turns dispatch concurrently,
        # the second must see the first's cost even though no response has
        # come back yet. reconcile() corrects it to the real figure below.
        quota.record_dispatch(estimated)

        async with self._client.stream(
            "POST", "/chat/completions", json=self._payload(rung, messages)
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")[:300]
                if response.status_code == 429:
                    # Trust the provider over our own estimate: it just told us
                    # exactly how long this bucket is unusable for.
                    retry_after = parse_reset(response.headers.get("retry-after"))
                    quota.mark_cold(retry_after if retry_after is not None else 60.0)
                raise _RungUnavailable(
                    _describe_http_error(response.status_code, response.headers, body)
                )
            headers = response.headers

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

        # The provider's own figures are authoritative -- they account for usage
        # from anywhere sharing this key, not just this process.
        quota.reconcile(
            remaining_requests=headers.get("x-ratelimit-remaining-requests"),
            remaining_tokens=headers.get("x-ratelimit-remaining-tokens"),
            reset_requests=headers.get("x-ratelimit-reset-requests"),
            reset_tokens=headers.get("x-ratelimit-reset-tokens"),
            actual_tokens=prompt_tokens + completion_tokens,
            estimated_tokens=estimated,
        )

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


# --------------------------------------------------------------------------
# Tool-using completions (owner mode only)
#
# Deliberately NOT streamed. A tool loop has to see a whole response before it
# knows whether to run a tool or reply, so streaming would buy nothing but
# complexity -- and owner replies are a sentence or two, arriving in ~200ms.


@dataclass(frozen=True)
class ToolTurn:
    text: str
    model: str
    hops: int
    tools_used: list[str]
    total_ms: float


class ToolDispatcher(Protocol):
    async def run(self, name: str, args: dict[str, Any]) -> Any: ...


MAX_HOPS = 5


class ToolLadder(GroqLadder):
    """GroqLadder plus a tool-calling loop."""

    async def complete_with_tools(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        dispatcher: ToolDispatcher,
        *,
        max_hops: int = MAX_HOPS,
    ) -> ToolTurn:
        started = time.perf_counter()
        convo: list[dict[str, Any]] = [dict(m) for m in messages]
        used: list[str] = []
        failures: list[str] = []

        rungs = [r for r in self._settings.ladder if r.supports_tools]
        estimated = estimate_tokens(
            [str(m.get("content") or "") for m in convo],
            max_output=self._settings.llm_max_tokens,
        )

        for rung in rungs:
            if (refusal := self.ledger.refusal(rung, estimated)) is not None:
                failures.append(f"{rung.model}: {refusal}")
                continue
            try:
                for hop in range(max_hops):
                    reply = await self._one_completion(rung, convo, tools, estimated)
                    calls = reply.get("tool_calls") or []
                    if not calls:
                        return ToolTurn(
                            text=(reply.get("content") or "").strip(),
                            model=rung.model,
                            hops=hop,
                            tools_used=used,
                            total_ms=(time.perf_counter() - started) * 1000,
                        )

                    # The assistant turn carrying the calls must be replayed
                    # verbatim, or the follow-up messages have nothing to
                    # attach their tool_call_id to.
                    convo.append(reply)
                    for call in calls:
                        name = call["function"]["name"]
                        try:
                            args = json.loads(call["function"].get("arguments") or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        used.append(name)
                        log.info("tool call", extra={"tool": name, "model": rung.model})
                        result = await dispatcher.run(name, args)
                        convo.append({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": name,
                            "content": json.dumps(result, default=str)[:6000],
                        })

                # Ran out of hops. Better a plain answer than an endless loop.
                log.warning("tool loop hit the hop limit", extra={"model": rung.model})
                return ToolTurn(
                    text="I got a bit tangled up there. "
                         "Could you ask me again, more simply?",
                    model=rung.model, hops=max_hops, tools_used=used,
                    total_ms=(time.perf_counter() - started) * 1000,
                )
            except _RungUnavailable as e:
                failures.append(f"{rung.model}: {e}")
                log.warning("tool rung unavailable",
                            extra={"model": rung.model, "reason": str(e)})
                continue

        raise LadderExhausted("; ".join(failures) or "no tool-capable rung available")

    async def _one_completion(
        self, rung: LadderRung, convo: list[dict[str, Any]],
        tools: list[dict[str, Any]], estimated: int,
    ) -> dict[str, Any]:
        quota = self.ledger.get(rung)
        quota.record_dispatch(estimated)

        payload: dict[str, object] = {
            "model": rung.model,
            "messages": convo,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self._settings.llm_temperature,
            "max_tokens": self._settings.llm_max_tokens,
        }
        if rung.reasoning_effort is not None:
            payload["reasoning_effort"] = rung.reasoning_effort

        response = await self._client.post("/chat/completions", json=payload)
        if response.status_code != 200:
            if response.status_code == 429:
                retry_after = parse_reset(response.headers.get("retry-after"))
                quota.mark_cold(retry_after if retry_after is not None else 60.0)
            raise _RungUnavailable(
                _describe_http_error(response.status_code, response.headers,
                                     response.text[:300])
            )

        body = response.json()
        usage = body.get("usage") or {}
        quota.reconcile(
            remaining_requests=response.headers.get("x-ratelimit-remaining-requests"),
            remaining_tokens=response.headers.get("x-ratelimit-remaining-tokens"),
            reset_requests=response.headers.get("x-ratelimit-reset-requests"),
            reset_tokens=response.headers.get("x-ratelimit-reset-tokens"),
            actual_tokens=(
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            ),
            estimated_tokens=estimated,
        )
        message: dict[str, Any] = body["choices"][0]["message"]
        # Reasoning is internal; replaying it wastes tokens on every hop.
        message.pop("reasoning", None)
        return message
