-- Replace Supabase Auth with a first-party users table.
--
-- Operator decision: users are our own rows, not `auth.users`. That has a
-- consequence worth stating plainly, because it makes the whole system
-- simpler and safer:
--
--   The frontend no longer authenticates to Supabase at all. It talks only to
--   our backend, which connects to Postgres directly as the owner role. So the
--   anon/publishable key has no consumer, and §3.1's "no keys in the frontend"
--   rule goes back to being absolute -- the carve-out r2 had to add for the
--   Supabase anon key is gone.
--
-- Because nothing but the backend ever connects, RLS is left enabled with *no*
-- permissive policies. Deny-by-default: anything reaching Postgres over
-- PostgREST or a publishable key sees nothing at all, whatever it presents.

-- ------------------------------------------------------------------- users
create table if not exists users (
    id            uuid primary key default gen_random_uuid(),
    email         text not null unique,
    -- scrypt, via hashlib. Format: scrypt$n$r$p$<salt_b64>$<hash_b64>.
    -- No plaintext password ever reaches this table or any log.
    password_hash text not null,
    role          text not null default 'operator'
                  check (role in ('operator', 'owner')),
    created_at    timestamptz not null default now(),
    last_login_at timestamptz,
    disabled      boolean not null default false
);

create index if not exists users_active_email_idx
    on users (email) where not disabled;

-- --------------------------------------------------- drop the auth.users tie
-- The old operators table referenced auth.users, which no longer governs
-- anything here.
drop policy if exists operators_read on operators;
drop table if exists operators;

-- ------------------------------------------------------------ policy sweep
-- Every policy below granted access to a Supabase-authenticated caller. There
-- are none of those any more, and leaving them would be a standing invitation
-- for the publishable key to become load-bearing again by accident.
drop policy if exists conversations_read     on conversations;
drop policy if exists turns_read             on turns;
drop policy if exists tickets_read           on tickets;
drop policy if exists tickets_update_status  on tickets;
drop policy if exists agent_runs_read        on agent_runs;

drop function if exists is_operator();

alter table users enable row level security;

-- Deliberately no policies on any table. RLS enabled plus zero policies means
-- deny-all for every role except the table owner, which is the backend.
-- Verify with: set role anon; select * from tickets;  -- must return 0 rows.
