"""Push notification for due reminders.

A reminder that only appears in a dashboard you have to remember to open is not
much of a reminder, so this pushes.

Default channel is **ntfy** — free, no account, no API key, and the phone app
subscribes to a topic. It is a plain HTTP POST, which is why it works without
any of the machinery a mail or messaging provider would need.

Two consequences of "no account" worth being honest about:

- **The topic name is the only secret.** Anyone who knows it can read your
  reminders and send you fake ones. Use a long random topic, not "charbel".
- Delivery is best-effort. If a notification fails the reminder is left unfired
  so the next sweep retries it, rather than being marked done and lost.

Set `NOTIFY_URL` to any endpoint that accepts a POST body; ntfy is only the
default, not a dependency.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("assistant.notify")


class Notifier:
    def __init__(self, url: str | None, *, timeout_s: float = 10.0) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self._url)

    async def send(self, *, title: str, body: str, tag: str = "bell") -> bool:
        """Push one notification. False means it did not go out.

        The caller must not mark the reminder fired on False -- an unfired
        reminder retries on the next sweep, a wrongly-fired one is simply lost.
        """
        if not self._url:
            # Not an error: notification is optional, and the dashboard still
            # shows the reminder. Logged so a silent phone is explainable.
            log.warning(
                "reminder due but no NOTIFY_URL configured", extra={"title": title}
            )
            return False

        try:
            response = await self._client.post(
                self._url,
                content=body.encode("utf-8"),
                headers={
                    # ntfy reads these; other endpoints ignore them harmlessly.
                    "Title": title.encode("ascii", "replace").decode(),
                    "Tags": tag,
                    "Priority": "default",
                },
            )
        except httpx.HTTPError as e:
            log.error("notification failed", extra={"error": f"{type(e).__name__}: {e}"})
            return False

        if response.status_code >= 400:
            log.error(
                "notification rejected",
                extra={"status": response.status_code, "body": response.text[:200]},
            )
            return False
        return True
