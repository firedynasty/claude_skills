-- Run this in your Supabase SQL editor (safe to rerun anytime)

create table if not exists flashcards (
  id        bigserial primary key,
  anki_id   bigint unique,          -- Anki note ID, used for upsert
  deck      text not null,
  model     text,                   -- Anki note type (Basic, Chinese Grammar Wiki, etc.)
  front     text,                   -- first meaningful field for study
  back      text,                   -- second meaningful field for study
  fields    jsonb,                  -- all fields as JSON for full access
  tags      text[],
  anki_mod  bigint,                 -- Anki mod timestamp (detect changes on re-sync)
  synced_at timestamptz default now()
);

-- Index for fast deck queries
create index if not exists idx_flashcards_deck on flashcards(deck);

-- Index for tag queries (GIN for array)
create index if not exists idx_flashcards_tags on flashcards using gin(tags);

-- Function to get distinct deck names (avoids 1000-row limit on large tables)
create or replace function get_decks()
returns table(deck text)
language sql stable
as $$
  select distinct deck from flashcards order by deck;
$$;
