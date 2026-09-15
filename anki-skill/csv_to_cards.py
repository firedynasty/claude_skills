#!/usr/bin/env python3
"""
Convert a raw vocab CSV into the JSON card format anki_connect.py expects.

Rule (language-agnostic — works for Chinese, Spanish, Hebrew, etc.):
    - Column 1 (the target-language term) is always the "front".
    - Columns 2+ (romanization/pinyin, translation, definition, ...) are
      joined with "\n", skipping any blank cells, to form the "back".
      This matches the join convention used in anki_to_supabase.py's
      FIELD_MAP (back_fields joined with newline).

Usage:
    python csv_to_cards.py input.csv -o cards.json [--tags tag1 tag2] [--no-header]

By default the first row is treated as a header and skipped. Pass
--no-header if the file has no header row.
"""

import argparse
import csv
import json
import sys


def convert(path: str, has_header: bool = True) -> list[dict]:
    cards = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if has_header and rows:
        rows = rows[1:]

    for row in rows:
        if not row or not any(cell.strip() for cell in row):
            continue  # skip blank lines
        front = row[0].strip()
        if not front:
            continue  # skip rows missing the term itself
        back = "\n".join(cell.strip() for cell in row[1:] if cell.strip())
        cards.append({"front": front, "back": back})

    return cards


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", help="Path to the source CSV")
    parser.add_argument("-o", "--output", required=True, help="Path to write cards JSON")
    parser.add_argument("--tags", nargs="*", default=[], help="Tags to attach to every card")
    parser.add_argument("--no-header", action="store_true", help="CSV has no header row")
    args = parser.parse_args()

    cards = convert(args.csv_file, has_header=not args.no_header)
    if args.tags:
        for c in cards:
            c["tags"] = args.tags

    if not cards:
        print("ERROR: no cards parsed — check the CSV format.", file=sys.stderr)
        sys.exit(1)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)

    print(f"Parsed {len(cards)} cards from {args.csv_file} -> {args.output}")
    print(f"  e.g. front={cards[0]['front']!r} back={cards[0]['back']!r}")


if __name__ == "__main__":
    main()
