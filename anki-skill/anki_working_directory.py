#!/usr/bin/env python3
"""
Mirror a folder of CSVs into Anki decks. The path inside the folder is the deck name,
with subfolders becoming subdecks:

    anki_working_directory/switchboard/safety.csv  ->  deck "switchboard::safety"
    anki_working_directory/bus_words.csv           ->  deck "bus_words"

Each run makes every deck match its CSV:
    - new rows                 -> added
    - changed back             -> updated in place (review history kept)
    - row moved to another CSV -> card moved to that deck (review history kept)
    - rows you deleted         -> deleted from Anki (asks first unless --yes)

Cards are matched by their Front text, so editing a Front counts as a delete
plus an add and resets that card's progress. Decks without a CSV in the folder
are never touched, and removing a CSV from the folder leaves its deck alone.
Files ending in _glossed.csv are skipped: they're source material too big to
study whole. Copy the rows you want into a new CSV and that one gets synced.

Usage:
    python anki_working_directory.py              # sync every CSV
    python anki_working_directory.py --dry-run    # show what would change
    python anki_working_directory.py --pull "Deck Name"   # deck -> CSV, to start editing an existing deck

CSV format: column 1 is the Front, every other column joins into the Back
(one line each, blanks skipped), so Chinese,Pinyin,English gives a Back of
pinyin + English. A header row like "Chinese,Pinyin,English" is skipped.
Tags aren't used: cards in these decks are kept tag-free.
Anki's import header lines are optional:
    #separator:comma
    #html:false

Requires Anki desktop running with AnkiConnect (add-on 2055492159).
Changes reach your phone the next time Anki syncs with AnkiWeb.
"""

import argparse
import csv
import html
import io
import os
import sys
from pathlib import Path

from anki_connect import _invoke

# Set ANKI_WORK_DIR to use another folder.
WORK_DIR = Path(os.environ.get("ANKI_WORK_DIR", Path(__file__).parent / "anki_working_directory")).expanduser()
MODEL = "Basic"
SKIP_SUFFIX = "_glossed"  # source files, never synced
SEPARATORS = {"comma": ",", "tab": "\t", "semicolon": ";", "pipe": "|", "space": " "}
# Same list anki-manager.html uses to spot a header row.
HEADER_WORDS = {"chinese", "pinyin", "definition", "term", "front", "back", "english", "word",
                "vocab", "vocabulary", "language", "pronunciation", "中文", "translation"}


def deck_name(path: Path) -> str:
    return "::".join(path.relative_to(WORK_DIR).with_suffix("").parts)


def deck_path(deck: str) -> Path:
    return WORK_DIR.joinpath(*deck.split("::")).with_suffix(".csv")


def short(front: str) -> str:
    return html.unescape(front)[:70]


# ── CSV side ──────────────────────────────────────────────────────────────────

def read_csv(path: Path) -> dict[str, dict]:
    """Return {front_html: back_html} for one CSV."""
    text = path.read_text(encoding="utf-8-sig")
    sep, is_html = ",", False
    body = []
    for line in text.splitlines(keepends=True):
        if line.startswith("#") and ":" in line and not body:
            key, _, val = line[1:].partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "separator":
                sep = SEPARATORS.get(val.lower(), val)
            elif key == "html":
                is_html = val.lower() == "true"
        else:
            body.append(line)

    render = (lambda s: s) if is_html else (lambda s: html.escape(s, quote=False).replace("\n", "<br>"))
    cards: dict[str, str] = {}
    for n, row in enumerate(csv.reader(io.StringIO("".join(body)), delimiter=sep), 1):
        if not row or not row[0].strip():
            continue
        if n == 1 and any(c.strip().lower() in HEADER_WORDS for c in row):
            continue
        if len(row) < 2:
            print(f"  ! {path.name} row {n}: no Back column, skipped")
            continue
        front = render(row[0].strip())
        if front in cards:
            print(f"  ! {path.name} row {n}: duplicate Front, later row wins: {row[0][:60]}")
        cards[front] = "<br>".join(render(c.strip()) for c in row[1:] if c.strip())
    return cards


# ── Anki side ─────────────────────────────────────────────────────────────────

def read_deck(deck: str) -> tuple[dict[str, dict], int]:
    """Return ({front: {"id", "cards", "back", "tags"}}, skipped_count) for Basic notes in deck (not subdecks)."""
    q = f'"deck:{deck}" -"deck:{deck}::*"'
    ids = _invoke("findNotes", query=q)
    notes, skipped = {}, 0
    for info in _invoke("notesInfo", notes=ids) if ids else []:
        if info["modelName"] != MODEL:
            skipped += 1
            continue
        f = info["fields"]
        notes[f["Front"]["value"]] = {"id": info["noteId"], "cards": info["cards"],
                                      "back": f["Back"]["value"], "tags": set(info["tags"])}
    return notes, skipped


# ── Sync ──────────────────────────────────────────────────────────────────────

