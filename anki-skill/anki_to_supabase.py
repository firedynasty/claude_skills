#!/usr/bin/env python3
"""
Sync Anki decks to Supabase.

Usage:
    python anki_to_supabase.py --list                     # list all decks
    python anki_to_supabase.py --deck "module_5"          # sync one deck
    python anki_to_supabase.py --deck "Chinese Grammar Wiki"
    python anki_to_supabase.py --all                      # sync everything

Requires:
    pip install supabase

Env vars (or edit the constants below):
    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=your-anon-or-service-role-key
"""

import sys
import os
import json
import argparse
import urllib.request
import urllib.error
from typing import Any

# ── Config ─────────────────────────────────────────────────────────────────────
ANKI_URL     = "http://localhost:8765"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BATCH_SIZE   = 50   # notes per AnkiConnect batch

# How to map each model's fields to front/back for the study view.
# Key = modelName, value = (front_field, back_fields)
# back_fields is a list — values get joined with newline.
# If a model isn't listed here, the first two fields are used automatically.
FIELD_MAP = {
    "Basic": ("Front", ["Back"]),
    "Basic (and reversed card)": ("Front", ["Back"]),
    "Chinese Grammar Wiki": ("English", ["中文", "Pinyin"]),
}

# ── AnkiConnect ────────────────────────────────────────────────────────────────
def anki(action: str, **params) -> Any:
    payload = json.dumps({"action": action, "version": 6, "params": params})
    req = urllib.request.Request(
        ANKI_URL,
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.load(r)
    except urllib.error.URLError as e:
        print(f"❌  Cannot reach AnkiConnect — is Anki open?\n    {e}", file=sys.stderr)
        sys.exit(1)
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result["result"]


def get_deck_names() -> list[str]:
    names = anki("deckNames")
    return [n for n in names if n != "*"]


def get_notes_for_deck(deck: str) -> list[dict]:
    ids = anki("findNotes", query=f'deck:"{deck}"')
    if not ids:
        return []
    notes = []
    for i in range(0, len(ids), BATCH_SIZE):
        batch = ids[i : i + BATCH_SIZE]
        notes.extend(anki("notesInfo", notes=batch))
        print(f"    fetched {min(i + BATCH_SIZE, len(ids))}/{len(ids)} notes…", end="\r")
    print()
    return notes


# ── Field mapping ──────────────────────────────────────────────────────────────
def extract_front_back(note: dict) -> tuple[str, str]:
    model  = note.get("modelName", "")
    fields = note.get("fields", {})

    def get(field_name: str) -> str:
        f = fields.get(field_name)
        return f["value"].strip() if f else ""

    if model in FIELD_MAP:
        front_field, back_fields = FIELD_MAP[model]
        front = get(front_field)
        back  = "\n".join(get(f) for f in back_fields if get(f))
    else:
        # Auto: use first two fields in order
        ordered = sorted(fields.items(), key=lambda x: x[1]["order"])
        front = ordered[0][1]["value"].strip() if len(ordered) > 0 else ""
        back  = ordered[1][1]["value"].strip() if len(ordered) > 1 else ""

    return front, back


def build_record(note: dict, deck: str) -> dict:
    front, back = extract_front_back(note)
    # Store all field values as plain JSON (strip the "order" metadata)
    fields_json = {k: v["value"] for k, v in note.get("fields", {}).items()}
    return {
        "anki_id":  note["noteId"],
        "deck":     deck,
        "model":    note.get("modelName"),
        "front":    front,
        "back":     back,
        "fields":   fields_json,
        "tags":     note.get("tags", []),
        "anki_mod": note.get("mod"),
    }


# ── Supabase ───────────────────────────────────────────────────────────────────
def get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌  SUPABASE_URL and SUPABASE_KEY must be set.", file=sys.stderr)
        print("    export SUPABASE_URL=https://xxxx.supabase.co", file=sys.stderr)
        print("    export SUPABASE_KEY=your-key", file=sys.stderr)
        sys.exit(1)
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except ImportError:
        print("❌  supabase not installed. Run: pip install supabase", file=sys.stderr)
        sys.exit(1)


def upsert_records(sb, records: list[dict]) -> dict:
    """Upsert in batches of 500 (Supabase row limit per request)."""
    added = updated = 0
    for i in range(0, len(records), 500):
        batch = records[i : i + 500]
        sb.table("flashcards").upsert(batch, on_conflict="anki_id").execute()
        added += len(batch)
    return {"upserted": added}


# ── Sync ───────────────────────────────────────────────────────────────────────
def sync_deck(sb, deck: str):
    print(f"\n📦  Syncing deck: {deck}")
    notes = get_notes_for_deck(deck)
    if not notes:
        print("    No notes found.")
        return

    records = [build_record(n, deck) for n in notes]
    result  = upsert_records(sb, records)
    print(f"    ✅  {result['upserted']} notes upserted into Supabase.")


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sync Anki decks to Supabase.")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list",         action="store_true", help="List available Anki decks")
    group.add_argument("--all",          action="store_true", help="Sync all decks")
    group.add_argument("--deck",         metavar="DECK",      help='Sync one deck, e.g. --deck "module_5"')
    args = parser.parse_args()

    if args.list:
        decks = get_deck_names()
        print("Available decks:")
        for d in decks:
            print(f"  • {d}")
        return

    sb = get_supabase()

    if args.all:
        decks = [d for d in get_deck_names() if d not in ("Custom Study Session",)]
        for deck in decks:
            sync_deck(sb, deck)
    else:
        sync_deck(sb, args.deck)

    print("\nDone. 🎉")


if __name__ == "__main__":
    main()
