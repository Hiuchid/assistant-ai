"""Supabase writes.

INSTRUCTIONS.md §3.3: the database is the source of truth, not the client.
Turns are written as they happen, and the item is always built from what is in
Postgres -- never from a transcript the client hands us at the end.

**Direct Postgres rather than the REST API.** The plan said "Supabase writes
(service role key)". A direct asyncpg connection as the `postgres` role does
the same job, bypasses PostgREST entirely, gives real transactions, and needs
one fewer credential.

Since users are our own rows rather than Supabase Auth, nothing but this
process ever connects: the dashboard reads through our API, not through
PostgREST. RLS is therefore enabled with no policies at all -- deny-by-default
for every role except the owner, which is us.

The connection is IPv6-only -- Supabase moved direct connections off IPv4, and
`db.<ref>.supabase.co` has no A record. This VPS has IPv6 egress; a host
without it would need the pooler and a different username format.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Self

import asyncpg

log = logging.getLogger("assistant.persistence")

Role = Literal["customer", "agent"]
Mode = Literal["owner", "visitor"]
Channel = Literal["text", "voice"]


@dataclass(frozen=True)
class StoredTurn:
    seq: int
    role: Role
    text: str
    cancelled: bool


class PersistenceError(RuntimeError):
    """A write failed.

    Deliberately does not abort the conversation. §6's philosophy is that the
    caller is never dropped, and a lost row is better than a dead call -- but it
    is logged at ERROR because the item built from a gappy transcript will be
    wrong, and that must be visible rather than silent.
    """


class Store:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool[Any] | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=10.0,
            # Supabase free tier idles the compute; a stale pooled connection
            # otherwise surfaces as a confusing first-write failure.
            max_inactive_connection_lifetime=180.0,
        )
        log.info("persistence pool ready", extra={"max_size": self._max_size})

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool[Any]:
        if self._pool is None:
            raise PersistenceError("persistence pool not initialised")
        return self._pool

    # ------------------------------------------------------- conversations

    async def create_conversation(
        self,
        *,
        mode: Mode,
        channel: Channel,
        lang: str = "en-GB",
        visitor_ref: str | None = None,
    ) -> str:
        row = await self.pool.fetchrow(
            """
            insert into conversations (mode, channel, lang, visitor_ref)
            values ($1, $2, $3, $4)
            returning id::text
            """,
            mode, channel, lang, visitor_ref,
        )
        if row is None:
            raise PersistenceError("conversation insert returned nothing")
        return str(row["id"])

    async def set_channel(self, conversation_id: str, channel: Channel) -> None:
        """A session becomes 'voice' the first time it sends audio."""
        await self.pool.execute(
            "update conversations set channel = $2 where id = $1::uuid",
            conversation_id, channel,
        )

    async def mark_degraded(self, conversation_id: str) -> None:
        await self.pool.execute(
            "update conversations set degraded = true where id = $1::uuid",
            conversation_id,
        )

    async def end_conversation(self, conversation_id: str) -> None:
        await self.pool.execute(
            """
            update conversations
               set ended_at = now(), status = 'ended'
             where id = $1::uuid and ended_at is null
            """,
            conversation_id,
        )

    # --------------------------------------------------------------- turns

    async def add_turn(
        self,
        conversation_id: str,
        *,
        role: Role,
        text: str,
        latency_ms: int | None = None,
        cancelled: bool = False,
    ) -> int:
        """Append a turn and bump the conversation's activity clock.

        Both in one statement so a conversation can never look idle to the
        sweeper while turns are still arriving.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                insert into turns (conversation_id, role, text, latency_ms, cancelled)
                values ($1::uuid, $2, $3, $4, $5)
                returning id
                """,
                conversation_id, role, text, latency_ms, cancelled,
            )
            await conn.execute(
                "update conversations set last_activity_at = now() where id = $1::uuid",
                conversation_id,
            )
        if row is None:
            raise PersistenceError("turn insert returned nothing")
        return int(row["id"])

    async def transcript(self, conversation_id: str) -> list[StoredTurn]:
        """The conversation as the database has it, in order.

        §8: ordered by the monotonic id, never by ts. Same-millisecond writes,
        clock skew and reconnect races reorder a transcript keyed on ts, which
        silently corrupts the generated item.
        """
        rows = await self.pool.fetch(
            """
            select id, role, text, cancelled
              from turns
             where conversation_id = $1::uuid
             order by id
            """,
            conversation_id,
        )
        return [
            StoredTurn(
                seq=int(r["id"]),
                role=r["role"],
                text=r["text"],
                cancelled=r["cancelled"],
            )
            for r in rows
        ]

    # ----------------------------------------------------------------- users

    async def find_user(self, email: str) -> dict[str, Any] | None:
        """Look up a user by email. Returns None rather than raising."""
        row = await self.pool.fetchrow(
            """
            select id::text as id, email, password_hash, role, disabled
              from users
             where email = $1
            """,
            email,
        )
        return dict(row) if row else None

    async def record_login(self, user_id: str) -> None:
        await self.pool.execute(
            "update users set last_login_at = now() where id = $1::uuid", user_id
        )

    # ------------------------------------------------------------ sweeping

    async def stale_conversations(self, idle_minutes: int) -> list[str]:
        """Active conversations nobody has touched recently (§9).

        The WebSocket close handler cannot be relied on -- a hung socket never
        fires it -- so ticket generation also runs from this.
        """
        rows = await self.pool.fetch(
            """
            select id::text as id
              from conversations
             where status = 'active'
               and last_activity_at < now() - ($1 || ' minutes')::interval
            """,
            str(idle_minutes),
        )
        return [str(r["id"]) for r in rows]

    async def resumable(self, conversation_id: str) -> bool:
        row = await self.pool.fetchrow(
            "select 1 from conversations where id = $1::uuid and status = 'active'",
            conversation_id,
        )
        return row is not None
