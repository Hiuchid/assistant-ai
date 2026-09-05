-- Tasks and projects.
--
-- ## Why tasks are a table and reminders were not
--
-- 003_reminders.sql argued against a reminders table: a reminder is a ticket
-- that happens to have a time on it, and splitting it would mean two places to
-- look for "things I need to deal with". That argument still holds and this
-- does not break it.
--
-- A ticket is the record of a *conversation*. It has a transcript behind it, a
-- channel, a mode and a caller -- tickets.conversation_id is not null and
-- unique, so every ticket needs a conversation that actually happened. A task
-- has none of that: it is something written down directly, by the owner or by
-- the assistant on their behalf. Forcing one into the other would mean minting
-- a fake conversation per task.
--
-- So there are two kinds of row, and one sweeper fires the due ones from both.
-- The app shows them on a single timeline, which is what the original argument
-- was actually protecting.

-- ---------------------------------------------------------------- projects
-- A named piece of work that outlives any single message. The case this is
-- for: someone leaves a voice message asking for an app, and that becomes a
-- project with the original message attached and tasks underneath it.
create table if not exists projects (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    emoji       text not null default '📁',
    colour      text not null default 'violet'
                check (colour in ('violet', 'blue', 'green', 'amber', 'red', 'grey')),
    status      text not null default 'active'
                check (status in ('active', 'paused', 'done')),
    notes       text,
    due_at      timestamptz,
    source      text not null default 'owner'
                check (source in ('owner', 'assistant')),
    created_at  timestamptz not null default now(),
    -- Archive, never delete: same reasoning as tickets. The assistant acts on
    -- text written by whoever called, so the worst it can do is hide a row.
    archived_at timestamptz
);

create index if not exists projects_live_idx
    on projects (status, created_at desc) where archived_at is null;

-- ------------------------------------------------------------------- tasks
create table if not exists tasks (
    id          uuid primary key default gen_random_uuid(),
    title       text not null,
    notes       text,
    priority    text not null default 'med'
                check (priority in ('low', 'med', 'high')),
    -- Always a real instant so the sweeper can compare it to now(). all_day
    -- only decides whether the app prints a time next to it; an all-day task
    -- is stored at 09:00 local, which is what the summariser already assumes
    -- when someone names a day but not an hour.
    due_at      timestamptz,
    all_day     boolean not null default true,
    -- 0 = one-off. Otherwise the task rolls forward this many days each time
    -- it is completed, rather than spawning a new row per occurrence.
    repeat_days integer not null default 0 check (repeat_days between 0 and 365),
    done_at     timestamptz,
    completed_count integer not null default 0,
    project_id  uuid references projects (id) on delete set null,
    -- Set when a task came out of a message, so it can link back to who asked.
    ticket_id   uuid references tickets (id) on delete set null,
    source      text not null default 'owner'
                check (source in ('owner', 'assistant')),
    notified_at timestamptz,
    created_at  timestamptz not null default now(),
    archived_at timestamptz
);

create index if not exists tasks_open_idx
    on tasks (due_at) where archived_at is null and done_at is null;

create index if not exists tasks_project_idx
    on tasks (project_id) where archived_at is null;

-- The sweeper's query, mirroring tickets_due_idx: due, unfired, still open.
create index if not exists tasks_due_idx
    on tasks (due_at)
    where due_at is not null and notified_at is null and done_at is null
      and archived_at is null;

-- ------------------------------------------------------ tickets -> projects
-- A message can be filed under a project. Nullable and on delete set null:
-- losing a project must never take the record of a conversation with it.
alter table tickets add column if not exists project_id uuid
    references projects (id) on delete set null;

create index if not exists tickets_project_idx
    on tickets (project_id) where archived_at is null;

-- ------------------------------------------------------- events -> projects
alter table events add column if not exists project_id uuid
    references projects (id) on delete set null;

alter table projects enable row level security;
alter table tasks enable row level security;
-- No policies, as everywhere else: only the backend connects.
