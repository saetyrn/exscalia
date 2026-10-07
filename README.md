# EXSCALIA

A self-hosted, single-user markdown notebook with **OMNIA**, an AI overseer
that can search, organize, and operate your vault. Explicit requests run
immediately as scoped Git commits with **Undo**; proactive suggestions remain
reviewable, and destructive changes require a separate confirmation.

- Notes are plain `.md` files with YAML frontmatter, stored in a **git
  repository** that is the source of truth. Your edits autosave and commit
  quietly; approved AI changes commit with a distinct `OMNIA` author.
- **Wikilinks** `[[Note]]` with autocomplete and a backlinks panel; **tags**
  are `#hashtags` typed anywhere in a note (legacy frontmatter `tags:` also
  read); **daily notes**; user-definable **templates**; and a three-state
  **Tasks** board (`[ ]`, `[/]`, `[x]`) whose first created/started/completed
  milestones appear on the calendar.
- **One blended search** combines keyword (SQLite FTS5) and semantic (local
  [fastembed](https://github.com/qdrant/fastembed) embeddings in embedded
  [LanceDB](https://lancedb.com) — no external services, no API cost, chunked
  by markdown heading, re-embedded incrementally on save), with safe text
  highlighting, filters, and task results.
- Local IndexedDB drafts, revision-checked saves, Git version history, and a
  30-day Git-backed Trash keep edits recoverable across reloads and devices.
- A **live-preview editor**: markdown renders in place and the caret's line
  shows its raw syntax, so there is one always-editable view.
- A **media tracker** for books, films, TV and games: a poster-grid library,
  watchlist, activity timeline and a detail page per item, each one a note
  under `media/` with `media:` frontmatter and a dated progress log. Metadata
  and covers come from Open Library for books and from TMDB and RAWG, with
  keys you enter in Settings, for the rest.
- **Recipes**: every recipe in one format, each a note under `recipes/` with
  the whole record in frontmatter. A servings slider rescales every amount and
  switches units where a clean one exists. The add box turns typed text, a
  recipe link, or photos and PDFs into a draft you review before it saves, and
  each recipe gets a small coloured ASCII drawing. Reading photos and drawing
  need an AI key; typed text and sites that publish recipe data work without.
- **Paper**: an iPad app (`ipad/`) wraps the web app and adds Apple Pencil
  notebooks, stored in the vault under `paper/` beside the notes. Handwriting
  is recognised on the device and searchable by OMNIA; the web and desktop
  apps show notebooks read-only.
- **Flashcards** with SM-2 spaced repetition, as Git-backed decks built from
  your notes by OMNIA and studied full-screen.
- Runs as an installable **PWA** (offline app shell), a **desktop app**
  (`desktop/`, Tauri 2; Windows builds come from CI) that runs the same
  frontend against your server with a local SQLite cache (edits made offline
  queue up and merge three-way on reconnect; updates are signed and come from
  the server's own `/updates` feed) and the iPad app. **Web Push** delivers
  reminders and OMNIA's messages, switched on per browser in Settings →
  Notifications; the server generates its VAPID keypair on first boot, and on
  iOS push needs the PWA installed to the home screen.
- `Cmd/Ctrl+K` opens the command palette, `Cmd/Ctrl+M` creates an untitled
  note, `Cmd/Ctrl+J` opens OMNIA (add Shift to resume the last conversation),
  and `?` lists every shortcut.
- Runs comfortably on a 1–2 GB VPS. No Postgres, no Redis, no vector-DB
  server — everything is embedded and file-based.
- **Works fully offline / AI-free.** OMNIA activates only when an API key is
  configured.

## Quick start (Docker)

```bash
git clone <this repo> omnianote && cd omnianote
cp .env.example .env        # set username, password, and a random session secret
docker compose up -d --build
```

Open `https://localhost` (self-signed) — or set `DOMAIN=notes.example.com` in
`.env` with DNS pointing at the server, and Caddy provisions real HTTPS
certificates automatically.

A fresh install boots into an empty vault with a Welcome note and default
note/daily templates.

### Enabling OMNIA (the AI overseer)

Open **Settings** in the app, pick a provider — **Anthropic (Claude)**,
**OpenAI (ChatGPT)**, or **Google (Gemini)** — paste that provider's API key,
and choose a model:

- Anthropic: Claude Opus 5.5 (recommended), Sonnet 5.5, Haiku 4.5, Fable 5.1, plus the previous generation
- OpenAI: GPT-6.1 Sol (recommended), GPT-6 Astra, GPT-6 Luna, plus the GPT-5.x models
- Google: Gemini 3.6 Flash (recommended), 3.5 Flash, 3.5 Flash-Lite, 2.5 Pro

OMNIA turns on immediately — no restart. Keys are stored server-side in
`data/ai.json` (mode 600, outside the vault git repo, never committed). All
configured providers' keys are kept, so you can switch freely — even
mid-conversation, since chat history is provider-neutral.

Alternatively set `OMNIA_ANTHROPIC_API_KEY`, `OMNIA_OPENAI_API_KEY`, or
`OMNIA_GOOGLE_API_KEY` in `.env`; a key entered in the UI overrides the
environment one.

Once OMNIA is on, every note's right-hand panel gets an **OMNIA tab** that
automatically knows which note you're viewing — "summarize this note", "tag
it", "find related notes". The sidebar's OMNIA page is the full-vault chat. Named conversations and one daily thread are persisted under
`.exscalia/omnia/` in the Git-backed vault; messages, tool traces, graphics,
partial generation state, and timestamps survive refreshes and restarts.

OMNIA can also render **single-use charts inline in the chat** — bar, line,
pie, and heatmap — for quick visualizations of note data ("chart my notes per
tag", "show a heatmap of my writing activity"). Charts are ephemeral: shown
in the conversation, never written to the vault.

OMNIA's tools have four enforced permission modes: **read**, **proposal**
(review tray only), **direct** (only for explicit requests), and **confirmed**
(deletions, overwrite restores, and bulk changes). Each successful mutation
commits only its affected paths, persists an action card in chat, and can be
reverted with Undo when no later edit conflicts. It can operate notes,
folders, tasks, Activity hours, History, Trash restoration, appearance,
timezone, redacted backup/security status, search indexes, and recurring
standalone calendar events. Proactive passes receive only read and proposal
tools. It also has a **habit store** — a "curious then settles"
learning layer: early on it asks single clarifying questions about your
patterns (do daily notes always log hours?), records the answers with
confidence scores, reinforces or decays them against observed behavior, and
eventually acts on settled judgment instead of asking. Around these it can:
keep tags clean (suggesting tags per note, detecting `#ml` vs
`#machine-learning` duplicates), find and merge near-duplicate notes (staged
as one reviewable batch with inbound-link rewrites), normalize frontmatter,
summarize any scope into cited answers or MOC notes, and produce
daily/weekly digests of open, due, and overdue work, events, and reminders.

The app also shows a read-only **Related Notes feed** beside every note
(semantic neighbors, with one-click `[[link]]` adding), and greets you **once
per day** on first open — raising due tasks and habit reminders when there's
something worth saying, or just saying good morning when there isn't.

OMNIA also keeps a **persistent memory of you** at `omnia/memory.md` — a
regular note in your vault, injected into every conversation. When it learns
a durable fact (who you are, preferences, ongoing projects, vault
conventions), `remember` writes it into that note straight away, dated and
filed under a heading, as a direct action with its own Undo card; `forget`
drops entries that turned out wrong. Nothing is staged for approval, so undo
or edit the note whenever it keeps something you'd rather it didn't. It's
git-versioned like everything else.

## OMNIA actions and review

- “Create/update/move this” runs directly with revision checks and produces a
  persistent action card with Undo.
- “Suggest/draft/preview this” stages a diff in the existing review tray.
- Deletes, overwrite restores, and multi-action batches produce a confirmation
  card tied to the current signed-in device and thread; it expires after ten
  minutes and cannot be approved by the model.
- Proactive morning/background runs are technically unable to invoke direct or
  confirmed tools.
- If a proposal becomes stale after a user edit, the review tray still requires
  an explicit override. Leaving with unreviewed proposals keeps the native
  browser warning.

Approved deletes and direct user deletes both enter the 30-day Trash. Git
history may retain content after Trash is emptied, so emptying it is not
secure erasure.

## Backup

The vault is a normal git repository inside the Docker volume
(`/data/vault`).

**Automatic off-site backup (recommended):** create an empty private repo
(GitHub/GitLab/anywhere) and a fine-grained access token scoped to it
(contents: read/write), then set in `.env`:

```
OMNIA_BACKUP_REMOTE=https://x-access-token:YOUR_TOKEN@github.com/you/vault-backup.git
OMNIA_BACKUP_INTERVAL=3600
```

and `docker compose up -d`. The app pushes the vault whenever new commits
exist, on that interval and on a clean shutdown when the push finishes within
its 20 s budget. Settings → Backup shows the last push and the last error
(token redacted), and has a "Push now" button to verify. After three failed
pushes in a row the app sends a notification (at most once a day) and the
morning briefing flags it until a push succeeds; the failure is also in the
app log.

Fine-grained GitHub tokens expire, 30 days by default: pick a 1-year (or
no) expiry when creating one and note the date. To rotate, create a new
token, replace it in `OMNIA_BACKUP_REMOTE` in `.env`, `docker compose up -d`
(the app rewrites the `backup` remote's URL on boot) and press "Push now".

To restore, follow [RESTORE.md](RESTORE.md). A clone of the backup repo brings
back the vault and none of the files listed next.

**What the vault push does not cover:** it sends `/data/vault` and nothing
else. These live elsewhere in `/data`, or beside it on the VPS, and the
push never copies them:

- `ai.json` and `media.json`: AI provider and media API keys
- `push_keys.json`: the Web Push (VAPID) keypair. Regenerating it silently
  kills every push subscription.
- `auth.sqlite3`: sessions, desktop and iPad device tokens, and the push
  subscriptions
- `omnia-alerts.json`, `omnia-task-reminders.json`,
  `omnia-confirmations.json`: OMNIA's alert, reminder and confirmation ledgers
- `proofread/`: staged proofreading suggestions
- `omnia/history/`: raw provider transcripts, kept out of the vault on purpose
- `index/metadata.db`: next to the rebuildable search tables, the proposals
  waiting in OMNIA's review tray (`pending_changes`)
- the server `.env`: the login password, the session secret that peppers
  every stored token hash, and the backup token

None of this is note content and all of it can be recreated, but losing the
volume means re-entering every key, logging every device in again and
re-subscribing push. `scripts/backup/` has two jobs for it.
`omnianote-backup.sh` runs nightly on the VPS from a systemd timer and sends
an encrypted restic snapshot of the whole volume, except the index and model
caches, plus `.env` to B2 or R2. `pull-vault-bundle.sh` runs daily on a Mac
from a launchd agent and pulls a git bundle of the vault, a copy the server
has no way to delete. Install steps are at the top of
`omnianote-backup.service` and `art.exscalia.vault-pull.plist`.

**One-off copy:**

```bash
docker compose cp app:/data/vault ./vault-backup
```

Only the derived tables under `/data/index` are disposable. Settings →
"Rebuild search index" regenerates the search tables and the LanceDB vectors
from the markdown at any time, and the habits table comes back from its mirror
in the vault. `metadata.db` also holds `pending_changes`, OMNIA's unapproved
proposals, and deleting the directory drops them; the restic job skips the
directory too. Each proposal's content stays in the OMNIA thread that proposed
it, and the app purges unreviewed ones after 60 days anyway.

## Configuration (env vars)

Every setting in `backend/app/config.py`, plus the two Caddy reads. Under
Docker the app sees only what `docker-compose.yml` passes it (the "Docker"
column): the container has no `.env` of its own. To use a "no" row there, add
it to the compose file's `environment:` list.

| Variable | Default | Docker | Purpose |
| --- | --- | --- | --- |
| `OMNIA_AUTH_USERNAME` | `owner` | yes | Web UI username |
| `OMNIA_AUTH_PASSWORD` | *(empty = auth off)* | yes | Web UI password |
| `OMNIA_SESSION_SECRET` | `change-me-in-production` | yes | Peppers the stored session and device token hashes; changing it signs every device out |
| `OMNIA_REQUIRE_AUTH` | `0` (repo) / `1` (Docker) | yes | Refuse to boot with an empty password or the default session secret; set `0` in `.env` to run the container with auth off |
| `OMNIA_TIMEZONE` | `America/New_York` | yes | IANA timezone for Daily, calendar milestones, and OMNIA day threads (a timezone picked in Settings wins) |
| `OMNIA_AI_PROVIDER` | `anthropic` | yes | `anthropic`, `openai`, or `google` (switchable in Settings) |
| `OMNIA_ANTHROPIC_API_KEY` | *(empty)* | yes | Claude key (the Settings UI can set/override this) |
| `OMNIA_ANTHROPIC_MODEL` | `claude-opus-5-5` | no | Claude model (pickable in Settings) |
| `OMNIA_OPENAI_API_KEY` | *(empty)* | yes | ChatGPT key (the Settings UI can set/override this) |
| `OMNIA_OPENAI_MODEL` | `gpt-6.1-sol` | no | ChatGPT model (pickable in Settings) |
| `OMNIA_GOOGLE_API_KEY` | *(empty)* | yes | Gemini key (the Settings UI can set/override this) |
| `OMNIA_GOOGLE_MODEL` | `gemini-3.6-flash` | no | Gemini model (pickable in Settings) |
| `OMNIA_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | yes | fastembed model name |
| `OMNIA_EMBEDDING_BACKEND` | `fastembed` | no | `none` disables semantic search |
| `OMNIA_BACKUP_REMOTE` | *(empty = no push)* | yes | Git remote the vault is pushed to, token in the URL (see Backup) |
| `OMNIA_BACKUP_INTERVAL` | `3600` | yes | Seconds between backup pushes (at least 60) |
| `OMNIA_VAPID_PUBLIC_KEY` | *(empty = generated)* | yes | Web Push public key, base64url. Leave both keys unset and the first boot writes a pair to `/data/push_keys.json`; set both only to pin an existing pair |
| `OMNIA_VAPID_PRIVATE_KEY` | *(empty = generated)* | yes | Web Push private key, base64url |
| `OMNIA_VAPID_SUBJECT` | `mailto:owner@exscalia.art` | yes | Contact URI sent to the push services |
| `OMNIA_DATA_DIR` | `./data` (repo) / `/data` (Docker) | image | Vault + index location |
| `OMNIA_FRONTEND_DIR` | `./frontend/build` / `/app/frontend-build` (Docker) | image | Built SPA that FastAPI serves |
| `OMNIA_UPDATES_DIR` | `./updates` / `/updates` (Docker) | fixed | Desktop update feed, served unauthenticated at `/updates/*`; compose mounts the host's `./updates` read-only |
| `OMNIA_GIT_SHA` | `dev` | image | Commit the image was built from (the `GIT_SHA` build arg deploy.sh passes); `/api/health` reports it as `build` |
| `OMNIA_GIT_AUTHOR_NAME` | `user` | no | Author of your own autosave commits |
| `OMNIA_GIT_AUTHOR_EMAIL` | `user@exscalia.local` | no | Email of that author |
| `OMNIA_AUTOCOMMIT_DEBOUNCE` | `10` | no | Seconds of quiet before autosave commits |
| `DOMAIN` | `localhost` | Caddy | Domain Caddy serves and gets certificates for |
| `REDIRECT_DOMAIN` | `redirect.localhost` (inert) | Caddy | Extra domain, e.g. the bare apex, permanently redirected to `DOMAIN`; needs its own DNS record |

Active logins are stored in `/data/auth.sqlite3` so individual devices can be
revoked from Settings. On login, a public client IP is sent once to
`ipwho.is` to obtain the approximate city/region shown in that device list;
VPNs may show their exit location instead.

## Development

```bash
# backend (Python 3.12, uv)
cd backend && uv sync --extra dev
uv run uvicorn app.main:app --port 8600 --reload

# frontend (SvelteKit; dev server proxies /api to :8600)
cd frontend && npm install && npm run dev

# tests (offline — includes a scripted fake AI provider)
cd backend && uv run pytest
```

## Architecture, briefly

```
backend/app/
  vault/     git-backed store: frontmatter, note CRUD, templates, auto-commit,
             the off-site backup push
  index/     SQLite (notes, links, tags, lifecycle tasks, media, FTS5) + LanceDB
             index; paper_index.py indexes Paper pages and serves their change feed
  overseer/  provider abstraction (Anthropic/OpenAI/Google), tool-calling agent
             loop; persona.py is the single source of truth for OMNIA's voice
             plus action/confirmation coordination and recurring-event tools
  pending/   staged AI changes: overlay reads, stale detection, approve/discard
  api/       notes, recovery/Trash, tasks/calendar, search, OMNIA, pending, auth,
             attachments, live (SSE change stream), paper (the iPad app's
             contract), media, recipes, flashcards, push
  paper_store.py     Paper notebooks as package folders in the vault
  media_*.py         Media tracker: items parsed from note frontmatter and logs,
                     metadata and cover lookup, the backdrop job
  recipes.py         Recipes: the note format and the service layer the API
                     and OMNIA share; recipe_parse/import/ai/art.py read text,
                     websites, photos and PDFs and draw the dish
  push_service.py    Web Push (VAPID) delivery; alerts.py times the reminders
  flashcards.py      decks with SM-2 scheduling
frontend/    SvelteKit SPA (CodeMirror 6 editor, tasks, recovery, calendar, chat,
             Paper viewer, media, recipes, study); also the desktop app's UI, whose
             offline cache and sync engine live in src/lib/sync/
desktop/     Tauri 2 shell: keychain token, signed auto-updates (desktop/README.md)
ipad/        native iPad app: web shell plus Paper (PencilKit), with its models,
             API client and store in the PaperCore package (ipad/README.md)
scripts/     backup/ (restic snapshot and vault bundle jobs), refresh-mirror.sh
```

The frontend builds to static files served by FastAPI — a single Python
process at runtime, with Caddy in front for HTTPS.
