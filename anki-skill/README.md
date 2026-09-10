# Anki → Supabase → Vercel Flashcards Pipeline

## Overview

```
Anki (desktop)
    ↓  AnkiConnect (localhost:8765)
anki_to_supabase.py
    ↓  Supabase REST API (upsert by anki_id)
Supabase DB (flashcards table)
    ↓  Vercel serverless proxy (/api/supabase.js)
learn-google.html / learn-chinese-tts.html (mobile study)
```

---

## Step 1 — Supabase schema (one-time setup)

Run `supabase_schema.sql` in the Supabase SQL editor:

```sql
-- Creates: flashcards table, indexes, get_decks() RPC function
-- Safe to rerun anytime (uses IF NOT EXISTS / CREATE OR REPLACE)
```

Table columns: `id`, `anki_id` (unique), `deck`, `model`, `front`, `back`, `fields` (jsonb), `tags`, `anki_mod`, `synced_at`

---

## Step 2 — Sync Anki decks to Supabase

Anki must be open with the AnkiConnect add-on running.

```bash
export SUPABASE_URL=https://xxxx.supabase.co
export SUPABASE_KEY=your-service-role-or-anon-key

# List available decks
python anki_to_supabase.py --list

# Sync one deck
python anki_to_supabase.py --deck "Chinese Grammar Wiki"

# Sync all decks
python anki_to_supabase.py --all
```

### Field mapping

| Anki model | front field | back field(s) |
|---|---|---|
| Basic | Front | Back |
| Basic (and reversed card) | Front | Back |
| Chinese Grammar Wiki | English | 中文 + Pinyin (joined with `\n`) |
| _(other models)_ | first field | second field |

Edit `FIELD_MAP` in `anki_to_supabase.py` to add more models.

---

## Step 3 — Vercel proxy (credentials stay server-side)

File: `vercel_flashcards/api/supabase.js`

Set in Vercel dashboard → Settings → Environment Variables:
- `SUPABASE_URL`
- `SUPABASE_KEY`

Endpoints:
```
GET /api/supabase?action=decks          → list of deck names
GET /api/supabase?action=cards&deck=xxx → cards for a deck (front, back)
```

---

## Step 4 — Study on mobile / web

**`learn-google.html`** — general vocabulary (2-col)
- Click "Connect via Vercel proxy" (auto on Vercel deploy)
- Select deck → Load deck
- Cards show `front` as term, `back` as definition
- Click the card term to hear **Suggestopedia TTS** (1x → 1.4x → 1x → 1.4x)

**`learn-chinese-tts.html`** — Chinese / foreign language (2 or 3-col)
- Select column mode **before** loading:
  - **3 columns** — `front`=English, `back`="Chinese\nPinyin" (e.g. Chinese Grammar Wiki)
  - **2 columns** — `front`=term, `back`=definition (e.g. Basic vocab decks)
- Select deck → Load deck
- Full TTS with language-aware voice selection

---

## Notes

- Supabase upserts by `anki_id` — safe to re-sync anytime, no duplicates
- `get_decks()` RPC avoids the 1000-row default limit when listing decks
- Cards per deck are fetched up to 1000 (sufficient for most decks; increase `limit` in `api/supabase.js` if needed)
- Local dev fallback: enter Supabase URL + key directly in the UI (stored in `localStorage`)
