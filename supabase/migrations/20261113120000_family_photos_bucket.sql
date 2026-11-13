-- Private storage for the dashboard's photo tile. Only the server's secret key can read or write it;
-- the dashboard serves the photos to signed-in family members itself.
insert into storage.buckets (id, name, public)
values ('family-photos', 'family-photos', false)
on conflict (id) do nothing;
