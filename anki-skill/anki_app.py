#!/usr/bin/env python3
"""
Streamlit app that adds and edits cards straight in Anki (the source of truth). No CSV folder in between.

    streamlit run anki_app.py

Tabs:
    Quiz        today's due cards: type an answer, OpenAI grades it, the result is recorded in Anki
    Review      today's due cards, flashcard style: reveal, then Again / Good / Easy (keys 1 2 3)
    Add cards   type cards, upload a CSV / .xlsx, or pick Google Sheet tabs -> a deck or subdeck in Anki
    Edit deck   any Anki deck as a table: change cards, add rows, optionally delete removed rows

Cards are Basic notes matched by Front within a deck. Column 1 is the Front; every other column
joins into the Back, one line each. No tags.
Requires Anki desktop running with AnkiConnect (add-on 2055492159).
"""

import csv
import html
import io
import json
import os
import re
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from anki_connect import _invoke

MODEL = "Basic"
SKIP_DECKS = {"Default", "Custom Study Session"}
# A first row containing one of these is a header, not a card.
HEADER_WORDS = {"chinese", "pinyin", "definition", "term", "front", "back", "english", "word",
                "vocab", "vocabulary", "language", "pronunciation", "中文", "translation",
                "question", "answer"}

st.set_page_config(page_title="Anki cards", page_icon="🗂️", layout="wide")


# ── Anki ──────────────────────────────────────────────────────────────────────

def anki_version():
    try:
        return _invoke("version")
    except Exception:
        return None


def read_deck(deck: str) -> dict[str, dict]:
    """{front_html: {"id", "back"}} for the Basic notes in deck itself (not its subdecks)."""
    ids = _invoke("findNotes", query=f'"deck:{deck}" -"deck:{deck}::*"')
    return {n["fields"]["Front"]["value"]: {"id": n["noteId"], "back": n["fields"]["Back"]["value"]}
            for n in (_invoke("notesInfo", notes=ids) if ids else []) if n["modelName"] == MODEL}


def anki_decks() -> list[str]:
    return sorted(d for d in _invoke("deckNames")
                  if d not in SKIP_DECKS and not d.startswith("Filtered Deck"))


def to_html(text: str) -> str:
    return html.escape(str(text).strip(), quote=False).replace("\n", "<br>")


def from_html(h: str) -> str:
    return html.unescape(re.sub(r"<br\s*/?>", "\n", h))


def edited_html(text: str) -> str:
    """An edited cell back to HTML. Cells that already hold tags (<b>, <i>, ...) are kept as typed."""
    text = str(text).strip()
    if re.search(r"</?[a-zA-Z][^>]*>", text):
        return text.replace("\n", "<br>")
    return to_html(text)


def card_fields(row: list[str]) -> tuple[str, str]:
    """Table row -> (Front, Back) HTML: column 1 is the Front, the rest join into the Back."""
    return to_html(row[0]), "<br>".join(to_html(c) for c in row[1:] if str(c).strip())


def plan_add(deck: str, df: pd.DataFrame) -> dict:
    """Compare a table with what the deck already holds (its own cards, not subdecks)."""
    have = read_deck(deck)
    new, same, changed = [], 0, []
    for row in df.fillna("").astype(str).itertuples(index=False):
        front, back = card_fields(list(row))
        if not front:
            continue
        if front not in have:
            new.append((front, back))
        elif have[front]["back"] == back:
            same += 1
        else:
            changed.append((have[front]["id"], front, back))
    return {"deck": deck, "new": new, "same": same, "changed": changed}


def apply_add(p: dict, update_backs: bool) -> str:
    deck = p["deck"]
    _invoke("createDeck", deck=deck)
    added = 0
    if p["new"]:
        notes = [{"deckName": deck, "modelName": MODEL, "fields": {"Front": f, "Back": b},
                  "options": {"allowDuplicate": True}} for f, b in p["new"]]
        added = sum(1 for r in _invoke("addNotes", notes=notes) if r is not None)
    updated = 0
    if update_backs:
        for nid, _, back in p["changed"]:
            _invoke("updateNoteFields", note={"id": nid, "fields": {"Back": back}})
            updated += 1
    msg = f"{deck}: added {added}"
    if added < len(p["new"]):
        msg += f" (✗ {len(p['new']) - added} failed)"
    msg += f", updated {updated}, {p['same']} already there"
    if p["changed"] and not update_backs:
        msg += f", {len(p['changed'])} with a different back left as is"
    return msg


# ── tables from files / sheets ────────────────────────────────────────────────

def rows_to_frame(rows: list[list]) -> pd.DataFrame:
    """A first row with a header word (front, chinese, ...) names the columns."""
    rows = [["" if c is None else str(c) for c in r] for r in rows]
    header = None
    if rows and any(c.strip().lower() in HEADER_WORDS for c in rows[0]):
        header, rows = rows[0], rows[1:]
    width = max([len(r) for r in rows] + [len(header or []), 2])
    cols = []
    for i in range(width):
        default = f"col{i + 1}" if header else "front" if i == 0 else "back" if i == 1 else f"col{i + 1}"
        name = (header[i].strip() if header and i < len(header) else "") or default
        while name in cols:
            name += "_"
        cols.append(name)
    return pd.DataFrame([r + [""] * (width - len(r)) for r in rows], columns=cols, dtype=str)


def cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def workbook_tabs(wb) -> dict[str, list[list[str]]]:
    tabs = {ws.title: [[cell(c) for c in r] for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
    return {t: r for t, r in tabs.items() if any(any(c.strip() for c in row) for row in r)}


def xlsx_tabs(data: bytes) -> dict[str, list[list[str]]]:
    from openpyxl import load_workbook
    return workbook_tabs(load_workbook(io.BytesIO(data), data_only=True, read_only=True))


def has_meaning(cells) -> bool:
    """Something after the Front that isn't just a link."""
    return any(c.strip() and not re.fullmatch(r"https?://\S+", c.strip()) for c in cells)


SUBDECK = "→ subdeck"  # column added when a tab is split into subdecks by one of its columns
DECK_WORDS = ("deck", "subdeck", "sub deck", "sub-deck")


def clean(df: pd.DataFrame, need_back: bool) -> pd.DataFrame:
    """Drop rows without a Front, rows with no meaning after the Front (section titles, links,
    whole sentences), and repeated Fronts (first one kept; per subdeck when split)."""
    df = df.fillna("").astype(str)
    df = df[df.iloc[:, 0].str.strip() != ""]
    fields = [c for c in df.columns[1:] if c != SUBDECK]
    if need_back:
        df = df[df[fields].apply(has_meaning, axis=1)] if fields else df.iloc[0:0]
    key = [df.columns[0]] + ([SUBDECK] if SUBDECK in df.columns else [])
    return df.drop_duplicates(subset=key).reset_index(drop=True)


def pick_tabs(tabs: dict, src_id: str) -> list[str]:
    key = f"tabs::{src_id}"
    b1, b2, _ = st.columns([1, 1, 5])
    if b1.button("Select all tabs", key=f"all::{src_id}"):
        st.session_state[key] = list(tabs)
    if b2.button("Clear", key=f"none::{src_id}"):
        st.session_state[key] = []
    return st.multiselect(f"Tabs to add — {len(tabs)} in this file, each becomes a subdeck", list(tabs), key=key)


FRONT_WORDS = ("front", "question", "chinese", "中文", "term", "word", "vocab", "vocabulary")


def guess_columns(df: pd.DataFrame) -> tuple[str, list[str]]:
    """Best Front column, and columns to leave out by default (#, row numbers, a lesson label repeated down the rows)."""
    cols = list(df.columns)
    front = next((c for w in FRONT_WORDS for c in cols if c.strip().lower() == w), cols[0])
    drop = []
    for c in cols:
        vals = df[c].fillna("").astype(str).str.strip()
        vals = vals[vals != ""]
        if c == front or vals.empty:
            continue
        numeric = vals.str.fullmatch(r"\d+").all()
        repeated = len(vals) > 3 and vals.nunique() <= max(1, len(vals) // 10)
        if c.strip() == "#" or numeric or repeated:
            drop.append(c)
    return front, drop


def arrange(df: pd.DataFrame, front: str, drop: list[str]) -> pd.DataFrame:
    return df[[front] + [c for c in df.columns if c != front and c not in drop]]


def stem(name: str) -> str:
    name = Path(name).stem if name.lower().endswith((".csv", ".xlsx")) else name
    return re.sub(r"[/\\:]", "-", name).strip()


# ── Google ────────────────────────────────────────────────────────────────────

def google():
    """(credentials or None, error text or None) from gsheet_bridge's saved sign-in."""
    try:
        import gsheet_bridge as gb
        return gb.get_credentials(), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def find_sheets(creds, term: str) -> list[dict]:
    """Google Sheets whose name contains term (newest edit first); no term = the 50 most recently opened."""
    from googleapiclient.discovery import build
    drive = build("drive", "v3", credentials=creds)
    q = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    if term:
        q += " and name contains '" + term.replace("\\", "\\\\").replace("'", "\\'") + "'"
    resp = drive.files().list(q=q, orderBy="modifiedTime desc" if term else "viewedByMeTime desc",
                              pageSize=50, fields="files(id, name, modifiedTime)").execute()
    return resp.get("files", [])


GOOGLE_FIX = ("Google sign-in for these Python tools isn't working (the OAuth client in `client_secret.json` "
              "is missing or was deleted). To fix: Google Cloud Console → APIs & Services → Credentials → "
              "Create OAuth client ID → **Desktop app** → download it as `client_secret.json` into the anki-skill "
              "folder, delete `gsheet_token.json`, then press Sign in. Meanwhile: in Google Sheets use "
              "File → Download → Microsoft Excel (.xlsx) and upload it here.")


def diff_edit(orig: pd.DataFrame, edited: pd.DataFrame):
    """(changed [(id, front, back)], new [(front, back)], removed [id]) between the deck table and its edit."""
    rows = edited.fillna("").astype(str)
    old = orig.set_index("id")
    kept = rows[rows["id"] != ""]
    changes = [(int(r.id), r.Front, r.Back) for r in kept.itertuples()
               if (r.Front, r.Back) != (old.loc[r.id, "Front"], old.loc[r.id, "Back"])]
    added = [(r.Front, r.Back) for r in rows[rows["id"] == ""].itertuples() if r.Front.strip()]
    removed = [int(i) for i in orig["id"] if i not in set(kept["id"])]
    return changes, added, removed


# ── sidebar ───────────────────────────────────────────────────────────────────

st.sidebar.title("🗂️ Anki cards")
ver = anki_version()
if not ver:
    st.sidebar.error("Anki not reachable")
    st.error("Open Anki desktop (with the AnkiConnect add-on, 2055492159), then press ↻ Refresh.")
    if st.button("↻ Refresh"):
        st.rerun()
    st.stop()
st.sidebar.success(f"Anki connected (AnkiConnect v{ver})")
if st.sidebar.button("↻ Refresh"):
    st.rerun()
st.sidebar.markdown(
    "Cards go **straight into Anki**.\n\n"
    "- Column 1 → Front, other columns → Back\n"
    "- A Front already in the deck is never added twice\n"
    "- Nothing is deleted unless you tick the delete box in Edit deck\n"
    "- Then sync Anki → AnkiWeb for your phone"
)
DECKS = anki_decks()

tab_quiz, tab_review, tab_add, tab_edit = st.tabs(["Quiz", "Review", "Add cards", "Edit deck"])


# ── Study (Quiz + Review): today's due cards, results recorded in Anki ───────

GRADER_MODEL = os.environ.get("ANKI_QUIZ_MODEL", "gpt-4.1-mini")
PLANNER_MODEL = os.environ.get("ANKI_PLAN_MODEL", "gpt-4.1")  # writes the grouped questions: one call per 25 cards
EASE = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


def plain(h: str) -> str:
    return re.sub(r"<[^>]+>", "", from_html(h)).strip()


def study_queue(deck: str, subs: bool, new_limit: int, practice: bool) -> list[int]:
    base = f'"deck:{deck}"' + ("" if subs else f' -"deck:{deck}::*"') + " -is:suspended -is:buried"
    if practice:
        return _invoke("findCards", query=base)
    return _invoke("findCards", query=base + " is:due") + _invoke("findCards", query=base + " is:new")[:new_limit]


def card_text(ids: list[int]) -> dict[int, tuple[str, str, bool]]:
    """card id -> (front html, back html, is new): the note's first and second fields, and whether the
    card has never been studied."""
    out = {}
    for c in _invoke("cardsInfo", cards=ids) if ids else []:
        fields = sorted(c["fields"].values(), key=lambda f: f["order"])
        out[c["cardId"]] = (fields[0]["value"], fields[1]["value"] if len(fields) > 1 else "", c["type"] == 0)
    return out


def grade(front: str, back: str, answer: str) -> tuple[bool, str]:
    from openai import OpenAI
    resp = OpenAI().chat.completions.create(
        model=GRADER_MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content":
                'You grade flashcard answers. Reply only JSON: {"correct": true|false, "feedback": "..."}. '
                "Correct if the answer shows the learner knows the card: same meaning, a synonym, any one of "
                "several listed meanings, minor typos or missing tone marks are fine. Pinyin / romanization is "
                "optional unless the card asks only for it. Wrong if blank, vague, or a different meaning. "
                "Feedback: one short sentence; when wrong, give the right answer."},
            {"role": "user", "content": f"Card front:\n{plain(front)}\n\nCorrect answer (card back):\n{plain(back)}"
                                        f"\n\nLearner's answer:\n{answer}"},
        ])
    data = json.loads(resp.choices[0].message.content)
    return bool(data.get("correct")), str(data.get("feedback", ""))


def ask_json(system: str, user: str, model: str = GRADER_MODEL) -> dict:
    from openai import OpenAI
    resp = OpenAI().chat.completions.create(
        model=model, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return json.loads(resp.choices[0].message.content)


PLAN_PROMPT = (
    "You turn flashcards into a short quiz with FEWER questions than cards. Group 2-3 cards that fit naturally "
    "into one question: words that make one short, natural, grammatically correct sentence (a sentence a native "
    "speaker would say, with a subject and verb; e.g. 我晚上食晚飯 for 晚上 + 晚飯; you may add a few very common "
    "words), or closely related facts. For each group write ONE "
    "question the learner can only answer well if they know every card in it, e.g. 'Translate: <that sentence>' "
    "or one question linking the facts. Keep the cards' direction: show the front side's language and ask for "
    "the back side's (Chinese fronts -> ask for the English meaning; never ask the learner to produce the front "
    "side), and keep the cards' script and dialect "
    "(traditional stays traditional; Cantonese words in a Cantonese sentence). Never force a group: if no natural "
    "sentence or link exists, give the card its own question (just ask the card). Aim for about one question per "
    "2-3 cards. A sentence may also use words from other groups or common words as context; only its group's cards "
    "are graded. Every card number appears in exactly "
    'one group. Reply only JSON: {"groups": [{"cards": [numbers], "question": "...", "answer": "model answer"}]}')

GROUP_GRADE_PROMPT = (
    "Grade a learner's answer to one quiz question that covers several flashcards. For each card give one verdict. "
    "Be lenient on form: synonyms, word order, typos and missing tone marks are fine; pinyin is optional.\n"
    '"clear": the answer renders this card\'s own meaning specifically and correctly, in a way the rest of the '
    "sentence or a well-known phrase would not give away.\n"
    '"context": the answer gets it right, but the meaning is easy to guess from the other words, the sentence, or '
    "a memorised phrase or verse, or the answer is vague or generic about this word.\n"
    '"missed": wrong, missing, or the answer is blank.\n'
    'Reply only JSON: {"results": {"<card number>": "clear"|"context"|"missed"}, "feedback": "one or two short '
    'sentences; for each missed card give its right meaning"}')

VERDICTS = {"clear": "✓ Knew it", "context": "~ Guessed (Hard)", "missed": "✗ Missed"}


def single(c: int, text: dict) -> dict:
    return {"cids": [c], "question": plain(text[c][0]), "answer": plain(text[c][1])}


def plan_groups(ids: list[int], text: dict, chunk: int = 25) -> list[dict]:
    """[{"cids": [...], "question": str, "answer": str}] covering every card once. New cards (never studied)
    are asked on their own, first; studied cards are grouped 2-3 per question. Falls back to one card per
    question for anything the planner drops or if the call fails."""
    new = [c for c in ids if text[c][2]]
    groups, seen = [single(c, text) for c in new], set(new)
    ids = [c for c in ids if c not in seen]
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        lines = "\n".join(f"{n}. {plain(text[c][0])} → {plain(text[c][1])}".replace("\n", " / ")
                          for n, c in enumerate(part, 1))
        try:
            data = ask_json(PLAN_PROMPT, "Cards:\n" + lines, PLANNER_MODEL)
        except Exception:  # noqa: BLE001
            data = {"groups": []}
        for g in data.get("groups", []):
            cids = []
            for n in g.get("cards", []):
                try:
                    c = part[int(n) - 1]
                except (ValueError, IndexError):
                    continue
                if c not in seen:
                    seen.add(c)
                    cids.append(c)
            q = str(g.get("question", "")).strip()
            if len(cids) > 1:
                # Word cards (short fronts) only count as covered if the word is in the question; the rest get
                # their own question below. Stops questions that ask the learner to produce the front side.
                missing = [c for c in cids if len(plain(text[c][0])) <= 12 and plain(text[c][0]) not in q]
                for c in missing:
                    seen.discard(c)
                cids = [c for c in cids if c not in missing]
            for c in cids[3:]:  # groups stay small: extra cards get their own question
                seen.discard(c)
            cids = cids[:3]
            if cids and q:
                groups.append({"cids": cids, "question": q, "answer": str(g.get("answer", ""))})
        for c in part:  # anything the planner left out: ask it on its own
            if c not in seen:
                seen.add(c)
                groups.append(single(c, text))
    return groups


def grade_group(g: dict, text: dict, answer: str) -> tuple[dict, str]:
    """({card id: "clear" | "context" | "missed"}, feedback). A card asked on its own is never "context"."""
    if not answer.strip():
        return {c: "missed" for c in g["cids"]}, ""
    cards = "\n".join(f"{n}. {plain(text[c][0])} → {plain(text[c][1])}".replace("\n", " / ")
                      for n, c in enumerate(g["cids"], 1))
    data = ask_json(GROUP_GRADE_PROMPT, f"Question:\n{g['question']}\n\nModel answer:\n{g['answer']}\n\n"
                                        f"Cards it covers:\n{cards}\n\nLearner's answer:\n{answer}")
    res = data.get("results", {})
    out = {}
    for n, c in enumerate(g["cids"], 1):
        v = res.get(str(n), "missed")
        v = {True: "clear", False: "missed"}.get(v, v) if isinstance(v, bool) else str(v).lower()
        v = v if v in VERDICTS else "missed"
        out[c] = "clear" if v == "context" and len(g["cids"]) == 1 else v
    return out, str(data.get("feedback", ""))


def deck_counts(deck: str) -> str:
    try:
        st_ = next(iter(_invoke("getDeckStats", decks=[deck]).values()))
        return f"{st_['review_count']} to review · {st_['learn_count']} learning · {st_['new_count']} new today"
    except Exception:  # noqa: BLE001
        return ""


def study_setup(mode: str, extra: dict | None = None):
    """Deck + options + Start. Returns nothing; Start puts a fresh session in st.session_state[mode]."""
    c1, c2, c3 = st.columns([3, 2, 2])
    deck = c1.selectbox("Deck", DECKS, key=f"{mode}_deck")
    subs = c2.checkbox("Include subdecks", value=True, key=f"{mode}_subs")
    new_limit = c3.number_input("New cards (max)", 0, 200, 10, key=f"{mode}_new")
    which = st.radio("Cards", ["Due today — recorded in Anki", "All cards — practice, not recorded"],
                     horizontal=True, key=f"{mode}_which")
    practice = which.startswith("All")
    if deck:
        st.caption(deck_counts(deck))
    if st.button("Start", type="primary", key=f"{mode}_start", disabled=not deck):
        ids = study_queue(deck, subs, int(new_limit), practice)
        st.session_state[mode] = {"ids": ids, "text": card_text(ids), "i": 0, "pending": None,
                                  "log": [], "record": not practice, "deck": deck, "revealed": False,
                                  **(extra or {})}
        st.rerun()


def record(sess: dict, cid: int, ease: int):
    if sess["record"]:
        _invoke("answerCards", answers=[{"cardId": cid, "ease": ease}])
    sess["log"].append((cid, ease))


def study_summary(mode: str, sess: dict):
    right = [c for c, e in sess["log"] if e > 2]
    hard = [c for c, e in sess["log"] if e == 2]
    wrong = [c for c, e in sess["log"] if e == 1]
    st.success(f"Done with {sess['deck']}: {len(right)} right · {len(hard)} hard · {len(wrong)} again"
               + ("" if sess["record"] else " (practice — nothing recorded)"))
    for title, cards in (("To go over", wrong), ("Hard / guessed", hard)):
        if cards:
            st.write(f"**{title}:**")
            st.markdown("\n".join(f"- {plain(sess['text'][c][0])} → {plain(sess['text'][c][1])}" for c in cards))
    if sess["record"]:
        if sess["log"] and not sess.get("refreshed"):
            try:  # Anki's window doesn't redraw its counts after outside changes; show the deck list fresh
                _invoke("guiDeckBrowser")
            except Exception:  # noqa: BLE001
                pass
            sess["refreshed"] = True
        st.caption(f"{len(sess['log'])} answers saved in Anki (its deck list is refreshed). "
                   "Sync Anki → AnkiWeb to send them to your phone.")
    if st.button("New session", key=f"{mode}_again"):
        st.session_state.pop(mode)
        st.rerun()


def show_front(front_html: str):
    st.markdown(f"<div style='font-size:2.2rem;line-height:1.3;margin:.4rem 0 .8rem'>{front_html}</div>",
                unsafe_allow_html=True)


def keys_js(script: str):
    """Run a small script in the page itself (Streamlit puts components in an iframe)."""
    components.html(f"<script>(function(){{const d = window.parent.document; {script}}})();</script>", height=0)


# Quiz: type, OpenAI grades. The verdict waits on screen (Flip fixes it) and is written to Anki
# when you answer the next card, so a wrong verdict never reaches Anki.
with tab_quiz:
    if not os.environ.get("OPENAI_API_KEY"):
        st.warning("OPENAI_API_KEY isn't set: start the app from a terminal that loads ~/.secrets.")
    sess = st.session_state.get("quiz")
    if not sess:
        style = st.radio("Quiz style", ["Grouped — one question covers several cards", "One card at a time"],
                         horizontal=True, key="quiz_style")
        study_setup("quiz", {"grouped": style.startswith("Grouped")})
        st.caption("Type the answer, press Enter. Empty + Enter = don't know. Every card a right answer covers "
                   "is marked Good, a missed one Again; written to Anki when you move on.")
    elif sess.get("grouped"):
        if "groups" not in sess:
            with st.spinner(f"Writing questions for {len(sess['ids'])} cards…"):
                sess.update(groups=plan_groups(sess["ids"], sess["text"]), gi=0)
        groups, gi, p = sess["groups"], sess["gi"], sess["pending"]
        good = 3  # right / knew it → Good

        ease_for = {"clear": good, "context": 2, "missed": 1}

        def verdict(p, c):
            return st.session_state.get(f"pg::{p['gi']}::{c}", p["ok"][c])

        def commit(p):
            for c in p["cids"]:
                record(sess, c, ease_for[verdict(p, c)])
            sess["pending"] = None

        if p:
            vs = [verdict(p, c) for c in p["cids"]]
            counts = " · ".join(f"{vs.count(k)} {w}" for k, w in (("clear", "knew"), ("context", "guessed"),
                                                                   ("missed", "missed")) if vs.count(k))
            box = st.success if set(vs) == {"clear"} else st.error if set(vs) == {"missed"} else st.warning
            box(f"**{counts}** — Q: {p['question']}"
                + (f"\n\nYou: {p['answer']}" if p["answer"] else "")
                + (f"\n\nModel answer: {p['model']}" if p["model"] else "")
                + (f"\n\n{p['feedback']}" if p["feedback"] else ""))
            st.caption("Change any verdict before you move on — Knew it → Good, Guessed → Hard, Missed → Again:")
            for c in p["cids"]:
                st.radio(f"{plain(sess['text'][c][0])} → {plain(sess['text'][c][1])}".replace("\n", " "),
                         list(VERDICTS), format_func=VERDICTS.get, index=list(VERDICTS).index(p["ok"][c]),
                         horizontal=True, key=f"pg::{p['gi']}::{c}")
        if gi < len(groups):
            g = groups[gi]
            covered = sum(len(x["cids"]) for x in groups[:gi])
            st.caption(f"Question {gi + 1} of {len(groups)} · covers {len(g['cids'])} card"
                       f"{'s' if len(g['cids']) != 1 else ''} · {covered} of {len(sess['ids'])} cards done · "
                       f"{sess['deck']}" + ("" if sess["record"] else " · practice"))
            show_front(html.escape(g["question"]))
            with st.form(f"quizg_form_{gi}", clear_on_submit=True):
                answer = st.text_input("Your answer", placeholder="type, then Enter")
                go = st.form_submit_button("Check ⏎")
            keys_js("const el = d.querySelector('input[aria-label=\"Your answer\"]'); if (el) setTimeout(() => el.focus(), 50);")
            if go:
                if p:
                    commit(p)
                try:
                    ok, fb = grade_group(g, sess["text"], answer.strip())
                except Exception as e:  # noqa: BLE001 -- keep going; tick the boxes if you were right
                    ok, fb = {c: "missed" for c in g["cids"]}, f"Couldn't grade ({e}); set the cards you knew."
                sess["pending"] = {"gi": gi, "cids": g["cids"], "ok": ok, "question": g["question"],
                                   "model": g["answer"], "answer": answer.strip(), "feedback": fb}
                sess["gi"] += 1
                st.rerun()
            if st.button("Stop & save", key="quizg_stop", help="Saves every answer so far, including the last one, and ends"):
                if p:
                    commit(p)
                sess["ids"] = [c for x in groups[:gi] for c in x["cids"]]
                sess["groups"] = groups[:gi]
                st.rerun()
        elif p:
            if st.button("Save last answer and finish", type="primary"):
                commit(p)
                st.rerun()
        else:
            if not sess["ids"]:
                st.info("Nothing due in this deck right now. Pick “All cards — practice” to go through it anyway.")
            study_summary("quiz", sess)
            if not sess["ids"] and st.button("Back", key="quizg_back"):
                st.session_state.pop("quiz")
                st.rerun()
    else:
        ids, i = sess["ids"], sess["i"]
        good = 3  # right / knew it → Good
        p = sess["pending"]
        if p:
            box = st.success if p["ease"] > 1 else st.error
            box(f"{'✓ Right' if p['ease'] > 1 else '✗ Again'} — {plain(p['front'])} → {plain(p['back'])}"
                + (f"\n\nYou: {p['answer']}" if p["answer"] else "") + (f"\n\n{p['feedback']}" if p["feedback"] else ""))
            if st.button("Flip ↔ (the grader got it wrong)", key=f"flip{i}"):
                p["ease"] = 1 if p["ease"] > 1 else good
                st.rerun()
        if i < len(ids):
            front, back = sess["text"][ids[i]][:2]
            st.caption(f"Card {i + 1} of {len(ids)} · {sess['deck']}" + ("" if sess["record"] else " · practice"))
            show_front(front)
            with st.form(f"quiz_form_{i}", clear_on_submit=True):
                answer = st.text_input("Your answer", placeholder="type, then Enter")
                go = st.form_submit_button("Check ⏎")
            keys_js("const el = d.querySelector('input[aria-label=\"Your answer\"]'); if (el) setTimeout(() => el.focus(), 50);")
            if go:
                if p:
                    record(sess, p["cid"], p["ease"])
                try:
                    ok, fb = (False, "") if not answer.strip() else grade(front, back, answer.strip())
                except Exception as e:  # noqa: BLE001 -- keep going; Flip if you were right
                    ok, fb = False, f"Couldn't grade ({e}); marked Again — press Flip if you were right."
                sess["pending"] = {"cid": ids[i], "front": front, "back": back, "answer": answer.strip(),
                                   "ease": good if ok else 1, "feedback": fb}
                sess["i"] += 1
                st.rerun()
            if st.button("Stop & save", key="quiz_stop", help="Saves every answer so far, including the last one, and ends"):
                if p:
                    record(sess, p["cid"], p["ease"])
                sess["pending"], sess["ids"] = None, ids[:i]
                st.rerun()
        elif p:
            if st.button("Save last card and finish", type="primary"):
                record(sess, p["cid"], p["ease"])
                sess["pending"] = None
                st.rerun()
        else:
            if not ids:
                st.info("Nothing due in this deck right now. Pick “All cards — practice” to go through it anyway.")
            study_summary("quiz", sess)
            if not ids and st.button("Back", key="quiz_back"):
                st.session_state.pop("quiz")
                st.rerun()

# Review: flashcard style. Space reveals; 1 Again, 2 Hard, 3 Good, 4 Easy (Enter = Good).
with tab_review:
    sess = st.session_state.get("review")
    if not sess:
        study_setup("review")
        st.caption("Space shows the answer, then 1 Again · 2 Hard · 3 Good (or Enter) · 4 Easy.")
    else:
        ids, i = sess["ids"], sess["i"]
        if i < len(ids):
            front, back = sess["text"][ids[i]][:2]
            st.caption(f"Card {i + 1} of {len(ids)} · {sess['deck']}" + ("" if sess["record"] else " · practice"))
            show_front(front)
            if not sess["revealed"]:
                if st.button("Show answer (Space)", type="primary", key=f"rev_show{i}"):
                    sess["revealed"] = True
                    st.rerun()
            else:
                st.markdown(f"<div style='font-size:1.4rem;line-height:1.4;padding:.6rem 0;border-top:1px solid #ddd'>"
                            f"{back}</div>", unsafe_allow_html=True)
                cols = st.columns(4)
                for col, ease in zip(cols, (1, 2, 3, 4)):
                    if col.button(f"{EASE[ease]} ({ease})", key=f"rev_{ease}_{i}", type="primary" if ease == 3 else "secondary"):
                        record(sess, ids[i], ease)
                        sess["i"] += 1
                        sess["revealed"] = False
                        st.rerun()
            if st.button("Stop & save", key="rev_stop", help="Each answer is already saved when you press it; this just ends"):
                sess["ids"] = ids[:i]
                st.rerun()
            # Keyboard: one listener installed in the page itself (via the page's own eval), so it keeps
            # working after Streamlit replaces the component frame on every rerun. It clicks the visible button.
            keys_js("const w = window.parent; if (w.__ankiReviewKeys) return; w.__ankiReviewKeys = true; w.eval("
                    + json.dumps("""
                document.addEventListener('keydown', function (e) {
                    const t = e.target; if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA')) return;
                    if (e.metaKey || e.ctrlKey || e.altKey) return;
                    const map = {' ': 'Show answer', '1': 'Again (1)', '2': 'Hard (2)', '3': 'Good (3)', '4': 'Easy (4)', 'Enter': 'Good (3)'};
                    const label = map[e.key]; if (!label) return;
                    const btn = [...document.querySelectorAll('button')].find(b => b.offsetParent !== null && b.innerText.trim().startsWith(label));
                    if (btn) { e.preventDefault(); btn.click(); }
                });""") + ");")
        else:
            if not ids:
                st.info("Nothing due in this deck right now. Pick “All cards — practice” to go through it anyway.")
            study_summary("review", sess)
            if not ids and st.button("Back", key="rev_back"):
                st.session_state.pop("review")
                st.rerun()

# ── Add cards ─────────────────────────────────────────────────────────────────

def parent_picker(suggest: str) -> str:
    """Parent deck dropdown. Follows the spreadsheet / file name until you pick something yourself;
    your pick is kept in parent_choice so it survives the dropdown disappearing between choices."""
    auto = suggest if suggest in DECKS else f"{suggest}  (new)" if suggest else "(none — top-level deck)"
    value = st.session_state.get("parent_choice") or auto
    opts = ["(none — top-level deck)"]
    for extra in (auto, value):
        if extra not in opts and extra not in DECKS and extra != "(type a new name…)":
            opts.append(extra)
    opts += DECKS + ["(type a new name…)"]
    st.session_state.parent = value
    pick = st.selectbox("Parent deck", opts, key="parent",
                        on_change=lambda: st.session_state.update(parent_choice=st.session_state.parent))
    if pick == "(type a new name…)":
        return st.text_input("New parent deck", placeholder="biology", key="parent_new").strip()
    if pick.startswith("(none"):
        return ""
    return pick.removesuffix("  (new)")


with tab_add:
    source = st.radio("Cards from", ["Type them", "Upload CSV / Excel", "Google Sheet"], horizontal=True)
    tables, src_id, suggest_parent = {}, "", ""  # suggested subdeck name -> rows

    if source == "Type them":
        tables, src_id = {"": [["front", "back"]]}, "typed"

    elif source == "Upload CSV / Excel":
        up = st.file_uploader("CSV, or an .xlsx (e.g. Google Sheets → File → Download → .xlsx)", type=["csv", "xlsx"])
        if up:
            src_id = f"{up.name}:{up.size}"
            if up.name.lower().endswith(".xlsx"):
                tabs = xlsx_tabs(up.getvalue())
                pick = pick_tabs(tabs, src_id)
                tables, suggest_parent = {t: tabs[t] for t in pick}, stem(up.name)
            else:
                tables = {stem(up.name): list(csv.reader(io.StringIO(up.getvalue().decode("utf-8-sig"))))}

    else:  # Google Sheet
        creds, err = google()
        if not creds:
            if err:
                st.error(GOOGLE_FIX if "client" in err.lower() else f"Google sign-in error: {err}")
            if st.button("Sign in with Google"):
                try:
                    import gsheet_bridge as gb
                    gb.sign_in()
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(GOOGLE_FIX if "client" in str(e).lower() else f"Sign-in failed: {e}")
        else:
            import gsheet_bridge as gb
            c1, c2 = st.columns(2)
            term = c1.text_input("Search your Google Sheets", placeholder="chinese")
            url = c2.text_input("…or paste a spreadsheet URL / ID")
            file_id, sheet_name = None, ""
            m = re.search(r"/d/([\w-]+)", url) or re.fullmatch(r"([\w-]{25,})", url.strip())
            if m:
                file_id = m.group(1)
            else:
                found = find_sheets(creds, term.strip())
                if found:
                    labels = [f"{f['name']}  ·  edited {f['modifiedTime'][:10]}" for f in found]
                    i = st.selectbox(f"{'Matches' if term.strip() else 'Recently opened'} ({len(found)})",
                                     range(len(found)), format_func=lambda i: labels[i])
                    file_id, sheet_name = found[i]["id"], found[i]["name"]
                else:
                    st.info(f"No Google Sheets match “{term}”. (An uploaded .xlsx in Drive isn't a Google Sheet: "
                            "open it and use File → Save as Google Sheets, or upload the .xlsx here instead.)")
            if file_id:
                tabs = workbook_tabs(gb.fetch_workbook(creds, file_id))
                pick = pick_tabs(tabs, file_id)
                tables, src_id = {t: tabs[t] for t in pick}, file_id
                suggest_parent = stem(sheet_name) if sheet_name else ""

    if tables:
        c1, c2 = st.columns(2)
        with c1:
            parent = parent_picker(suggest_parent)
        need_back = c2.checkbox("Skip rows with no meaning (titles, links, lone sentences)", value=True,
                                help="Drops rows with nothing after column 1, or only a link")
        update_backs = c2.checkbox("Update the back of cards already in the deck", value=True,
                                   help="When a Front is already in the deck but its Back differs")
        jobs = []  # (deck, table)
        for suggest, rows in tables.items():
            with st.expander(suggest or "New cards", expanded=len(tables) == 1):
                df = rows_to_frame(rows)
                split = None
                if src_id != "typed":
                    cols = list(df.columns)
                    auto = next((c for c in cols if c.strip().lower() in DECK_WORDS), None)
                    k0, k1, k2 = st.columns(3)
                    split = k0.selectbox("Split into subdecks by column", ["(no — one subdeck)"] + cols,
                                         index=cols.index(auto) + 1 if auto else 0, key=f"split::{src_id}::{suggest}",
                                         help="Each row goes to the subdeck named in this column, e.g. a 'deck' column")
                    split = None if split.startswith("(no") else split
                    rest = df.drop(columns=[split]) if split else df
                    front, drop = guess_columns(rest)
                    front = k1.selectbox("Front column", list(rest.columns), index=list(rest.columns).index(front),
                                         key=f"front::{src_id}::{suggest}::{split}")
                    drop = k2.multiselect("Leave out columns", [c for c in rest.columns if c != front],
                                          default=[c for c in drop if c != front], key=f"drop::{src_id}::{suggest}::{split}")
                    card = arrange(rest, front, drop)
                    if split:
                        card[SUBDECK] = df[split]
                    df = clean(card, need_back)
                label = ("Put the subdecks under (optional)" if split
                         else "Subdeck name" if parent else "Deck name")
                sub = st.text_input(label, value="" if split else stem(suggest) if suggest else "",
                                    placeholder="histones", key=f"name::{src_id}::{suggest}::{bool(split)}").strip()
                edited = st.data_editor(df, num_rows="dynamic", use_container_width=True,
                                        key=f"new::{src_id}::{suggest}::{need_back}::{list(df.columns)}")
                cards = clean(edited, False)
                groups = (list(cards.groupby(SUBDECK, sort=False)) if SUBDECK in cards.columns
                          else [("", cards)])
                targets, unnamed = [], 0
                for val, g in groups:
                    deck = "::".join(x for x in [parent, sub, str(val).strip()] if x)
                    if not deck:
                        unnamed += len(g)
                        continue
                    targets.append((deck, g.drop(columns=[SUBDECK], errors="ignore")))
                if not len(cards):
                    st.caption("No cards yet — add rows above.")
                elif not targets:
                    st.caption("Give it a name to add it.")
                else:
                    st.caption("→ " + " · ".join(f"**{d}** ({'exists' if d in DECKS else 'new'}, {len(g)})"
                                                 for d, g in targets)
                               + (f" — {unnamed} rows with no subdeck and no name are skipped" if unnamed else ""))
                    jobs.extend(targets)

        c1, c2, _ = st.columns([1, 1, 4])
        if c1.button("Preview", key="add_prev", disabled=not jobs):
            plans = [plan_add(d, df) for d, df in jobs]
            st.session_state.add_out = pd.DataFrame([{
                "deck": p["deck"], "new": len(p["new"]), "already there": p["same"],
                "different back": len(p["changed"])} for p in plans])
        if c2.button("Add to Anki", type="primary", key="add_go", disabled=not jobs):
            st.session_state.add_out = "\n".join(apply_add(plan_add(d, df), update_backs) for d, df in jobs)
        out = st.session_state.get("add_out")
        if isinstance(out, pd.DataFrame):
            st.dataframe(out, hide_index=True)
            st.caption("Nothing changed yet — press Add to Anki.")
        elif out:
            (st.warning if "✗" in out else st.success)("Done — sync Anki to AnkiWeb to get these on your phone")
            st.code(out, language=None)

# ── Edit deck ─────────────────────────────────────────────────────────────────

with tab_edit:
    deck = st.selectbox("Anki deck", DECKS, key="edit_deck")
    if deck:
        notes = read_deck(deck)
        orig = pd.DataFrame([{"id": str(n["id"]), "Front": from_html(f), "Back": from_html(n["back"])}
                             for f, n in notes.items()], columns=["id", "Front", "Back"])
        st.caption(f"{len(orig)} cards in **{deck}** itself (subdecks are listed separately). "
                   "Edit cells, add rows at the bottom, or select rows and press Delete. "
                   "Line breaks in the Back show as new lines.")
        edited = st.data_editor(orig, num_rows="dynamic", use_container_width=True, key=f"edit::{deck}",
                                column_config={"id": None})
        old = orig.set_index("id")
        changes, added, removed = diff_edit(orig, edited)

        st.write(f"**{len(changes)}** changed · **{len(added)}** new · **{len(removed)}** removed from the table")
        really_delete = False
        if removed:
            really_delete = st.checkbox(f"Also delete those {len(removed)} card(s) from Anki (review history is lost)",
                                        value=False)
        if st.button("Save to Anki", type="primary", disabled=not (changes or added or (removed and really_delete))):
            for nid, front, back in changes:
                was = old.loc[str(nid)]
                fields = {k: edited_html(v) for k, v in (("Front", front), ("Back", back)) if v != was[k]}
                _invoke("updateNoteFields", note={"id": nid, "fields": fields})
            have = set(notes)
            fresh = [(to_html(f), to_html(b)) for f, b in added if to_html(f) not in have]
            if fresh:
                _invoke("addNotes", notes=[{"deckName": deck, "modelName": MODEL, "fields": {"Front": f, "Back": b},
                                            "options": {"allowDuplicate": True}} for f, b in fresh])
            if removed and really_delete:
                _invoke("deleteNotes", notes=removed)
            st.session_state.pop(f"edit::{deck}", None)
            st.session_state.edit_out = (f"{deck}: {len(changes)} updated, {len(fresh)} added"
                                         + (f", {len(removed)} deleted" if removed and really_delete else ""))
            st.rerun()
        if st.session_state.get("edit_out"):
            st.success(st.session_state.pop("edit_out"))
