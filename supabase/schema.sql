-- شغّل هذا الملف كاملاً في Supabase: SQL Editor -> New query -> Run

create table if not exists tiktok_posts (
  id          text primary key,
  account     text not null,
  caption     text,
  posted_at   timestamptz,
  video_path  text,
  thumb_path  text,
  info        jsonb,
  saved_at    timestamptz default now()
);

create table if not exists tellonym_answers (
  id          text primary key,
  account     text not null,
  tell        text,
  answer      text,
  posted_at   timestamptz,
  raw         jsonb,
  saved_at    timestamptz default now()
);

create index if not exists tiktok_posts_account_idx on tiktok_posts (account, posted_at desc);
create index if not exists tellonym_answers_account_idx on tellonym_answers (account, posted_at desc);

-- الخصوصية: لا أحد يقرأ البيانات إلا مستخدم مسجّل الدخول
alter table tiktok_posts enable row level security;
alter table tellonym_answers enable row level security;

drop policy if exists "auth read tiktok" on tiktok_posts;
create policy "auth read tiktok" on tiktok_posts
  for select to authenticated using (true);

drop policy if exists "auth read tellonym" on tellonym_answers;
create policy "auth read tellonym" on tellonym_answers
  for select to authenticated using (true);

-- مجلد التخزين (خاص) للفيديوهات والصور المصغرة
insert into storage.buckets (id, name, public)
values ('archive', 'archive', false)
on conflict (id) do nothing;

drop policy if exists "auth read archive files" on storage.objects;
create policy "auth read archive files" on storage.objects
  for select to authenticated using (bucket_id = 'archive');

-- ملاحظة: السكربت يكتب باستخدام service_role الذي يتجاوز هذه القيود، فلا يحتاج سياسات كتابة.
