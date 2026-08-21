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
- `Cmd/Ctrl+K` opens the command palette; `Ctrl+M` creates an untitled note.
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

- Anthropic: Claude Opus 4.8 (recommended), Sonnet 5, Haiku 4.5, Fable 5
- OpenAI: GPT-5.6 Sol (recommended), GPT-5.6 Terra, GPT-5.6 Luna, GPT-5.4
- Google: Gemini 3.6 Flash (recommended), 3.5 Flash, 3.5 Flash-Lite, 2.5 Pro

OMNIA turns on immediately — no restart. Keys are stored server-side in
`data/ai.json` (mode 600, outside the vault git repo, never committed). All
configured providers' keys are kept, so you can switch freely — even
mid-conversation, since chat history is provider-neutral.

Alternatively set `OMNIA_ANTHROPIC_API_KEY`, `OMNIA_OPENAI_API_KEY`, or
`OMNIA_GOOGLE_API_KEY` in `.env`; a key entered in the UI overrides the
environment one.

Once OMNIA is on, every note gets a collapsible **OMNIA chat drawer** on its
right edge that automatically knows which note you're viewing — "summarize
this note", "tag it", "find related notes". The sidebar's OMNIA page is the
full-vault chat. Named conversations and one daily thread are persisted under
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
regular note in your vault, injected into every conversation. As it learns
durable facts (who you are, preferences, ongoing projects, vault
conventions), it stages updates to that note through the same review flow as
any other edit, so nothing is remembered without your approval — and you can
edit the memory yourself at any time. It's git-versioned like everything
else.

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
exist, on that interval and on clean shutdown. Settings → Backup shows the
last push (token redacted) and has a "Push now" button to verify. Restoring
is just `git clone` of the backup repo.

**One-off copy:**

```bash
docker compose cp app:/data/vault ./vault-backup
```

The SQLite + LanceDB indexes under `/data/index` are disposable caches —
Settings → "Rebuild search index" regenerates them from the markdown at any
time.

## Configuration (env vars)

| Variable | Default | Purpose |
| --- | --- | --- |
| `OMNIA_AUTH_USERNAME` | `owner` | Web UI username |
| `OMNIA_AUTH_PASSWORD` | *(empty = auth off)* | Web UI password |
| `OMNIA_SESSION_SECRET` | `change-me-in-production` | Key used to protect server-side session tokens |
| `OMNIA_TIMEZONE` | `America/New_York` | IANA timezone for Daily, calendar milestones, and OMNIA day threads |
| `OMNIA_AI_PROVIDER` | `anthropic` | `anthropic`, `openai`, or `google` (switchable in Settings) |
| `OMNIA_ANTHROPIC_API_KEY` | *(empty)* | Claude key (the Settings UI can set/override this) |
| `OMNIA_ANTHROPIC_MODEL` | `claude-opus-4-8` | Claude model (pickable in Settings) |
| `OMNIA_OPENAI_API_KEY` | *(empty)* | ChatGPT key (the Settings UI can set/override this) |
| `OMNIA_OPENAI_MODEL` | `gpt-5.6-sol` | ChatGPT model (pickable in Settings) |
| `OMNIA_GOOGLE_API_KEY` | *(empty)* | Gemini key (the Settings UI can set/override this) |
| `OMNIA_GOOGLE_MODEL` | `gemini-3.6-flash` | Gemini model (pickable in Settings) |
| `OMNIA_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model name |
| `OMNIA_EMBEDDING_BACKEND` | `fastembed` | `none` disables semantic search |
| `OMNIA_DATA_DIR` | `./data` (repo) / `/data` (Docker) | Vault + index location |
| `OMNIA_AUTOCOMMIT_DEBOUNCE` | `10` | Seconds of quiet before autosave commits |

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
  vault/     git-backed store: frontmatter, note CRUD, templates, auto-commit
  index/     SQLite (notes, links, tags, lifecycle tasks, FTS5) + LanceDB index
  overseer/  provider abstraction (Anthropic/OpenAI/Google), tool-calling agent
             loop; persona.py is the single source of truth for OMNIA's voice
             plus action/confirmation coordination and recurring-event tools
  pending/   staged AI changes: overlay reads, stale detection, approve/discard
  api/       notes, recovery/Trash, tasks/calendar, search, OMNIA, pending, auth
frontend/    SvelteKit SPA (CodeMirror 6 editor, tasks, recovery, calendar, chat)
```

The frontend builds to static files served by FastAPI — a single Python
process at runtime, with Caddy in front for HTTPS.
