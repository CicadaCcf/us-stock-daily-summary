-- Collaborative overlay for Top Movers industry / reason.
--
-- Baseline screener data lives in src/data/{date}/screener.json (built by
-- the Python pipeline and committed). Rows here overlay those baseline
-- fields at render time, so teammates can annotate without a redeploy.
--
-- Apply via Supabase Dashboard → SQL Editor, or `supabase db push` if the
-- CLI is configured.

create table if not exists public.screener_edits (
  date        text not null,
  tk          text not null,
  industry    text,
  reason      text,
  updated_at  timestamptz not null default now(),
  updated_by  text,
  primary key (date, tk)
);

-- No realtime needed for v1; enable later if we want multi-user live sync.

-- Row-level security: permissive for v1 — anyone with the anon key can
-- read and write. Internal dashboard behind an obscure URL. Tighten later
-- (e.g. require a Supabase JWT with a specific email domain).
alter table public.screener_edits enable row level security;

drop policy if exists "anon read"   on public.screener_edits;
drop policy if exists "anon write"  on public.screener_edits;
drop policy if exists "anon update" on public.screener_edits;

create policy "anon read"
  on public.screener_edits
  for select
  using (true);

create policy "anon write"
  on public.screener_edits
  for insert
  with check (true);

create policy "anon update"
  on public.screener_edits
  for update
  using (true)
  with check (true);
