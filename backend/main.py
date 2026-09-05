"""FastAPI application.

Phase 0: health endpoint, CORS, structured logging, real client IPs behind nginx.
Phase 1: the public WebSocket -- Origin check, per-IP limits, and the Groq
ladder streamed token by token.

Not here yet: TTS (Phase 2), voice (Phase 3), persistence (Phase 4), owner mode
and auth (Phase 4.5).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ValidationError

from .config import OWNER_VOICE, VISITOR_VOICE, Settings, get_settings
from .degraded import ALL_FIXED_LINES, DegradedCapture
from .logging_setup import conversation_id_var, setup_logging
from .prompts.visitor import VISITOR_SYSTEM_PROMPT
from .providers.llm import (
    Completed,
    GroqLadder,
    LadderExhausted,
    StreamInterrupted,
    Token,
)
from .providers.stt import GroqWhisper, STTUnavailable, Transcript
from .providers.tts.base import Audio, TTSBackend, TTSUnavailable, VoiceProfile
from .providers.tts.cache import AudioCache
from .providers.tts.edge import EdgeTTS
from .providers.tts.engine import TTSEngine
from .providers.tts.fish import FishAudioTTS
from .providers.tts.piper import PiperTTS
from .ratelimit import ConnectionLimiter, MessageRateLimiter
from .sentences import SentenceSplitter
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
# §6: voice is capped separately and far lower. The binding constraint is
# Groq's 20 transcriptions/minute account-wide, not CPU -- an engaged speaker
# generates ~9 utterances/minute, so two concurrent voice sessions is the
# ceiling. A session becomes "voice" the first time it sends audio.
voice_sessions = ConnectionLimiter(per_ip=1, total=settings.max_concurrent_voice)
messages_limiter = MessageRateLimiter(per_minute=settings.ws_max_messages_per_minute)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.ladder = GroqLadder(settings)
    app.state.stt = GroqWhisper(settings)

    # Chain in descending quality, ascending reliability (§4). Fish is skipped
    # entirely when no key is configured, so the service still speaks.
    backends: list[TTSBackend] = []
    fish: FishAudioTTS | None = None
    if settings.fish_api_key:
        fish = FishAudioTTS(
            settings.fish_api_key,
            model=settings.fish_model,
            timeout_s=settings.fish_timeout_s,
            user_agent=settings.user_agent,
        )
        backends.append(fish)
    else:
        log.warning("no fish_api_key configured; TTS chain starts at edge-tts")
    backends.append(EdgeTTS())
    backends.append(
        PiperTTS(
            binary=settings.piper_binary,
            model=settings.piper_model,
            timeout_s=settings.piper_timeout_s,
        )
    )
    app.state.fish = fish
    app.state.tts = TTSEngine(
        backends=backends,
        cache=AudioCache(settings.tts_cache_dir, settings.tts_cache_max_bytes),
        failure_threshold=settings.tts_breaker_failures,
        cooldown_s=settings.tts_breaker_cooldown_s,
        force_backend=settings.tts_force_backend,
    )
    # §7.2: pre-render the fixed phrases so they cost nothing at call time, and
    # pin them so they survive eviction. This matters far more now that the
    # primary backend takes ~2s. Backgrounded -- a cold cache is slower, not
    # broken, and blocking startup on a third party would be daft.
    warm = asyncio.create_task(_warm_all(app))
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
        warm.cancel()
        await app.state.ladder.aclose()
        await app.state.stt.aclose()
        if app.state.fish is not None:
            await app.state.fish.aclose()


async def _warm_all(app: FastAPI) -> None:
    """Pre-render the fixed phrases in both voices.

    Owner and visitor use different voices, so the same line is two distinct
    cache entries. Both are pinned -- the degraded script must be instant in
    either mode, since it only runs when everything else has already failed.
    """
    for profile in (VISITOR_VOICE, OWNER_VOICE):
        await app.state.tts.warm(ALL_FIXED_LINES, profile)


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
    # base64 webm/opus from MediaRecorder, for type="audio"
    data: str = ""
    mime: str = "audio/webm"


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

    turn: asyncio.Task[None] | None = None
    holds_voice_slot = False

    async def cancel_turn() -> None:
        """Barge-in (§7.4): stop the reply the moment they start talking."""
        nonlocal turn
        if turn is None or turn.done():
            return
        turn.cancel()
        # Awaiting a task we just cancelled is *expected* to raise; there
        # is nothing to report, unlike a genuine except-pass.
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        # Tell the client to drop whatever audio it has queued. Without this it
        # keeps playing sentences the caller has already talked over.
        await websocket.send_json({"type": "interrupted"})
        log.info("turn cancelled by barge-in")

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

            # Sent the instant the client detects speech, before the audio
            # itself arrives, so playback stops without waiting for the upload.
            if incoming.type == "barge_in":
                await cancel_turn()
                continue

            if incoming.type not in ("user_message", "audio"):
                await websocket.send_json(
                    {"type": "error", "message": f"unknown type {incoming.type!r}"}
                )
                continue

            if not messages_limiter.allow(ip):
                log.warning("ws message rate limited", extra={"client_ip": ip})
                await websocket.send_json(
                    {"type": "error", "message": "You're sending messages too quickly."}
                )
                continue

            # Anything new supersedes the reply in flight.
            await cancel_turn()

            if incoming.type == "audio":
                if not holds_voice_slot:
                    if refusal := voice_sessions.try_acquire(ip):
                        # §6: the pre-rendered hold line, not an error -- the
                        # cap exists to protect Groq's STT budget, and the
                        # caller should not be made to feel it.
                        log.warning("voice refused", extra={"reason": refusal})
                        await _say(
                            websocket, conversation,
                            ["I'm sorry, could you hold for just a moment?"],
                            model="capacity-hold",
                        )
                        continue
                    holds_voice_slot = True
                    conversation.channel = "voice"

                text = await _transcribe(websocket, incoming)
                if text is None:
                    continue
            else:
                text = incoming.text.strip()

            if not text:
                continue
            if len(text) > 4000:
                await websocket.send_json(
                    {"type": "error", "message": "message too long"}
                )
                continue

            # Deliberately not awaited: the receive loop has to stay
            # responsive, or a barge_in cannot be read until the very turn it
            # is meant to cancel has already finished.
            turn = asyncio.create_task(
                _handle_turn(websocket, conversation_id=conversation.id, text=text)
            )
            turn.add_done_callback(_report_turn_failure)

    except WebSocketDisconnect:
        log.info("ws disconnected", extra={"client_ip": ip})
    except asyncio.CancelledError:
        raise
    finally:
        if turn is not None and not turn.done():
            turn.cancel()
        if holds_voice_slot:
            voice_sessions.release(ip)
        connections.release(ip)
        sessions.drop(conversation.id)


def _report_turn_failure(task: asyncio.Task[None]) -> None:
    """Surface exceptions from the un-awaited turn task.

    A fire-and-forget task swallows its exception unless someone looks, and the
    caller would just see the assistant stop replying.
    """
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        log.error("turn failed", extra={"error": repr(exc)}, exc_info=exc)


async def _transcribe(websocket: WebSocket, incoming: ClientMessage) -> str | None:
    """Turn an uploaded utterance into text, or None if it was not speech."""
    try:
        audio = base64.b64decode(incoming.data, validate=True)
    except (ValueError, binascii.Error):
        await websocket.send_json({"type": "error", "message": "malformed audio"})
        return None

    try:
        transcript: Transcript = await websocket.app.state.stt.transcribe(
            audio, mime=incoming.mime
        )
    except STTUnavailable as e:
        # Silence, noise, or a rate limit. Say nothing rather than inventing a
        # turn out of it -- Whisper hallucinates plausible speech for silence.
        log.warning("transcription failed", extra={"error": str(e)[:200]})
        await websocket.send_json({"type": "not_heard"})
        return None

    # Echo it back so the caller sees what was heard, which is the only way to
    # notice a misrecognition before it ends up in the ticket.
    await websocket.send_json({"type": "transcript", "text": transcript.text})
    return transcript.text


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

    # These are the pre-rendered, pinned fixed phrases, so this is a cache
    # hit and costs nothing -- which is the point, since the degraded path
    # only runs when everything else has already failed.
    profile = _voice_for(conversation)
    for line in lines:
        try:
            audio = await websocket.app.state.tts.synthesize(line, profile)
        except TTSUnavailable as e:
            log.error("degraded line could not be spoken",
                      extra={"error": str(e)[:200]})
            continue
        await websocket.send_json(
            {
                "type": "audio",
                "seq": 0,
                "mime": audio.mime,
                "data": base64.b64encode(audio.data).decode("ascii"),
            }
        )

    await websocket.send_json(
        {"type": "done", "model": model, "first_token_ms": 0, "total_ms": 0}
    )


def _voice_for(conversation: Conversation) -> VoiceProfile:
    """§1: owner and visitor hear different voices.

    Mode is set server-side from verified auth (§3.7), never from the
    client, so this cannot be steered by a caller asking nicely.
    """
    return OWNER_VOICE if conversation.mode == "owner" else VISITOR_VOICE


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
    splitter = SentenceSplitter()

    # §7.1 + §7.14: synthesis runs concurrently with generation so the first
    # sentence is already being spoken while the LLM writes the second, but
    # audio is *sent* strictly in submission order. Without the queue, a short
    # sentence -- or a cache hit racing a cache miss -- overtakes a long one and
    # the reply plays out of order.
    audio_queue: asyncio.Queue[asyncio.Task[Audio] | None] = asyncio.Queue()
    sender = asyncio.create_task(_send_audio_in_order(websocket, audio_queue))

    profile = _voice_for(conversation)
    cancelled = False

    def speak(sentence: str) -> None:
        audio_queue.put_nowait(
            asyncio.create_task(
                websocket.app.state.tts.synthesize(sentence, profile)
            )
        )

    try:
        async for event in ladder.stream(conversation.messages()):
            if isinstance(event, Token):
                reply.append(event.text)
                await websocket.send_json({"type": "token", "text": event.text})
                for sentence in splitter.feed(event.text):
                    speak(sentence)
            elif isinstance(event, Completed):
                # The final sentence has no trailing whitespace, so it never
                # matched a boundary -- this is how it gets spoken.
                if tail := splitter.flush():
                    speak(tail)
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
    except asyncio.CancelledError:
        # Barge-in. Abandon queued synthesis rather than draining it: §7.4 wants
        # the in-flight work cancelled, and audio nobody will hear still costs
        # Fish Audio quota and the caller's bandwidth.
        cancelled = True
        raise
    finally:
        if cancelled:
            sender.cancel()
        else:
            audio_queue.put_nowait(None)
        with contextlib.suppress(asyncio.CancelledError):
            await sender


async def _send_audio_in_order(
    websocket: WebSocket, queue: asyncio.Queue[asyncio.Task[Audio] | None]
) -> None:
    """Await synthesis tasks in submission order and forward the audio.

    Order is the whole point (§7.14). Awaiting each task in turn means a
    sentence that finishes early waits its turn rather than jumping the queue.

    Audio is base64 inside the JSON frame rather than a separate binary frame:
    a third of extra bytes on a ~40 KB sentence is nothing next to keeping the
    sequence number and the payload atomically together.
    """
    seq = 0
    pending: list[asyncio.Task[Audio]] = []
    try:
        while True:
            task = await queue.get()
            if task is None:
                return
            pending.append(task)
            seq += 1
            try:
                audio = await task
            except TTSUnavailable as e:
                # Both backends failed. The text already reached the client, so the
                # turn is not lost -- it is simply silent.
                log.error(
                    "synthesis failed for sentence",
                    extra={"seq": seq, "error": str(e)[:200]},
                )
                continue
            await websocket.send_json(
                {
                    "type": "audio",
                    "seq": seq,
                    "mime": audio.mime,
                    "data": base64.b64encode(audio.data).decode("ascii"),
                }
            )

    finally:
        # Barge-in cancels this task. Anything still synthesising is audio
        # nobody will hear, so stop paying for it (§7.4). The queue is drained
        # too, since tasks may have been enqueued after the last get().
        while not queue.empty():
            leftover = queue.get_nowait()
            if leftover is not None:
                pending.append(leftover)
        for outstanding in pending:
            if not outstanding.done():
                outstanding.cancel()
