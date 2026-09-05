"""FastAPI application.

Phase 0: health endpoint, CORS, structured logging, real client IPs behind nginx.
Phase 1: the public WebSocket -- Origin check, per-IP limits, and the Groq
ladder streamed token by token.

Not here yet: TTS (Phase 2), voice (Phase 3), persistence (Phase 4), owner mode
and auth (Phase 4.5).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

from .config import Settings, get_settings
from .degraded import DegradedCapture
from .logging_setup import conversation_id_var, setup_logging
from .prompts.visitor import VISITOR_SYSTEM_PROMPT
from .providers.llm import (
    Completed,
    GroqLadder,
    LadderExhausted,
    StreamInterrupted,
    Token,
)
from .ratelimit import ConnectionLimiter, MessageRateLimiter
from .session import Conversation, SessionStore

VERSION: Final = "0.2.0"

# Rejecting a WebSocket handshake. 1008 is "policy violation".
WS_POLICY_VIOLATION: Final = 1008

settings: Settings = get_settings()
setup_logging(settings.log_level)
log = logging.getLogger("assistant.main")

sessions = SessionStore(idle_timeout_s=settings.session_idle_timeout_s)
connections = ConnectionLimiter(
    per_ip=settings.ws_max_connections_per_ip,
    total=settings.max_concurrent_text,
)
messages_limiter = MessageRateLimiter(per_minute=settings.ws_max_messages_per_minute)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.ladder = GroqLadder(settings)
    sweeper = asyncio.create_task(_evict_idle_sessions())
    log.info(
        "service started",
        extra={
            "version": VERSION,
            "env": settings.env,
            "allowed_origins": settings.allowed_origins,
            "ladder": [r.model for r in settings.ladder],
        },
    )
    try:
        yield
    finally:
        sweeper.cancel()
        await app.state.ladder.aclose()


async def _evict_idle_sessions() -> None:
    """Drop idle conversations and report quota headroom.

    Phase 4 replaces the eviction half with the database sweeper that also
    triggers ticket generation on the 5-minute inactivity timeout (§9).
    """
    while True:
        await asyncio.sleep(60)
        if evicted := sessions.evict_idle():
            log.info("evicted idle sessions", extra={"count": evicted})
        # §6: log remaining quota per model once a minute. We cannot tune what
        # we cannot see, and this is the only view of how close we are to the
        # degraded path.
        log.info("quota", extra={"remaining": app.state.ladder.ledger.snapshot()})


app = FastAPI(
    title="Personal Assistant",
    version=VERSION,
    lifespan=lifespan,
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_prod else "/openapi.json",
)

# §3.5: exact origins only; the config validator rejects wildcards.
#
# This protects HTTP only. CORS is NOT enforced on WebSocket handshakes -- the
# browser sends them cross-origin with no preflight -- so the WS endpoint below
# checks the Origin header itself. Do not assume this middleware covers it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


class HealthResponse(BaseModel):
    status: str
    version: str
    env: str
    # Echoed back so Phase 0's proxy-header wiring stays verifiable. If this
    # shows 127.0.0.1 for a remote caller, per-IP rate limiting has silently
    # collapsed into one shared bucket (§4.2).
    client_ip: str | None
    active_sessions: int
    open_connections: int


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=VERSION,
        env=settings.env,
        client_ip=request.client.host if request.client else None,
        active_sessions=len(sessions),
        open_connections=connections.total_open,
    )


class ClientMessage(BaseModel):
    type: str
    text: str = ""


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    # ---- Origin check, BEFORE accept ----
    # This is the only access control on a public, unauthenticated endpoint
    # that spends finite quota (§3.5, §4.1). CORS does not apply here.
    origin = websocket.headers.get("origin")
    if origin not in settings.allowed_origins:
        log.warning("ws rejected: origin not allowed", extra={"origin": origin})
        await websocket.close(code=WS_POLICY_VIOLATION, reason="origin not allowed")
        return

    ip = websocket.client.host if websocket.client else "unknown"
    if refusal := connections.try_acquire(ip):
        log.warning("ws rejected", extra={"reason": refusal, "client_ip": ip})
        await websocket.close(code=WS_POLICY_VIOLATION, reason=refusal)
        return

    await websocket.accept()
    conversation = sessions.create(VISITOR_SYSTEM_PROMPT)
    conversation_id_var.set(conversation.id)
    log.info("ws connected", extra={"client_ip": ip})

    try:
        await websocket.send_json(
            {"type": "ready", "conversation_id": conversation.id}
        )
        while True:
            raw = await websocket.receive_json()
            try:
                incoming = ClientMessage.model_validate(raw)
            except ValidationError:
                await websocket.send_json(
                    {"type": "error", "message": "malformed message"}
                )
                continue

            if incoming.type != "user_message":
                await websocket.send_json(
                    {"type": "error", "message": f"unknown type {incoming.type!r}"}
                )
                continue

            text = incoming.text.strip()
            if not text:
                continue
            if len(text) > 4000:
                await websocket.send_json(
                    {"type": "error", "message": "message too long"}
                )
                continue

            if not messages_limiter.allow(ip):
                log.warning("ws message rate limited", extra={"client_ip": ip})
                await websocket.send_json(
                    {"type": "error", "message": "You're sending messages too quickly."}
                )
                continue

            await _handle_turn(websocket, conversation_id=conversation.id, text=text)

    except WebSocketDisconnect:
        log.info("ws disconnected", extra={"client_ip": ip})
    finally:
        connections.release(ip)
        sessions.drop(conversation.id)


async def _say(
    websocket: WebSocket,
    conversation: Conversation,
    lines: list[str],
    *,
    model: str,
) -> None:
    """Send fixed text over the same wire shape as a streamed reply.

    The client renders it identically -- it should not be able to tell that no
    model was involved, beyond the label in the latency line.
    """
    text = " ".join(lines)
    conversation.add("agent", text)
    await websocket.send_json({"type": "token", "text": text})
    await websocket.send_json(
        {"type": "done", "model": model, "first_token_ms": 0, "total_ms": 0}
    )


async def _handle_turn(websocket: WebSocket, *, conversation_id: str, text: str) -> None:
    conversation = sessions.get(conversation_id)
    if conversation is None:
        await websocket.send_json({"type": "error", "message": "session expired"})
        return

    # §12: never log transcript content at INFO -- this is someone's message.
    log.debug("customer turn", extra={"chars": len(text)})
    conversation.add("customer", text)

    # Already in the scripted path: no model is involved, and none is needed.
    if conversation.degraded is not None:
        await _say(websocket, conversation, conversation.degraded.submit(text),
                   model="degraded-capture")
        return

    reply: list[str] = []
    ladder: GroqLadder = websocket.app.state.ladder
    try:
        async for event in ladder.stream(conversation.messages()):
            if isinstance(event, Token):
                reply.append(event.text)
                await websocket.send_json({"type": "token", "text": event.text})
            elif isinstance(event, Completed):
                conversation.add("agent", "".join(reply))
                await websocket.send_json(
                    {
                        "type": "done",
                        "model": event.model,
                        "first_token_ms": round(event.first_token_ms),
                        "total_ms": round(event.total_ms),
                    }
                )
    except LadderExhausted as e:
        # §6: never drop the caller. Switch to the scripted interview, which
        # needs no model, no quota and no synthesis. The product degrades to a
        # transcribing voicemail rather than an outage.
        log.warning("ladder exhausted, entering degraded capture", extra={"error": str(e)})
        conversation.degraded = DegradedCapture()
        await _say(
            websocket, conversation, conversation.degraded.open(), model="degraded-capture"
        )
    except StreamInterrupted as e:
        log.error("stream interrupted", extra={"error": str(e)})
        if reply:
            conversation.add("agent", "".join(reply), cancelled=True)
        await websocket.send_json(
            {"type": "error", "message": "That reply got cut off. Say that again?"}
        )
