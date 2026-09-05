-- Web Push subscriptions.
--
-- Replaces the ntfy topic, which was a shared secret in all but name: anyone
-- who learned it could read the reminders and forge new ones. A Web Push
-- subscription is bound to one browser install and one VAPID key pair, so
-- there is nothing to leak by knowing a name.

create table if not exists push_subscriptions (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references users (id) on delete cascade,
    -- The endpoint IS the identity: it is a unique URL at the browser vendor's
    -- push service. Unique so re-subscribing the same install updates rather
    -- than accumulating duplicates that all fire at once.
    endpoint     text not null unique,
    p256dh       text not null,
    auth         text not null,
    user_agent   text,
    created_at   timestamptz not null default now(),
    last_used_at timestamptz,
    -- Set when the push service reports the subscription is gone (404/410).
    -- Kept rather than deleted so a re-subscribe is visibly a re-subscribe.
    expired      boolean not null default false
);

create index if not exists push_active_idx
    on push_subscriptions (user_id) where not expired;

alter table push_subscriptions enable row level security;
-- No policies, as with every other table: only the backend connects.
