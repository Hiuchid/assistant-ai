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

import json
import logging
from dataclasses import dataclass
from datetime import datetime
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

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection[Any]) -> None:
        """Decode jsonb to Python objects rather than raw strings.

        Without this asyncpg hands back the JSON *text*, so action_items
        arrives as '["a","b"]' and anything iterating it gets characters --
        which is exactly how it failed: `.map is not a function` in the
        dashboard, and contact silently rendering as indexed letters.
        """
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            init=self._init_connection,
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

    # ------------------------------------------------------------- tickets

    async def conversation_meta(self, conversation_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            select mode, channel, degraded, status
              from conversations
             where id = $1::uuid
            """,
            conversation_id,
        )
        return dict(row) if row else None

    async def insert_ticket(
        self,
        conversation_id: str,
        *,
        mode: Mode,
        type: str,
        title: str,
        summary: str,
        intent: str | None,
        action_items: list[str],
        urgency: str,
        contact: dict[str, str],
        requested_slot: str | None,
        due_at: datetime | None = None,
    ) -> bool:
        """Insert the item. False means one already existed.

        §9 asks for idempotency. `on conflict do nothing` against the unique
        constraint is what provides it -- checking first would lose the race
        between session-end and the sweeper, which genuinely happens.
        """
        row = await self.pool.fetchrow(
            """
            insert into tickets (conversation_id, mode, type, title, summary,
                                 intent, action_items, urgency, contact,
                                 requested_slot, due_at)
            values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            on conflict (conversation_id) do nothing
            returning id
            """,
            conversation_id, mode, type, title, summary, intent,
            action_items, urgency, contact, requested_slot,
            due_at,
        )
        return row is not None

    async def list_tickets(
        self, *, mode: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            select t.id::text as id, t.type, t.title, t.summary, t.intent,
                   t.action_items, t.urgency, t.contact, t.requested_slot,
                   t.mode, t.status, t.created_at, t.due_at,
                   c.channel, c.degraded
              from tickets t
              join conversations c on c.id = t.conversation_id
             where t.archived_at is null
               and ($1::text is null or t.mode = $1)
               and ($2::text is null or t.status = $2)
             order by t.created_at desc
             limit $3
            """,
            mode, status, limit,
        )
        return [dict(r) for r in rows]

    async def archive_ticket(self, ticket_id: str) -> bool:
        """Hide an item. Reversible -- nothing is destroyed.

        This is as far as the assistant can go. Real erasure is a human action
        via scripts/forget.py, because the text driving these decisions is
        written by whoever called.
        """
        row = await self.pool.fetchrow(
            "update tickets set archived_at = now() where id = $1::uuid returning id",
            ticket_id,
        )
        return row is not None

    async def set_ticket_due(self, ticket_id: str, due_at: datetime | None) -> bool:
        row = await self.pool.fetchrow(
            """
            update tickets set due_at = $2, notified_at = null
             where id = $1::uuid returning id
            """,
            ticket_id, due_at,
        )
        return row is not None

    async def set_ticket_status(self, ticket_id: str, status: str) -> bool:
        row = await self.pool.fetchrow(
            """
            update tickets set status = $2 where id = $1::uuid returning id
            """,
            ticket_id, status,
        )
        return row is not None

    async def ticket_transcript(self, ticket_id: str) -> list[StoredTurn]:
        rows = await self.pool.fetch(
            """
            select tn.id, tn.role, tn.text, tn.cancelled
              from turns tn
              join tickets tk on tk.conversation_id = tn.conversation_id
             where tk.id = $1::uuid
             order by tn.id
            """,
            ticket_id,
        )
        return [
            StoredTurn(seq=int(r["id"]), role=r["role"], text=r["text"],
                       cancelled=r["cancelled"])
            for r in rows
        ]

    # ------------------------------------------------------- push subs

    async def save_subscription(
        self, *, user_id: str, endpoint: str, p256dh: str, auth: str,
        user_agent: str | None,
    ) -> None:
        """Upsert on endpoint: re-subscribing an install must not duplicate it.

        Duplicates would all fire at once, so the same reminder would arrive
        three times on one phone.
        """
        await self.pool.execute(
            """
            insert into push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
            values ($1::uuid, $2, $3, $4, $5)
            on conflict (endpoint) do update
                set user_id = excluded.user_id,
                    p256dh  = excluded.p256dh,
                    auth    = excluded.auth,
                    expired = false
            """,
            user_id, endpoint, p256dh, auth, user_agent,
        )

    async def active_subscriptions(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            select id::text as id, endpoint, p256dh, auth
              from push_subscriptions
             where not expired
            """
        )
        return [dict(r) for r in rows]

    async def mark_subscriptions_expired(self, ids: list[str]) -> None:
        if not ids:
            return
        await self.pool.execute(
            "update push_subscriptions set expired = true where id = any($1::uuid[])",
            ids,
        )

    async def delete_subscription(self, endpoint: str) -> bool:
        row = await self.pool.fetchrow(
            "delete from push_subscriptions where endpoint = $1 returning id", endpoint
        )
        return row is not None

    # ----------------------------------------------------------- reminders

    async def due_reminders(self) -> list[dict[str, Any]]:
        """Items whose time has come and which have not been announced yet.

        `status <> 'done'` so an item already dealt with never pings. Nothing
        is marked here -- the caller marks only what it actually delivered, so
        a failed notification retries on the next sweep instead of vanishing.
        """
        rows = await self.pool.fetch(
            """
            select id::text as id, title, summary, due_at, mode, type, contact
              from tickets
             where due_at is not null
               and archived_at is null
               and notified_at is null
               and status <> 'done'
               and due_at <= now()
             order by due_at
             limit 20
            """
        )
        return [dict(r) for r in rows]

    async def mark_notified(self, ticket_id: str) -> None:
        await self.pool.execute(
            "update tickets set notified_at = now() where id = $1::uuid", ticket_id
        )

    # ------------------------------------------------------------- events

    async def create_event(
        self, *, title: str, starts_at: datetime, ends_at: datetime | None,
        location: str | None, notes: str | None, ticket_id: str | None,
        source: str = "owner",
    ) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            insert into events (title, starts_at, ends_at, location, notes,
                                ticket_id, source)
            values ($1, $2, $3, $4, $5, $6::uuid, $7)
            returning id::text as id, title, starts_at, ends_at, location,
                      notes, source
            """,
            title, starts_at, ends_at, location, notes, ticket_id, source,
        )
        if row is None:
            raise PersistenceError("event insert returned nothing")
        return dict(row)

    async def list_events(self, *, days: int = 14) -> list[dict[str, Any]]:
        """Events in a window around now. Negative days looks backwards."""
        lo, hi = (f"{days} days", "0 days") if days < 0 else ("0 days", f"{days} days")
        rows = await self.pool.fetch(
            f"""
            select id::text as id, title, starts_at, ends_at, location, notes,
                   source, ticket_id::text as ticket_id
              from events
             where not cancelled
               and starts_at >= date_trunc('day', now()) - interval '{lo}'
               and starts_at <= now() + interval '{hi}'
             order by starts_at
             limit 100
            """
        )
        return [dict(r) for r in rows]

    async def cancel_event(self, event_id: str) -> bool:
        row = await self.pool.fetchrow(
            "update events set cancelled = true where id = $1::uuid returning id",
            event_id,
        )
        return row is not None

    # ----------------------------------------------------------- settings

    async def get_setting(self, key: str, default: str = "") -> str:
        value = await self.pool.fetchval(
            "select value from app_settings where key = $1", key
        )
        return str(value) if value is not None else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.pool.execute(
            """
            insert into app_settings (key, value) values ($1, $2)
            on conflict (key) do update
                set value = excluded.value, updated_at = now()
            """,
            key, value,
        )

    # ---------------------------------------------------------- retention

    async def purge_old_transcripts(self, older_than_days: int) -> int:
        """Delete turns from finished conversations past the retention window.

        Only the turns. The item built from them survives, so who called and
        what they wanted is kept while the verbatim record of how they said it
        is not -- which is what the notice on the visitor widget promises.

        Restricted to ended conversations so a long-running one is never
        truncated underneath itself.
        """
        result = await self.pool.execute(
            """
            delete from turns
             where conversation_id in (
                   select id from conversations
                    where ended_at is not null
                      and ended_at < now() - ($1 || ' days')::interval
             )
            """,
            str(older_than_days),
        )
        return int(result.split()[-1]) if result.startswith("DELETE") else 0

    async def purge_old_conversations(self, older_than_days: int) -> int:
        """Delete whole conversations, cascading to turns and items."""
        result = await self.pool.execute(
            """
            delete from conversations
             where ended_at is not null
               and ended_at < now() - ($1 || ' days')::interval
            """,
            str(older_than_days),
        )
        return int(result.split()[-1]) if result.startswith("DELETE") else 0

    async def find_conversations_for(self, needle: str) -> list[dict[str, Any]]:
        """Everything matching a person, for an erasure request.

        Searches the item's contact blob and title, and the raw transcript.
        Deliberately broad: missing a row on a deletion request is worse than
        showing one row too many to a human who is about to confirm.
        """
        rows = await self.pool.fetch(
            """
            select distinct c.id::text as id, c.mode, c.channel, c.started_at,
                   t.title, t.contact
              from conversations c
              left join tickets t on t.conversation_id = c.id
              left join turns tn on tn.conversation_id = c.id
             where c.id::text = $1
                or t.contact::text ilike '%' || $1 || '%'
                or t.title ilike '%' || $1 || '%'
                or tn.text ilike '%' || $1 || '%'
             order by c.started_at desc
            """,
            needle,
        )
        return [dict(r) for r in rows]

    async def delete_conversations(self, ids: list[str]) -> int:
        result = await self.pool.execute(
            "delete from conversations where id = any($1::uuid[])", ids
        )
        return int(result.split()[-1]) if result.startswith("DELETE") else 0

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
