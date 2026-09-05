-- Reminders.
--
-- Deliberately NOT a new table. A reminder is a ticket that happens to have a
-- time on it, and giving it its own table would mean two places to look for
-- "things I need to deal with" and two things to keep in sync.
--
-- This is the useful half of what Phase 7 was going to do, without any of the
-- risk: the action is fixed (notify at a time), so the item stays data and
-- never becomes an instruction. Nothing here executes anything.

alter table tickets add column if not exists due_at      timestamptz;
alter table tickets add column if not exists notified_at timestamptz;

-- The sweeper's query: due, not yet fired, not already dealt with. Partial so
-- it stays small no matter how many items accumulate.
create index if not exists tickets_due_idx
    on tickets (due_at)
    where due_at is not null and notified_at is null and status <> 'done';
