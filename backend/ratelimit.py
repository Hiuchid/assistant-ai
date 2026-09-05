"""Per-IP limits for the public WebSocket.

INSTRUCTIONS.md §12: the endpoint is unauthenticated, on the open internet, and
every message spends finite Groq quota. The box is already being swept by
scanners, so this is not theoretical.

Limits are per-IP, which only works because uvicorn runs with --proxy-headers
and --forwarded-allow-ips. Without those, request.client.host is nginx and every
visitor shares one bucket -- the failure mode §4.2 warns about. Phase 0 verified
the real client IP arrives.

In-memory and per-process, which is correct for a single-worker personal
assistant. It would need shared state if this ever ran multi-worker.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class ConnectionLimiter:
    """Caps concurrent connections per IP and across the service."""

    def __init__(self, *, per_ip: int, total: int) -> None:
        self._per_ip = per_ip
        self._total = total
        self._by_ip: dict[str, int] = defaultdict(int)
        self._total_open = 0

    @property
    def total_open(self) -> int:
        return self._total_open

    def try_acquire(self, ip: str) -> str | None:
        """Reserve a slot. Returns None on success, or a reason for refusal."""
        if self._total_open >= self._total:
            return "at_capacity"
        if self._by_ip[ip] >= self._per_ip:
            return "too_many_connections"
        self._by_ip[ip] += 1
        self._total_open += 1
        return None

    def release(self, ip: str) -> None:
        if self._by_ip.get(ip):
            self._by_ip[ip] -= 1
            if self._by_ip[ip] == 0:
                del self._by_ip[ip]
        # Guard against double-release leaving a permanently consumed slot.
        self._total_open = max(0, self._total_open - 1)


class MessageRateLimiter:
    """Sliding-window message cap per IP."""

    def __init__(self, *, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        window = self._hits[ip]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= self._per_minute:
            return False
        window.append(now)
        return True

    def forget(self, ip: str) -> None:
        self._hits.pop(ip, None)
