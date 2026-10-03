#!/usr/bin/env python3
"""
Mirror a folder of CSVs into Anki decks. The path inside the folder is the deck name,
with subfolders becoming subdecks:

    anki_working_directory/switchboard/safety.csv  ->  deck "switchboard::safety"
    anki_working_directory/bus_words.csv           ->  deck "bus_words"

Works like `rclone copy`: CSV -> Anki, never deleting. Anki stays the full record.
    - new rows                 -> added
    - changed back             -> updated in place (review history kept)
    - row moved to another CSV -> card moved to that deck (review history kept)
    - rows not in the CSV      -> kept in Anki, just counted in the summary

Cards are matched by their Front text, so editing a Front adds a new card and
leaves the old one in Anki. Decks without a CSV in the folder are never touched.
Files ending in _glossed.csv are skipped: they're source material too big to
study whole. Copy the rows you want into a new CSV and that one gets synced.

Usage:
    python anki_working_directory.py              # sync every CSV
    python anki_working_directory.py --dry-run    # show what would change
    python anki_working_directory.py --pull "Deck Name"   # deck -> CSV, to start editing an existing deck
    python anki_working_directory.py --sheet "switchboard"  # Google Sheet -> folder of CSVs, then sync
    python anki_working_directory.py --download   # decks uploaded to Supabase -> folder of CSVs, then sync

--sheet takes a spreadsheet's name, URL or ID (repeatable). The spreadsheet
becomes a folder and each tab becomes a CSV in it, so spreadsheet "switchboard"
with tabs paint and safety gives decks switchboard::paint and switchboard::safety.
The tabs overwrite those CSVs each time; nothing else in the folder changes.
Uses the Google sign-in from gsheet_bridge.py (client_secret.json + gsheet_token.json).

--download fetches the CSVs uploaded from anki_manager.html (Supabase table
anki_decks). Each deck becomes a CSV the same way: "switchboard::safety"
-> switchboard/safety.csv. Only decks uploaded since the last download are
written, so a CSV you edited here is only replaced when that deck is uploaded
again. Needs SUPABASE_URL and SUPABASE_KEY (same as anki_to_supabase.py).
Like --sheet, it writes the CSVs even with --dry-run; only Anki is left alone.

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
import json
import os
import re
import sys
import urllib.error
import urllib.request
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

def sync(paths: list[Path], dry_run: bool) -> None:
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
        extra = sum(1 for f in have if f not in cards)
        to_update = [f for f in cards if f in have
                     and (cards[f] != have[f]["back"] or have[f]["tags"])]
        moved_in = [(f, src) for f, src, dst in moves if dst == deck]

        print(f"{deck}: {len(cards)} in CSV  →  +{len(to_add)} add, ~{len(to_update)} update, "
              f">{len(moved_in)} moved in" + (f", {extra} only in Anki (kept)" if extra else ""))
        if skipped[deck]:
            print(f"  ({skipped[deck]} non-{MODEL} notes in this deck are left alone)")
        if dry_run:
            for f in to_add:
                print(f"  + {short(f)}")
            for f, src in moved_in:
                print(f"  > {short(f)}  (from {src})")
            for f in to_update:
                print(f"  ~ {short(f)}")
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

    # Re-read Anki and compare against the CSVs, so a card that silently didn't land shows up here.
    in_csv = sum(len(cards) for cards in wanted.values())
    if dry_run:
        print(f"\nTotal: {in_csv} cards in CSVs, {before} in Anki now (dry run, nothing changed)")
        return
    after, missing = 0, 0
    for deck, cards in wanted.items():
        notes = read_deck(deck)[0]
        after += len(notes)
        lost = [f for f in cards if f not in notes]
        missing += len(lost)
        if lost:
            print(f"  ! {deck}: {len(lost)} CSV row(s) not in Anki, e.g. {short(lost[0])}")
    print(f"\nTotal: {in_csv} cards in CSVs, Anki went {before} → {after}"
          + ("  ✓ every CSV row is in Anki" if not missing else f"  ✗ {missing} missing, see ! lines above"))


# ── Google Sheets: spreadsheet -> folder, tab -> CSV ──────────────────────────

def safe_name(name: str) -> str:
    return re.sub(r'[/\\:]', "-", name).strip()


def fetch_sheet(ref: str) -> None:
    """Export every tab of a Google Sheet to WORK_DIR/<spreadsheet>/<tab>.csv."""
    from googleapiclient.discovery import build
    from gsheet_bridge import fetch_workbook, get_credentials, sheet_to_csv, sign_in

    creds = get_credentials() or sign_in()
    drive = build("drive", "v3", credentials=creds)
    m = re.search(r"/d/([\w-]+)", ref)
    if m or re.fullmatch(r"[\w-]{25,}", ref):
        file_id = m.group(1) if m else ref
        name = drive.files().get(fileId=file_id, fields="name").execute()["name"]
    else:
        q = ("mimeType='application/vnd.google-apps.spreadsheet' and trashed=false "
             f"and name='{ref.replace(chr(39), chr(92) + chr(39))}'")
        found = drive.files().list(q=q, fields="files(id, name)").execute().get("files", [])
        if len(found) != 1:
            sys.exit(f'{len(found)} Google Sheets named "{ref}"; pass its URL instead')
        file_id, name = found[0]["id"], found[0]["name"]

    wb = fetch_workbook(creds, file_id)
    folder = WORK_DIR / safe_name(name)
    folder.mkdir(parents=True, exist_ok=True)
    for tab in wb.sheetnames:
        (folder / f"{safe_name(tab)}.csv").write_text(sheet_to_csv(wb, tab), encoding="utf-8")
    print(f'Google Sheet "{name}" -> {folder.name}/ ({len(wb.sheetnames)} tabs: {", ".join(wb.sheetnames)})')


# ── Supabase: uploaded deck -> CSV ─────────────────────────────────────────────────

DOWNLOAD_STATE = WORK_DIR / ".supabase_downloads.json"  # {deck: updated_at last written}


def download_decks() -> None:
    """Write each deck uploaded to Supabase since the last download to WORK_DIR/<deck path>.csv."""
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        sys.exit("--download needs SUPABASE_URL and SUPABASE_KEY (same as anki_to_supabase.py)")
    req = urllib.request.Request(f"{url}/rest/v1/anki_decks?select=deck,csv,updated_at&order=deck",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req) as r:
            rows = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Supabase download failed: {e.code} {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"Can't reach Supabase at {url}: {e.reason}")

    state = json.loads(DOWNLOAD_STATE.read_text()) if DOWNLOAD_STATE.exists() else {}
    written = 0
    for row in rows:
        parts = [safe_name(part) for part in row["deck"].split("::")]
        if any(part in ("", ".", "..") for part in parts):
            print(f'  ! skipped deck "{row["deck"]}": not a usable deck name')
            continue
        out = WORK_DIR.joinpath(*parts[:-1], parts[-1] + ".csv")
        if state.get(row["deck"]) == row["updated_at"] and out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(row["csv"], encoding="utf-8")
        state[row["deck"]] = row["updated_at"]
        written += 1
        print(f'Supabase "{row["deck"]}" -> {out.relative_to(WORK_DIR)}')
    DOWNLOAD_STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print(f"Downloaded {written} new/updated deck{'' if written == 1 else 's'}"
          f" ({len(rows) - written} unchanged)\n")


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
    p.add_argument("--pull", metavar="DECK", help="write an existing deck out as a CSV in the folder")
    p.add_argument("--sheet", action="append", default=[], metavar="NAME_OR_URL",
                   help="export a Google Sheet's tabs into the folder as CSVs before syncing (repeatable)")
    p.add_argument("--download", action="store_true",
                   help="write decks uploaded to Supabase into the folder as CSVs before syncing")
    args = p.parse_args()

    WORK_DIR.mkdir(exist_ok=True)
    for ref in args.sheet:
        fetch_sheet(ref)
    if args.download:
        download_decks()
    try:
        if args.pull:
            return pull(args.pull)
        all_csvs = sorted(WORK_DIR.rglob("*.csv"))
        paths = [p for p in all_csvs if not p.stem.endswith(SKIP_SUFFIX)]
        print(f"Reading {WORK_DIR}: {len(paths)} CSV(s), {len(all_csvs) - len(paths)} {SKIP_SUFFIX} skipped\n")
        if not paths:
            print(f"No CSVs in {WORK_DIR}. Drop some in (path = deck name) and rerun.")
            return
        sync(paths, args.dry_run)
    except ConnectionError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