def sync(paths: list[Path], dry_run: bool, assume_yes: bool) -> None:
    wanted = {deck_name(p): read_csv(p) for p in paths}
    current, skipped = {}, {}
    for deck in wanted:
        current[deck], skipped[deck] = read_deck(deck)
    before = sum(len(notes) for notes in current.values())

    # A row cut from one CSV and pasted into another: move the card rather than delete + add.
    moves = []  # (front, from_deck, to_deck)
    for deck, cards in wanted.items():
        for front in cards:
            if front in current[deck]:
                continue
            for other, notes in current.items():
                if other != deck and front in notes and front not in wanted[other]:
                    moves.append((front, other, deck))
                    current[deck][front] = notes.pop(front)
                    break

    for deck, cards in wanted.items():
        have = current[deck]
        to_add = [f for f in cards if f not in have]
        to_delete = [f for f in have if f not in cards]
        to_update = [f for f in cards if f in have
                     and (cards[f] != have[f]["back"] or have[f]["tags"])]
        moved_in = [(f, src) for f, src, dst in moves if dst == deck]

        print(f"{deck}: {len(cards)} in CSV  →  +{len(to_add)} add, ~{len(to_update)} update, "
              f">{len(moved_in)} moved in, -{len(to_delete)} delete")
        if skipped[deck]:
            print(f"  ({skipped[deck]} non-{MODEL} notes in this deck are left alone)")
        if dry_run:
            for f in to_add:
                print(f"  + {short(f)}")
            for f, src in moved_in:
                print(f"  > {short(f)}  (from {src})")
            for f in to_update:
                print(f"  ~ {short(f)}")
            for f in to_delete:
                print(f"  - {short(f)}")
            continue

        if to_add or moved_in:
            _invoke("createDeck", deck=deck)
        if moved_in:
            _invoke("changeDeck", cards=[c for f, _ in moved_in for c in have[f]["cards"]], deck=deck)
        if to_add:
            notes = [{"deckName": deck, "modelName": MODEL,
                      "fields": {"Front": f, "Back": cards[f]},
                      "options": {"allowDuplicate": True}}
                     for f in to_add]
            failed = sum(1 for r in _invoke("addNotes", notes=notes) if r is None)
            if failed:
                print(f"  ! {failed} card(s) could not be added")

        for f in to_update:
            nid, got = have[f]["id"], have[f]
            if cards[f] != got["back"]:
                _invoke("updateNoteFields", note={"id": nid, "fields": {"Back": cards[f]}})
            if got["tags"]:
                _invoke("removeTags", notes=[nid], tags=" ".join(got["tags"]))

        if to_delete:
            for f in to_delete:
                print(f"  - {short(f)}")
            if assume_yes or input(f"  Delete these {len(to_delete)} card(s) and their review history? [y/N] ").strip().lower() == "y":
                _invoke("deleteNotes", notes=[have[f]["id"] for f in to_delete])
            else:
                print("  kept them; delete them in Anki or add the rows back to stop this prompt")

    # Re-read Anki and compare against the CSVs, so a card that silently didn't land shows up here.
    in_csv = sum(len(cards) for cards in wanted.values())
    if dry_run:
        print(f"\nTotal: {in_csv} cards in CSVs, {before} in Anki now (dry run, nothing changed)")
        return
    after = 0
    for deck, cards in wanted.items():
        n = len(read_deck(deck)[0])
        after += n
        if n != len(cards):
            print(f"  ! {deck}: {len(cards)} in CSV but {n} in Anki")
    print(f"\nTotal: {in_csv} cards in CSVs, Anki went {before} → {after}"
          + ("  ✓ match" if after == in_csv else "  ✗ MISMATCH, see ! lines above"))


# ── Pull: deck -> CSV ─────────────────────────────────────────────────────────

def pull(deck: str) -> None:
    out = deck_path(deck)
    if out.exists():
        sys.exit(f"{out} already exists; delete or rename it first")
    notes, skipped = read_deck(deck)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        fh.write("#separator:comma\n#html:true\n")
        w = csv.writer(fh)
        for front, n in notes.items():
            w.writerow([front, n["back"]])
    print(f"Wrote {len(notes)} cards to {out}" + (f" ({skipped} non-{MODEL} notes skipped)" if skipped else ""))


def main():
    p = argparse.ArgumentParser(description=f"Mirror CSVs in {WORK_DIR} into Anki decks.")
    p.add_argument("--dry-run", action="store_true", help="show changes without making them")
    p.add_argument("--yes", action="store_true", help="delete removed rows without asking")
    p.add_argument("--pull", metavar="DECK", help="write an existing deck out as a CSV in the folder")
    args = p.parse_args()

    WORK_DIR.mkdir(exist_ok=True)
    try:
        if args.pull:
            return pull(args.pull)
        all_csvs = sorted(WORK_DIR.rglob("*.csv"))
        paths = [p for p in all_csvs if not p.stem.endswith(SKIP_SUFFIX)]
        print(f"Reading {WORK_DIR}: {len(paths)} CSV(s), {len(all_csvs) - len(paths)} {SKIP_SUFFIX} skipped\n")
        if not paths:
            print(f"No CSVs in {WORK_DIR}. Drop some in (path = deck name) and rerun.")
            return
        sync(paths, args.dry_run, args.yes)
    except ConnectionError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
