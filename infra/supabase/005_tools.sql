-- Calendar events, standing instructions, and a soft-delete marker.

-- ------------------------------------------------------------------ events
create table if not exists events (
    id          uuid primary key default gen_random_uuid(),
    title       text not null,
    starts_at   timestamptz not null,
    ends_at     timestamptz,
    notes       text,
    location    text,
    -- Where it came from, so an event the assistant created is visibly
    -- distinguishable from one the owner typed.
    source      text not null default 'owner'
                check (source in ('owner', 'assistant')),
    -- Set when an event was created from a message, so the calendar can link
    -- back to who asked for it.
    ticket_id   uuid references tickets (id) on delete set null,
    created_at  timestamptz not null default now(),
    cancelled   boolean not null default false
);

create index if not exists events_when_idx
    on events (starts_at) where not cancelled;

-- ---------------------------------------------------------------- settings
-- Owner-authored configuration. One row per key, edited from the app.
create table if not exists app_settings (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);

-- ------------------------------------------------------- soft delete
-- The assistant can archive an item but never destroy one. Archiving is
-- reversible and a mistaken archive costs nothing; a mistaken delete is
-- unrecoverable, and the text driving these decisions is written by whoever
-- called. Real erasure stays a human action via scripts/forget.py.
alter table tickets add column if not exists archived_at timestamptz;

create index if not exists tickets_live_idx
    on tickets (mode, status, created_at desc) where archived_at is null;

alter table events enable row level security;
alter table app_settings enable row level security;
-- No policies, as everywhere else: only the backend connects.
