# Google Sheets import — how to use it

Lets `anki-manager.html`'s **Import CSV** modal sign in with Google, list
your recent spreadsheets, and load a tab directly — no downloading or
copy-pasting a CSV by hand.

```
anki-manager.html (browser)
    │  fetch() to localhost
    ▼
gsheet_bridge.py (Python, runs locally)
    │  Desktop-app OAuth (google-auth-oauthlib)
    ▼
Google Drive API  →  exports the spreadsheet as .xlsx  →  openpyxl reads the tab you pick
```

`anki-manager.html` is a static file with no backend of its own, so a small
local script (`gsheet_bridge.py`) does the actual Google sign-in and Drive
calls, and serves the result over `localhost` for the page to fetch. This
is the same **Desktop app** OAuth client type/flow as `live_transcribe`'s
Streamlit Google Doc import — not a browser-side Google SDK — so it works
even opening `anki-manager.html` directly via `file://`.

---

## One-time setup

1. **Google Cloud Console** → [console.cloud.google.com](https://console.cloud.google.com/) → create or pick a project.
2. **Enable the Drive API** — APIs & Services → Library → search "Google Drive API" → Enable.
3. **OAuth consent screen** — APIs & Services → OAuth consent screen. "External" user type is fine; leave it in **Testing** and add your own Google account under **Test users** (this skips Google's verification review, since only you sign in).
4. **Create credentials** — APIs & Services → Credentials → Create Credentials → OAuth client ID → Application type **Desktop app**.
5. Download the JSON and save it as:
   ```
   anki-skill/client_secret.json
   ```
6. Install the Python dependencies (once):
   ```bash
   pip install google-auth google-auth-oauthlib google-api-python-client openpyxl
   ```

`client_secret.json` and the `gsheet_token.json` it generates after you
sign in are both listed in `.gitignore` at the repo root — never commit
either, and you'll need your own copy of `client_secret.json` per machine.

---

## Every time you want to use it

1. **Start the bridge** and leave it running in a terminal:
   ```bash
   cd anki-skill
   python3 gsheet_bridge.py
   # gsheet_bridge listening on http://localhost:8787
   ```
2. **Open `anki-manager.html`** — normally (`file://` is fine), or via a local server if you prefer.
3. Click **"↑ Import CSV"** in the toolbar to open the Import CSV modal.
4. Click **"Sign in with Google"** — a browser tab opens for Google's consent screen the first time; approve it, then return to `anki-manager.html`. (Skipped automatically on later runs — `gsheet_token.json` keeps you signed in.)
5. Click **"Recent ▾"** to see your 10 most recently viewed Google Sheets, and pick one.
6. Use the **Sheet** dropdown to pick a tab — it fills the paste box below with that tab's CSV.
7. Check the preview (Front/Back columns highlighted, `✦F`/`✦B`), pick a **Destination deck**, and click **Import**.

Same rule as pasting a CSV by hand: **column 1 is always Front**, every
other non-blank column in that row joins with `\n` into **Back** — works
for 2 columns, 3 columns, or more, any language.

To sign out (e.g. to switch Google accounts), click **"Sign out"** where
"Sign in with Google" used to be — this deletes `gsheet_token.json`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Status bar says "Bridge not running — start it with: python3 gsheet_bridge.py" | You forgot step 1 above, or it crashed — check the terminal it's running in. |
| "Sign-in failed: Missing client_secret.json..." | Step 5 of setup wasn't done, or the file isn't named exactly `client_secret.json` in `anki-skill/`. |
| Browser tab for sign-in doesn't open / hangs | `run_local_server(port=0)` opens it automatically — check for a blocked pop-up, or a firewall blocking the loopback port it picks. |
| "No recent spreadsheets found" | Drive's `viewedByMeTime` only includes spreadsheets you've actually opened recently in Drive/Sheets — open the one you want there first, then click Recent ▾ again. |
| Sheet loads but preview looks wrong (English in Front instead of e.g. Chinese) | Check which column is actually column 1 in that sheet/tab — the rule is strictly positional, not "detect the language." Reorder columns in the sheet if needed. |
| Wrong Google account signed in | Click **Sign out**, then **Sign in with Google** again and pick the right account in the browser tab. |

---

## Files involved

| File | Role |
|---|---|
| `gsheet_bridge.py` | Local server: OAuth sign-in + Drive API calls. Run this before using the picker. |
| `client_secret.json` | Your OAuth client credentials (gitignored, you provide it). |
| `gsheet_token.json` | Cached sign-in, created after your first successful sign-in (gitignored). |
| `anki-manager.html` | The UI — Import CSV modal's "Sign in with Google" / "Recent ▾" / Sheet dropdown. |

### Endpoints `gsheet_bridge.py` serves (`http://localhost:8787` by default)

| Endpoint | Returns |
|---|---|
| `GET /status` | `{"signedIn": bool}` |
| `GET /signin` | Opens the browser consent flow, blocks until done, then `{"signedIn": true}` |
| `GET /signout` | Deletes the cached token, `{"signedIn": false}` |
| `GET /recent` | `{"files": [{"id", "name"}, ...]}` — up to 10, most recently viewed |
| `GET /workbook?id=` | `{"sheets": ["Tab1", "Tab2", ...]}` for that spreadsheet |
| `GET /workbook/csv?id=&sheet=` | Plain CSV text for that one tab |

Pass `--port N` to `gsheet_bridge.py` to use a different port — if you do,
also update `GSHEET_BRIDGE_URL` near the top of the
`// ── GOOGLE SHEETS PICKER ──` block in `anki-manager.html` to match.
