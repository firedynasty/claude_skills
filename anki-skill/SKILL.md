# Anki Flashcard Skill

Converts any source material — YouTube video, PDF, article, or pasted text — into Anki flashcards, with an optional self-quiz step to surface knowledge gaps first.

## Trigger phrases

- "make Anki cards from…"
- "flashcard this…"
- "add to Anki…"
- "quiz me then Anki…"
- `/anki <source>`

## Inputs accepted

| Type | What to pass |
|------|-------------|
| YouTube | URL e.g. `https://youtu.be/abc123` |
| PDF / text file | File path e.g. `./paper.pdf` |
| Raw text | Paste directly into the prompt |
| Web article | URL of the article |

Optionally append:
- `--deck "DeckName"` to choose the destination Anki deck (default: `Claude::Imported`)
- `--quiz` to enable the gap-finding quiz step before card generation
- `--tags tag1 tag2` to attach Anki tags to every card

---

## Workflow

### Step 1 — Ingest the source

**YouTube URL**
```bash
python get_transcript.py "<url>" --format text
```
Read the output. It will be timestamped lines like:
```
[1:23](https://youtu.be/abc?t=83) The creator explains ...
```
Use the full transcript as the knowledge base.

**PDF or text file**
Read the file directly with the Read tool.

**Web article**
Fetch the URL with WebFetch.

**Pasted text**
Use as-is.

---

### Step 2 — (Optional) Gap-finding quiz

Only run this step if the user passed `--quiz` or asked to be quizzed first.

1. Identify the 8–12 most important concepts in the source.
2. Quiz the user on each one — ask a single question and wait for their answer before moving on.
3. Mark each answer as: **solid** / **shaky** / **missing**.
4. Only generate cards for **shaky** and **missing** concepts.
5. After the quiz summarise: "You knew X/Y topics solidly. Generating cards for the gaps."

If `--quiz` was NOT requested, skip straight to Step 3 and generate cards for all key concepts.

---

### Step 3 — Generate flashcards

Produce a JSON array of card objects. Rules:

- **Front**: one atomic question — no compound questions.
- **Back**: concise answer (1–3 sentences max). Include a timestamp link if the source was YouTube so the user can jump straight to the relevant moment.
- Aim for 10–25 cards unless the source is very short or very long.
- Prefer "why/how" questions over pure definition recall.
- For sequential content (move sequences, algorithms, steps) use cloze-style fronts: "Step 3 of the euro-step is: ___"

Example output:
```json
[
  {
    "front": "What is the key footwork principle behind the euro-step?",
    "back": "The second step plants in a different direction than the first, exploiting the gather rule. <a href='https://youtu.be/abc?t=142'>1:42</a>",
    "tags": ["basketball", "footwork"]
  }
]
```

Write this JSON to a temp file: `/tmp/anki_cards.json`

---

### Step 4 — Preview

Show the user a formatted preview table:

| # | Front (truncated) | Back (truncated) |
|---|-------------------|-----------------|
| 1 | … | … |

Ask: "Add all N cards to Anki, edit any, or cancel?"

- **Add all** → proceed to Step 5.
- **Edit** → ask which card numbers to change, apply edits, re-preview.
- **Cancel** → stop, leave `/tmp/anki_cards.json` in place so they can inspect it.

---

### Step 5 — Write to Anki

```bash
python anki_connect.py /tmp/anki_cards.json --deck "<deck>" --model Basic
```

Report the result: how many cards were added, any duplicates skipped.

If AnkiConnect is not running (connection refused), print the setup instructions below and offer to save the cards as a plain `.txt` import file instead.

---

## AnkiConnect setup (first-time)

1. Open Anki desktop.
2. Tools → Add-ons → Get Add-ons → code **2055492159** → restart Anki.
3. AnkiConnect now runs on `http://localhost:8765` whenever Anki is open.
4. Re-run this skill.

---

## Helper scripts in this folder

| Script | Purpose |
|--------|---------|
| `get_transcript.py` | Fetch YouTube transcript with clickable timestamps |
| `anki_connect.py` | POST cards to AnkiConnect; handles deck creation |
| `quiz.html` | Optional browser-based quiz UI (open locally) |

---

## Fallback: no AnkiConnect

If the user cannot use AnkiConnect, write the cards as a tab-separated `.txt` file:
```
front<TAB>back<TAB>tags
```
This format imports directly via Anki → File → Import.
