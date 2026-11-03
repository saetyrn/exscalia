# Restoring EXSCALIA

Use this when the server's data volume is gone or damaged, or when moving to a
new server without the old one. Nobody has run it end to end yet, so read it
through once before starting, and do the steps in order. The order matters.
Clients stay offline until the server has been checked, and the vault goes
into the volume before the app ever boots.

If only search is broken, you do not need any of this. Run Settings →
"Rebuild search index" instead; see the note at the end of the Backup section
in [README.md](README.md).

Commands marked "server" run as root in `~/omnianote` on the server. `SERVER`
and `DOMAIN` stand for your ssh target and your domain.

## What a backup can hold

| Source | Holds | How often |
| --- | --- | --- |
| Backup remote (`OMNIA_BACKUP_REMOTE`) | the vault | every `OMNIA_BACKUP_INTERVAL` seconds (hourly by default) when there are new commits, and on a clean shutdown when the push finishes within its 20 s budget |
| Mac bundle (`scripts/backup/pull-vault-bundle.sh`) | the vault | daily, into `$DEST` |
| restic snapshot (`scripts/backup/omnianote-backup.sh`) | all of `/data` except `/data/index` and the model caches, plus `.env` | nightly |

The vault is a git repository. It carries the notes, attachments, Paper
notebooks, flashcard decks, calendar events, OMNIA's readable threads and
memory, and the app's settings under `.exscalia/`. The README's Backup section
lists what lives outside it.

## 0. Freeze the clients

Every client may hold edits the backup does not have, and each one replays
them against whatever the server has as soon as it reconnects.

- Quit the desktop app. Copy its cache before anything else:
  `cp ~/Library/Application\ Support/art.exscalia.desktop/exscalia.db* ~/exscalia-desktop-copy/`
  (make the folder first). The queued offline edits are in there.
- Put the iPad in airplane mode. Download the app's container through Xcode
  (Window → Devices and Simulators → the iPad → EXSCALIA → Download Container);
  a cable works while the iPad stays offline.
- Close every browser tab of the app, and do not clear the site's data: unsaved
  drafts sit in its IndexedDB.

## 1. Find the freshest backup

Compare the newest commit of each source. Do not trust the backup remote just
because it is configured. Its hourly push once failed every hour for weeks
(from 2026-08-27) before anyone looked, and the only signs were the app log
and Settings → Backup.

```bash
# backup remote: paste its URL from the password manager (it carries the
# token, so read keeps it out of your shell history)
read -rs url
git clone --bare --quiet "$url" /tmp/vault-remote
git -C /tmp/vault-remote log -1 --format='%ci %h %s' main

# newest Mac bundle
b="$(ls -t "$DEST"/vault-*.bundle | head -1)"
git clone --bare --quiet "$b" /tmp/vault-bundle
git -C /tmp/vault-bundle log -1 --format='%ci %h %s' main

# restic (server; on a new server this needs the files from step 2)
docker run --rm -v /etc/omnianote-restic.pass:/etc/omnianote-restic.pass:ro \
  --env-file /etc/omnianote-backup.env restic/restic snapshots --host omnianote
```

If the old server still answers, Settings → Backup shows its last successful
push and the last error. Pick the newest source for the vault. It can differ
from the source of everything else.

## 2. Server, code and .env

Same server: stop the app and keep a copy of the old volume until the restore
has been checked (skip the copy if the volume is unreadable). The container
runs as uid 10001 and cannot write to the server's directory, so tar writes to
stdout.

```bash
# server
docker compose stop app
docker compose run --rm --no-deps -T --entrypoint tar app \
  -czf - -C /data . > old-data-$(date +%F).tgz
docker compose rm -sf app
docker volume rm omnianote_omnianote-data
```

New server: point `DOMAIN`'s DNS at it, then run
`./deploy.sh root@SERVER DOMAIN` from the repo, without `--with-vault`. That
ships the code and installs Docker, and it also boots an empty vault. Throw that
away before going on:

```bash
# server
docker compose down
docker volume rm omnianote_omnianote-data
```

`.env` must be the old one. It holds the login password, the backup token, any
pinned `OMNIA_VAPID_*` keys, and `OMNIA_SESSION_SECRET`, which peppers every
stored token hash. With a new secret every device has to log in again, even
with `auth.sqlite3` restored. On the same server it is still there. On a new
server, paste it from the password manager (`umask 077; cat > .env`), or take
it from the latest restic snapshot. restic needs `/etc/omnianote-backup.env`
and `/etc/omnianote-restic.pass` from the password manager first, laid out as
in `scripts/backup/omnianote-backup.service`. Skip its `restic init`, since the
repository already exists.

```bash
# server
docker run --rm -v "$PWD/restic-env:/out" \
  -v /etc/omnianote-restic.pass:/etc/omnianote-restic.pass:ro \
  --env-file /etc/omnianote-backup.env \
  restic/restic restore latest --host omnianote --target /out --include /backup-env
install -m 600 restic-env/backup-env/.env .env && rm -rf restic-env
```

## 3. Seed the vault before the first boot

