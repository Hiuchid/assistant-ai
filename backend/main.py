"""FastAPI application.

Phase 0 scope (INSTRUCTIONS.md §10): health endpoint, CORS locked to an exact
origin allowlist, structured logging, and correct client-IP resolution behind
nginx. The WebSocket endpoint arrives in Phase 1.
"""

from __future__ import annotations

import logging
from typing import Final

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import Settings, get_settings
from .logging_setup import setup_logging

VERSION: Final = "0.1.0"

settings: Settings = get_settings()
setup_logging(settings.log_level)
log = logging.getLogger("assistant.main")

app = FastAPI(
    title="Personal Assistant",
    version=VERSION,
    # The API is not public documentation; do not advertise it in production.
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_prod else "/openapi.json",
)

# §3.5: exact origins only. The validator in config.py rejects wildcards, so
# this list is trustworthy by the time it reaches here.
#
# Note this protects HTTP only. CORS is not enforced on WebSocket handshakes,
# so the Phase 1 WS endpoint must validate the Origin header itself against
# the same allowlist. Do not assume this middleware covers it.
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
    # Echoed back so Phase 0 can verify that nginx's X-Forwarded-For is being
    # honoured. If this shows 127.0.0.1 for a remote caller, per-IP rate
    # limiting would silently collapse into one shared bucket (§4.2).
    client_ip: str | None


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=VERSION,
        env=settings.env,
        client_ip=request.client.host if request.client else None,
    )


@app.on_event("startup")
async def on_startup() -> None:
    log.info(
        "service started",
        extra={
            "version": VERSION,
            "env": settings.env,
            "allowed_origins": settings.allowed_origins,
            "port": settings.port,
        },
    )
