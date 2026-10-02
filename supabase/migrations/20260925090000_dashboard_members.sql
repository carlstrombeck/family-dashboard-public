-- Who may sign in to the family dashboard with Google. role 'admin' can also open /setup.
-- Like dashboard_kv, only the server's secret / service_role key can read it.
--
-- Add someone:  insert into public.dashboard_members (email, role) values ('name@gmail.com', 'admin');
-- Remove:       delete from public.dashboard_members where email = 'name@gmail.com';

create table if not exists public.dashboard_members (
  email text primary key check (email = lower(email)),
  role text not null default 'member' check (role in ('admin', 'member')),
  added_at timestamptz not null default now()
);

alter table public.dashboard_members enable row level security;

revoke all on table public.dashboard_members from anon, authenticated;
grant select, insert, update, delete on table public.dashboard_members to service_role;
