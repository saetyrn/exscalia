# EXSCALIA Desktop

Tauri 2 shell around the SvelteKit frontend in `../frontend`, talking to a
remote EXSCALIA server (default `https://notes.exscalia.art`) with full
offline support. One frontend build serves both the website and this app —
desktop behavior is switched on at runtime, so the web deploy is untouched.

## How it works

- **Auth**: login with `create_token` returns a bearer token instead of the
  `__Host-` cookie; it lives in the OS keychain (`keyring` crate via the
  `secret_get/set/delete` commands in `src-tauri/src/lib.rs`). Desktop
  sessions appear in Settings → Sessions and can be revoked there.
- **Offline**: `frontend/src/lib/sync/` — SQLite cache + op queue
  (tauri-plugin-sql in the app data dir: `exscalia.db` for the default
  server, `exscalia-<hash>.db` for any other), an interceptor over
  the `api` object, and a sync engine that replays queued work and merges
  three-way on reconnect. Same-text conflicts become server-side
  `(conflict …)` copies, surfaced in-app.
- **Server URL**: stored in `settings.json` (tauri-plugin-store) in the app
  data dir; editable on the login screen.

## Build (macOS)

```sh
npm install
npx tauri build          # release .app + .dmg
npx tauri build --debug  # faster, for testing
npx tauri dev            # live dev against the Vite dev server
```

Output: `src-tauri/target/release/bundle/` (`macos/EXSCALIA.app`,
`dmg/EXSCALIA_<version>_aarch64.dmg`).

Unsigned builds: first launch needs right-click → Open (Gatekeeper), or
`xattr -dr com.apple.quarantine EXSCALIA.app`. To sign later, set
`bundle.macOS.signingIdentity` in `tauri.conf.json` and notarize with
`notarytool`.

## Build (Windows)

Options, in order of preference:

1. **GitHub Actions** (once this repo has a remote): `tauri-apps/tauri-action`
   with a `macos-latest` + `windows-latest` matrix builds both installers and
   attaches them to a release. This is the endgame — no Windows machine
   needed.
2. **Any Windows machine/VM**: install Rust (rustup) + NSIS, then
   `npm install && npx tauri build` in this directory → NSIS `.exe` installer
   (WebView2 bootstrapper included automatically).
3. **Cross-compile from macOS** (experimental): `cargo install cargo-xwin`,
   `rustup target add x86_64-pc-windows-msvc`, then
   `npx tauri build --runner cargo-xwin --target x86_64-pc-windows-msvc`.
   Works for NSIS bundles but is not officially supported — verify on a real
   Windows machine before relying on it.

Unsigned Windows builds trigger the SmartScreen "unrecognized app" warning
(More info → Run anyway). An OV/EV certificate or Azure Trusted Signing
removes it; not worth it for personal use.

## Auto-updates

The app checks `https://notes.exscalia.art/updates/latest.json` once per
launch (after login) and offers to install newer builds. Artifacts are
signed; the public key is in `tauri.conf.json`, the private key lives only
at `~/.tauri/exscalia.key` on this Mac (never in CI) — losing it means shipped
apps can never update again, so back it up.

Release flow:

```sh
# 1. bump "version" in src-tauri/tauri.conf.json, commit, tag v<version>
# 2. build + sign locally from the clean tagged tree (or download CI
#    artifacts into win-artifacts/)
TAURI_SIGNING_PRIVATE_KEY="$(cat ~/.tauri/exscalia.key)" npx tauri build
# 3. publish the feed (--dry-run stages and validates without uploading)
./publish-update.sh user@server [--notes "..."]
```

The build stamps its commit (`build-frontend.sh` →
`src-tauri/target/build-sha`, shown in Settings as `Build <sha>`). The
publish script refuses a macOS tarball whose Info.plist is not `<version>`
or whose stamp is `-dirty` or not the commit of `v<version>`, uploads
`latest.json` last via an atomic rename, then fetches the feed and checks
every artifact URL.

`.github/workflows/desktop.yml` builds the Windows installer, unsigned, on
every `v*` tag as a workflow artifact; download the `-setup.exe` into
`win-artifacts/` and `publish-update.sh` signs it locally to include Windows
in the feed.

## Deferred (not in v1)

- Notifications: `tauri-plugin-notification` for OMNIA reminders.
