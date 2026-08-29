#!/usr/bin/env python3
"""
Write flashcards to Anki via the AnkiConnect add-on.

Usage:
    python anki_connect.py cards.json [--deck "My Deck"] [--model Basic]

Input JSON format:
    [
      {
        "front": "Question text",
        "back":  "Answer text (HTML ok)",
        "tags":  ["optional", "tags"]   <- optional field
      },
      ...
    ]

Requires:
    - Anki desktop running
    - AnkiConnect add-on (Tools > Add-ons > 2055492159)

AnkiConnect runs at http://localhost:8765 by default.
"""

import sys
import json
import argparse
import urllib.request
import urllib.error
from typing import Any

ANKI_CONNECT_URL = "http://localhost:8765"


# ── AnkiConnect low-level ────────────────────────────────────────────────────

def _invoke(action: str, **params) -> Any:
    payload = json.dumps({"action": action, "version": 6, "params": params})
    req = urllib.request.Request(
        ANKI_CONNECT_URL,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.load(resp)
    except urllib.error.URLError as e:
        raise ConnectionError(
            "Could not reach AnkiConnect.\n"
            "Make sure Anki is open and the AnkiConnect add-on is installed.\n"
            f"  Add-on code: 2055492159\n"
            f"  Error: {e}"
        ) from e

    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result["result"]


# ── Public helpers ────────────────────────────────────────────────────────────

def ping() -> bool:
    """Return True if AnkiConnect is reachable."""
    try:
        _invoke("version")
        return True
    except ConnectionError:
        return False


def ensure_deck(deck_name: str) -> None:
    _invoke("createDeck", deck=deck_name)


def deck_names() -> list[str]:
    return _invoke("deckNames")


def add_cards(
    cards: list[dict],
    deck_name: str = "Claude::Imported",
    model_name: str = "Basic",
) -> dict:
    """
    Add cards to Anki. Returns a summary dict:
        {"added": int, "duplicates": int, "failed": list[dict]}
    """
    ensure_deck(deck_name)

    notes = []
    for card in cards:
        notes.append(
            {
                "deckName": deck_name,
                "modelName": model_name,
                "fields": {
                    "Front": card["front"],
                    "Back": card["back"],
                },
                "options": {
                    "allowDuplicate": False,
                    "duplicateScope": "deck",
                },
                "tags": card.get("tags", []),
            }
        )

    results = _invoke("addNotes", notes=notes)

    added, duplicates, failed = 0, 0, []
    for i, note_id in enumerate(results):
        if note_id is None:
            # AnkiConnect returns null for duplicates / model mismatches
            duplicates += 1
            failed.append({"index": i, "front": cards[i]["front"][:80]})
        else:
            added += 1

    return {"added": added, "duplicates": duplicates, "failed": failed}


def export_tsv(cards: list[dict], path: str) -> None:
    """
    Fallback: write cards as a tab-separated file importable via
    Anki > File > Import (select 'Tab-separated' and map columns).
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write("#separator:tab\n#html:true\n#notetype:Basic\n")
        for card in cards:
            tags = " ".join(card.get("tags", []))
            front = card["front"].replace("\t", " ")
            back = card["back"].replace("\t", " ")
            f.write(f"{front}\t{back}\t{tags}\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Send flashcards to Anki via AnkiConnect."
    )
    parser.add_argument("cards_file", help="Path to JSON file with card array")
    parser.add_argument(
        "--deck", default="Claude::Imported", help="Destination deck name"
    )
    parser.add_argument(
        "--model", default="Basic", help="Anki note model (default: Basic)"
    )
    parser.add_argument(
        "--tsv",
        metavar="OUTPUT.txt",
        help="Skip AnkiConnect and write a .txt import file instead",
    )
    args = parser.parse_args()

    with open(args.cards_file, encoding="utf-8") as f:
        cards = json.load(f)

    if not isinstance(cards, list) or not cards:
        print("ERROR: cards file must be a non-empty JSON array.", file=sys.stderr)
        sys.exit(1)

    # ── TSV fallback mode ────────────────────────────────────────────────────
    if args.tsv:
        export_tsv(cards, args.tsv)
        print(f"Wrote {len(cards)} cards to {args.tsv}")
        print("Import in Anki: File > Import, choose Tab-separated, map columns.")
        return

    # ── AnkiConnect mode ─────────────────────────────────────────────────────
    try:
        summary = add_cards(cards, deck_name=args.deck, model_name=args.model)
    except ConnectionError as e:
        print(f"\n{e}\n", file=sys.stderr)
        print(
            "Tip: run with --tsv output.txt to save a file you can import manually.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Done — added {summary['added']}/{len(cards)} cards to '{args.deck}'"
        + (f", {summary['duplicates']} duplicate(s) skipped" if summary["duplicates"] else "")
    )

    if summary["failed"]:
        print("\nFailed cards (duplicates or model mismatch):")
        for f in summary["failed"]:
            print(f"  [{f['index'] + 1}] {f['front']}")


if __name__ == "__main__":
    main()
