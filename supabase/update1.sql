-- تحديث 1: السماح بالحذف من اللوحة + تذكّر المحذوف حتى لا يُحفظ مرة أخرى
drop policy if exists "auth delete tiktok" on tiktok_posts;
create policy "auth delete tiktok" on tiktok_posts for delete to authenticated using (true);

drop policy if exists "auth delete tellonym" on tellonym_answers;
create policy "auth delete tellonym" on tellonym_answers for delete to authenticated using (true);

drop policy if exists "auth delete archive files" on storage.objects;
create policy "auth delete archive files" on storage.objects
  for delete to authenticated using (bucket_id = 'archive');

create table if not exists deleted_posts (
  id text not null,
  account text not null,
  deleted_at timestamptz default now(),
  primary key (id, account)
);
alter table deleted_posts enable row level security;
drop policy if exists "auth all deleted" on deleted_posts;
create policy "auth all deleted" on deleted_posts for all to authenticated using (true) with check (true);
