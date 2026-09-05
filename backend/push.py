"""Web Push.

Replaces ntfy. The difference that matters is not convenience but security:
an ntfy topic is a shared secret in all but name -- anyone who learns it can
read the reminders and forge new ones. A Web Push subscription is bound to one
browser install and one VAPID key pair, and the payload is encrypted end to end
by the browser's own push service.

It is also free, with no account and no third-party app: delivery goes through
whichever push service the browser already uses (FCM for Chrome, Mozilla's for
Firefox, Apple's for Safari).

**The iOS caveat is real and worth knowing:** Safari only delivers Web Push to
sites the user has added to the Home Screen, on iOS 16.4 or later. On iOS, "add
to home screen" is not a nicety -- it is the thing that makes notifications work
at all.

Sending is blocking, so it runs in a thread rather than on the event loop.
pywebpush is built on requests, not httpx, and wrapping it is cheaper than
reimplementing RFC 8291 message encryption.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from pywebpush import WebPushException, webpush

log = logging.getLogger("assistant.push")

# Some push services reject very large payloads; keep well inside the limit.
MAX_PAYLOAD_BYTES = 3000


@dataclass(frozen=True)
class Subscription:
    id: str
    endpoint: str
    p256dh: str
    auth: str


@dataclass(frozen=True)
class PushResult:
    delivered: int
    expired: list[str]
    """Subscription ids the push service says no longer exist.

    These are not failures to retry -- the install is gone (app deleted,
    permission revoked, browser data cleared) and re-sending will never work.
    """


class PushSender:
    def __init__(self, private_key: str | None, subject: str) -> None:
        self._private_key = private_key
        # RFC 8292 requires a contact so a push service can reach the sender
        # about abuse. mailto: or an https URL.
        self._claims = {"sub": subject}

    @property
    def configured(self) -> bool:
        return bool(self._private_key)

    def _send_one(self, sub: Subscription, payload: str) -> str | None:
        """Returns None on success, or 'expired' / an error string."""
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=self._private_key,
                vapid_claims=dict(self._claims),
                timeout=10,
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                return "expired"
            return f"HTTP {status}: {str(e)[:120]}"
        except Exception as e:  # network, DNS, TLS
            return f"{type(e).__name__}: {str(e)[:120]}"
        return None

    async def send(
        self,
        subscriptions: list[Subscription],
        *,
        title: str,
        body: str,
        url: str = "/",
        tag: str | None = None,
    ) -> PushResult:
        """Push to every subscription. Never raises.

        One dead install must not stop the others being notified, so each is
        attempted independently and failures are collected rather than thrown.
        """
        if not self._private_key or not subscriptions:
            if not self._private_key:
                log.warning("push not configured", extra={"title": title})
            return PushResult(delivered=0, expired=[])

        payload = json.dumps(
            {"title": title, "body": body[:400], "url": url, "tag": tag}
        )
        if len(payload) > MAX_PAYLOAD_BYTES:
            payload = json.dumps({"title": title, "body": "(too long to show)", "url": url})

        delivered = 0
        expired: list[str] = []
        for sub in subscriptions:
            outcome = await asyncio.to_thread(self._send_one, sub, payload)
            if outcome is None:
                delivered += 1
            elif outcome == "expired":
                expired.append(sub.id)
                log.info("push subscription gone", extra={"subscription_id": sub.id})
            else:
                log.error(
                    "push failed",
                    extra={"subscription_id": sub.id, "error": outcome},
                )
        return PushResult(delivered=delivered, expired=expired)


def rows_to_subscriptions(rows: list[dict[str, Any]]) -> list[Subscription]:
    return [
        Subscription(
            id=str(r["id"]),
            endpoint=r["endpoint"],
            p256dh=r["p256dh"],
            auth=r["auth"],
        )
        for r in rows
    ]
