-- Storage for the family dashboard when it runs on Vercel: the Google refresh token and the
-- Google Keep cache, one JSON value per key. Only the server (secret / service_role key) may
-- touch it; row level security with no policies shuts out the anon and authenticated roles.

create table if not exists public.dashboard_kv (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

alter table public.dashboard_kv enable row level security;

revoke all on table public.dashboard_kv from anon, authenticated;
grant select, insert, update, delete on table public.dashboard_kv to service_role;