Never copy into a running app. On an empty volume the app creates a vault,
seeds `Welcome.md` and the templates, and commits them. Copied over that, any
of them the restored vault had deleted come back, and the next boot commits
them into its history.

Create the volume without starting anything:

```bash
# server
docker compose up --no-start app
```

Then fill `/data/vault` from the source step 1 picked.

From the backup remote. `--origin backup` names the remote the way the app
does, so it reuses it rather than leaving a second copy of the token under
`origin`. The single quotes keep the URL out of your shell history: it expands
inside the container, from `.env`.

```bash
# server
docker compose run --rm --no-deps -T --entrypoint sh app \
  -c 'git clone --origin backup "$OMNIA_BACKUP_REMOTE" /data/vault'
```

From a Mac bundle:

```bash
# on the Mac
git clone --branch main "$b" vault-import && git -C vault-import remote remove origin
rsync -az vault-import/ root@SERVER:omnianote/vault-import/
# server
docker compose run --rm --no-deps -T -v "$PWD/vault-import:/src:ro" --entrypoint sh app \
  -c 'mkdir -p /data/vault && cp -a /src/. /data/vault/'
rm -rf vault-import
```

From restic only: skip this step, and drop `--exclude /data/vault` in step 4.

Check that the newest commit is the one step 1 found:

```bash
# server
docker compose run --rm --no-deps -T --entrypoint git app -C /data/vault log -1 --format='%ci %h %s'
```

## 4. The rest of /data

With a restic snapshot, restore everything but the vault into the volume, hand
it to the app's uid 10001 (restic restores the owners it recorded, and
snapshots from before the app ran as 10001 recorded root), then put the auth
database in place. The snapshot holds a consistent copy under
`backup-staging/`, not the live file.

```bash
# server
docker run --rm -v omnianote_omnianote-data:/data \
  -v /etc/omnianote-restic.pass:/etc/omnianote-restic.pass:ro \
  --env-file /etc/omnianote-backup.env \
  restic/restic restore latest --host omnianote --target / \
  --exclude /data/vault --exclude /backup-env
docker run --rm --user 0 -v omnianote_omnianote-data:/data --entrypoint chown \
  omnianote-app:latest -R 10001:10001 /data
docker compose run --rm --no-deps -T --entrypoint sh app \
  -c 'mv /data/backup-staging/auth.sqlite3 /data/auth.sqlite3'
```

Without one, expect this after the boot:

- Every device has to log in again (`auth.sqlite3`).
- The AI provider keys and settings have to be entered again in Settings
  (`ai.json`) unless `.env` sets the keys, and so do the media lookup keys
  (`media.json`).
- Push has to be turned on again on every device. The first boot generates a
  new VAPID keypair unless `.env` pins `OMNIA_VAPID_*`, and the old
  subscriptions die with the old key.
- OMNIA keeps the readable threads but loses their raw provider history
  (`omnia/history/`). That history is kept out of the vault on purpose.
- Old task reminders may fire again (`omnia-task-reminders.json` records which
  went out). Open alerts, confirmation cards and staged proofreading
  suggestions are gone.

Either way, proposals waiting in the review tray are gone: they live in
`/data/index/metadata.db`, which neither backup copies. Each one's content is
still in the OMNIA thread that proposed it.

## 5. Boot and reindex

```bash
# server
docker compose up -d
docker compose logs -f app
```

Wait for `vault ready`. The log should show `indexed N note(s)` with N close to
your note count, `indexed N Paper page(s)` if you use Paper, and
`backup remote configured`. `Seeded new vault` means the app found no vault and
made a new one: stop the app, and go back to step 2. The first boot also
downloads the embedding model again, which can take a few minutes.

Then log in on the web from a private window, which holds no drafts, and run
Settings → "Rebuild search index".

## 6. Check before any client reconnects

```bash
# server
docker compose exec -T app sh -c 'cd /data/vault &&
  git ls-files "*.md" | wc -l && git ls-files "*notebook.json" | wc -l'
```

The first number is the note count (including templates and media items), the
second the Paper notebook count. Compare them with the source clone from step
1, for example `git -C /tmp/vault-remote ls-tree -r --name-only main | grep -c '\.md$'`.

Then Settings → Backup → "Push now". The push is a plain fast-forward and is
never forced. If the remote rejects it, the remote has commits this vault
lacks, so the vault came from an older source than the remote. Stop, and
compare the two histories (`/tmp/vault-remote` has the remote's) before going
on.

## 7. Reconnect the clients one at a time

Web first: open the app in your usual browser, and review any local draft the
editor offers to restore. Then the desktop app, then the iPad. The desktop app
replays its queued edits and merges them three-way. The iPad uploads its Paper
journal, and a conflict keeps both halves. How the desktop app behaves against
a server whose history went back in time has never been tested, which is what
the copy from step 0 is for. Before launching the app, you can open that copy
with `sqlite3` and compare what it holds with the server. Treat that as a
precaution, not a checked procedure.

When everything matches, delete `old-data-*.tgz` and the `/tmp/vault-*`
clones.
