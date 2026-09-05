-- Personal assistant schema. INSTRUCTIONS.md §8.
--
-- Idempotent: safe to re-run. Every difference from the r1 schema is marked
-- with the revision that changed it and why, because several of them exist to
-- close a specific hole rather than as tidying.

-- ---------------------------------------------------------------- operators
-- [r2] "Authenticated users only" is not an access policy. Supabase Auth allows
-- public sign-up by default, so scoping reads to `authenticated` means anyone
-- who registers can read every message left for the owner. Reads are scoped to
-- this allowlist instead, and public sign-up must also be disabled in the
-- Supabase Auth settings -- the table alone is not enough.
create table if not exists operators (
    user_id    uuid primary key references auth.users (id) on delete cascade,
    email      text,
    added_at   timestamptz not null default now()
);

create or replace function is_operator()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (select 1 from operators where user_id = auth.uid());
$$;

-- ------------------------------------------------------------ conversations
create table if not exists conversations (
    id               uuid primary key default gen_random_uuid(),
    started_at       timestamptz not null default now(),
    ended_at         timestamptz,
    channel          text not null check (channel in ('text', 'voice')),
    -- [r3] Set server-side from verified auth (§3.7), never from the client.
    mode             text not null check (mode in ('owner', 'visitor')),
    -- [r3] Visitor correlation only (campaign tag / referrer). Never PII,
    -- never person-entered. Null in owner mode.
    visitor_ref      text,
    status           text not null default 'active',
    -- [r2] The inactivity sweeper needs a column to sweep on. Deriving
    -- max(turns.ts) per conversation on every sweep does not scale.
    last_activity_at timestamptz not null default now(),
    -- [r3] English at launch; Arabic (Lebanon) likely later (§14).
    lang             text not null default 'en-GB',
    -- [r2] Conversation ran without an LLM (§6 degraded mode).
    degraded         boolean not null default false
);

create index if not exists conversations_active_idle_idx
    on conversations (status, last_activity_at)
    where status = 'active';

-- -------------------------------------------------------------------- turns
create table if not exists turns (
    id              bigserial primary key,
    conversation_id uuid not null references conversations (id) on delete cascade,
    role            text not null check (role in ('customer', 'agent')),
    text            text not null,
    ts              timestamptz not null default now(),
    latency_ms      int,
    -- [r2] Barge-in (§7.4) cuts an agent turn off mid-delivery. What the caller
    -- actually heard is what the item must reflect, so the partial text is kept
    -- and flagged rather than discarded.
    cancelled       boolean not null default false
);

-- [r2] Ordered by the monotonic id, not ts. Same-millisecond writes, clock skew
-- and reconnect races can reorder a transcript keyed on ts, which silently
-- corrupts the generated item.
create index if not exists turns_conversation_seq_idx
    on turns (conversation_id, id);

-- ------------------------------------------------------------------ tickets
-- Table name kept from r1 for continuity. It holds owner-side notes and tasks
-- as well as visitor messages; read "ticket" as "actionable item".
create table if not exists tickets (
    id              uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references conversations (id) on delete cascade,
    -- [r3] Broadened: owner-side kinds alongside visitor-side ones.
    type            text not null check (type in
                        ('note', 'task', 'reminder', 'message', 'request', 'other')),
    title           text not null,
    summary         text not null,
    intent          text,
    action_items    jsonb not null default '[]'::jsonb,
    urgency         text check (urgency in ('low', 'medium', 'high')),
    contact         jsonb not null default '{}'::jsonb,
    -- [r2] Appointment requests are free text. There is no calendar
    -- integration, and the assistant must never present these as confirmed.
    requested_slot  text,
    -- [r3] Denormalised from conversations so the dashboard can filter
    -- "mine" vs "inbound" without a join.
    mode            text not null check (mode in ('owner', 'visitor')),
    status          text not null default 'new'
                    check (status in ('new', 'triaged', 'agent_queued', 'done')),
    created_at      timestamptz not null default now(),
    -- [r2] §9 asserts one item per conversation but nothing enforced it.
    -- WS-close and the 5-minute inactivity timeout race. Let the database
    -- arbitrate rather than pre-checking in application code.
    unique (conversation_id)
);

create index if not exists tickets_triage_idx
    on tickets (mode, status, created_at desc);

-- --------------------------------------------------------------- agent_runs
-- Phase 7 only. Created now, deliberately unused.
create table if not exists agent_runs (
    id          uuid primary key default gen_random_uuid(),
    ticket_id   uuid not null references tickets (id) on delete cascade,
    prompt      text not null,
    status      text not null default 'queued',
    started_at  timestamptz,
    finished_at timestamptz,
    output      text,
    approved_by text
);

-- ---------------------------------------------------------------------- RLS
-- Enabled on every table. The backend uses the service role key (or a direct
-- Postgres connection as the owner) and bypasses RLS entirely; these policies
-- govern the dashboard's anon-key access only. The visitor widget never
-- touches Supabase directly.
alter table operators     enable row level security;
alter table conversations enable row level security;
alter table turns         enable row level security;
alter table tickets       enable row level security;
alter table agent_runs    enable row level security;

-- Read-only for operators. No insert/update/delete policies exist, so the
-- dashboard cannot write even if the anon key leaks -- which it will, because
-- it ships in the page (§3.1).
drop policy if exists operators_read on operators;
create policy operators_read on operators
    for select using (is_operator());

drop policy if exists conversations_read on conversations;
create policy conversations_read on conversations
    for select using (is_operator());

drop policy if exists turns_read on turns;
create policy turns_read on turns
    for select using (is_operator());

drop policy if exists tickets_read on tickets;
create policy tickets_read on tickets
    for select using (is_operator());

-- The operator triages from the dashboard, so status is the one writable field.
drop policy if exists tickets_update_status on tickets;
create policy tickets_update_status on tickets
    for update using (is_operator()) with check (is_operator());

drop policy if exists agent_runs_read on agent_runs;
create policy agent_runs_read on agent_runs
    for select using (is_operator());

-- ----------------------------------------------------------------- realtime
-- [r2] Realtime needs the table added to the publication explicitly, and full
-- replica identity so updates carry the old row. It respects RLS, so the
-- operator policy above governs the subscription too -- verify that, do not
-- assume it.
alter table tickets replica identity full;

do $$
begin
    if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and tablename = 'tickets'
    ) then
        alter publication supabase_realtime add table tickets;
    end if;
end $$;
