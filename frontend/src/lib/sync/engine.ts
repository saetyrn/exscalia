// Desktop only: the sync engine. Owns the live SSE connection, the local
// cache, and the replay of offline work. States:
//
//   offline → connecting → replaying → live
//
// On every (re)connect it first replays queued structural ops, then pushes
// dirty notes (three-way merging when the server moved underneath them), then
// diffs the server manifest against the cache — so by the time it reports
// `live`, the cache and the vault agree.

import { ApiError, apiFetch, type Note, type NoteListItem } from '../api';
import { connectLive, type LiveNoteEvent } from '../live';
import { mergeThreeWay } from '../merge';
import { syncState } from '../stores.svelte';
import { notify } from '../ui.svelte';
import * as db from './db';
// Circular at module level (offline-api imports this module), but both sides
// only touch each other's bindings at runtime, so the cycle is benign. The
// engine must use the raw network methods — the wrapped ones fall back to the
// cache, which would let a network failure mid-replay masquerade as a server
// answer.
import { netApi as api } from './offline-api';

export type EngineMode = 'offline' | 'connecting' | 'replaying' | 'live';

let mode: EngineMode = 'offline';
let started = false;
let replaying = false;
/** A reconnect or a retry arrived while a pass was out: run another after it. */
let replayRequested = false;
/** The SSE stream said `ready` and has not dropped since. Tracked apart from
 * `mode`, which a failed replay can leave 'offline' over a healthy stream. */
let sseLive = false;
let startOptions: StartOptions = {};
/** Reconnects the stream now, skipping its backoff; set while started. */
let kickLive: (() => void) | null = null;
const buffered: LiveNoteEvent[] = [];
/** The replay or single-note push currently talking to the server. Pushes
 * never overlap: two of the same row would each merge from the same base. */
let inFlight: Promise<void> | null = null;

export function engineMode(): EngineMode {
	return mode;
}

export function isOffline(): boolean {
	return mode === 'offline' || mode === 'connecting';
}

export async function computeRevision(content: string): Promise<string> {
	const bytes = new TextEncoder().encode(content);
	const digest = await crypto.subtle.digest('SHA-256', bytes);
	return Array.from(new Uint8Array(digest))
		.map((b) => b.toString(16).padStart(2, '0'))
		.join('');
}

function setMode(next: EngineMode) {
	mode = next;
	syncState.mode = next;
	window.dispatchEvent(new CustomEvent('exscalia:sync-mode', { detail: next }));
}

export async function refreshPendingBadge(): Promise<void> {
	try {
		syncState.pending = await db.pendingCount();
	} catch {
		// Badge only; never let it break a save.
	}
}

/** Tell the rest of the app (sidebar, open editor) that a note changed
 * locally-visible state — same contract as a server SSE event. */
function emitNoteEvent(event: LiveNoteEvent) {
	window.dispatchEvent(new CustomEvent('exscalia:live', { detail: event }));
}

// --- applying remote changes to the cache -----------------------------------

async function pullNote(path: string): Promise<Note | null> {
	try {
		return await api.readNote(path);
	} catch (err) {
		// Gone (deleted or renamed since the event) is a real answer; a
		// connection failure must abort the pass instead of being mistaken
		// for one.
		if (isNetworkError(err)) throw err;
		return null;
	}
}

async function applyRemoteNote(path: string, pinned?: boolean): Promise<void> {
	const note = await pullNote(path);
	if (!note) return;
	const row = await db.getNote(path);
	if (row && (row.sync_status === 'dirty' || row.sync_status === 'new')) {
		// The server changed a note we also changed. Merge; a clean result is
		// adopted as new local content on top of the server version. A
		// conflicted one keeps its old base: the push must go out with that
		// stale revision so the server answers 'stale' and ours is parked as a
		// conflict copy. Moving the base here would turn that push into an
		// ordinary save over the remote edits.
		const merged = mergeThreeWay(row.base_content, row.content, note.content);
		if (merged.ok) {
			await db.setBase(path, note.content, note.revision);
			await db.writeDirty(path, merged.text, await computeRevision(merged.text));
			// Still pending, and no reconnect is coming to push it.
			schedulePush();
		}
		return;
	}
	await db.adoptServer(path, note.content, note.revision, note.meta, pinned);
}

async function handleLiveEvent(event: LiveNoteEvent): Promise<void> {
	// Paper pages are never cached offline (the viewer is network-only, like
	// media); the event is still re-broadcast so an open /paper view refetches.
	if (event.type === 'paper') return;
	if (event.type === 'note') {
		await applyRemoteNote(event.path);
	} else if (event.type === 'note_moved') {
		// A rename, not a deletion: the cached row follows the note. Local
		// edits under the old path stay put, as they do for a removal.
		const row = await db.getNote(event.path);
		if (!row || (row.sync_status !== 'dirty' && row.sync_status !== 'new')) {
			await db.removeNote(event.path);
		}
		if (event.new_path) await applyRemoteNote(event.new_path);
	} else {
		const row = await db.getNote(event.path);
		if (row && (row.sync_status === 'dirty' || row.sync_status === 'new')) return;
		await db.removeNote(event.path);
	}
}

// --- replay: queued ops, then dirty notes, then manifest ---------------------

const MAX_OP_ATTEMPTS = 3;

class Unreachable extends Error {}

/** A lost connection: fetch's TypeError ('Load failed' on desktop, which is
 * also how a server 500 arrives there, HANDOFF §7), offline-api's
 * ApiError(0), or a pass already cut short by one. Anything else is an answer
 * or a local fault (an HTTP 4xx/5xx, a SQLite error, which tauri-plugin-sql
 * rejects with a bare string, a bad JSON body) and must not pass for being
 * offline. */
export function isNetworkError(err: unknown): boolean {
	return err instanceof Unreachable || err instanceof TypeError || statusOf(err) === 0;
}

function statusOf(err: unknown): number | undefined {
	return (err as { status?: number } | null)?.status;
}

function errorText(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}

/** Failures recorded since startup; a pass that adds none is clean. */
let failures = 0;

/** Keep a failure that is not a lost connection where Settings → Sync shows
 * it, and its trace in sync_meta. */
async function recordFailure(context: string, err: unknown): Promise<void> {
	failures += 1;
	syncState.lastError = `${context}: ${errorText(err)}`;
	await db.debugNote(`${context}: ${(err as Error)?.stack ?? String(err)}`).catch(() => {});
}

async function replayOps(): Promise<void> {
	for (const op of await db.listOps()) {
		try {
			await replayOneOp(op);
			await db.deleteOp(op.id);
		} catch (err) {
			if (isNetworkError(err)) throw new Unreachable();
			// Only a 4xx says the operation itself is bad, so only a 4xx
			// counts: after a few the op is dropped (the note rows are intact
			// and re-push). A 5xx or a cache failure is not the op's fault;
			// it is recorded and the op stays queued.
			const status = statusOf(err) ?? 0;
			const refused = status >= 400 && status < 500;
			const attempts = await db.recordOpFailure(op.id, errorText(err), refused);
			await recordFailure(`${op.op} ${op.path}`, err);
			if (attempts >= MAX_OP_ATTEMPTS) await db.deleteOp(op.id);
		}
	}
}

async function replayOneOp(op: db.OpRow): Promise<void> {
	const payload = op.payload_json ? JSON.parse(op.payload_json) : {};
	switch (op.op) {
		case 'create': {
			// The row itself carries the content; push happens in the dirty pass.
			// Here we only guarantee the note exists server-side.
			const row = await db.getNote(op.path);
			if (!row || row.sync_status !== 'new') return;
			try {
				const created = await api.createNote(op.path, row.content);
				await adoptPushed(row, row.content, created);
			} catch (err) {
				if (statusOf(err) === 409) {
					// Already exists (made on another device too). Treat our copy
					// as an edit of it: merge against an empty base in the dirty
					// pass.
					await db.getNote(op.path).then(async (r) => {
						if (r) await db.writeDirty(op.path, r.content, r.revision ?? '');
					});
					return;
				}
				throw err;
			}
			break;
		}
		case 'delete':
			try {
				await api.deleteNote(op.path);
			} catch (err) {
				if ((err as { status?: number }).status !== 404) throw err;
			}
			await db.removeNote(op.path);
			break;
		case 'rename':
			try {
				await api.renameNote(op.path, op.new_path!, payload.new_title);
			} catch (err) {
				const status = (err as { status?: number }).status;
				if (status === 404) {
					// Source vanished remotely; the local row (already at the new
					// path) re-creates on push.
					const row = await db.getNote(op.new_path!);
					if (row) await db.insertNew(op.new_path!, row.content, row.revision ?? '');
					return;
				}
				throw err;
			}
			break;
		case 'pin':
			await api.pinNote(op.path, Boolean(payload.pinned));
			// Pinning rewrites frontmatter server-side — refetch so revisions line up.
			await applyRemoteNote(op.path, Boolean(payload.pinned));
			break;
		case 'create_folder':
			await api.createFolder(op.path);
			break;
		case 'rename_folder':
			await api.renameFolder(op.path, op.new_path!);
			break;
		case 'delete_folder':
			await api.deleteFolder(op.path);
			break;
	}
}

const MAX_PUSH_ROUNDS = 3;

/** Paths already reported as refused since the last replay, so the periodic
 * push between reconnects does not repeat the toast every minute. */
const refusedReported = new Set<string>();

async function pushDirtyNotes(): Promise<void> {
	for (const row of await db.pendingNotes()) {
		try {
			await pushOne(row);
		} catch (err) {
			if (isNetworkError(err)) {
				const trace = (err as Error)?.stack ?? String(err);
				await db.debugNote(`push ${row.path}: ${trace}`).catch(() => {});
				throw new Unreachable();
			}
			// A server error or a cache failure on one note should not strand
			// the rest.
			await recordFailure(`push ${row.path}`, err);
		}
	}
}

/** Base revision for pushing onto a server note this device never had (a
 * create that collided). It can never equal a sha256 hex, so the server
 * always answers 'stale' with its content and the loop merges from empty. A
 * null base would skip the server's staleness check and overwrite. */
const NEVER_MATCHES = 'new-collision';

/** Adopt what a push of `row` left on the server, unless the row was written
 * while the push was out: that text is not in the answer, so it stays
 * pending. When the push sent the row's text unmerged, the server copy is
 * that text and becomes the base the newer text is an edit of, so the next
 * push is an ordinary save. After a merge the old base stays and the next
 * push merges again. False when the row was left pending. */
async function adoptPushed(
	row: db.NoteRow,
	sent: string,
	answer: Pick<Note, 'content' | 'revision' | 'meta'>
): Promise<boolean> {
	const { path } = row;
	const at = row.updated_at ?? 0;
	if (await db.adoptServer(path, answer.content, answer.revision, answer.meta, undefined, at)) {
		return true;
	}
	if (sent === row.content) {
		await db.rebasePending(path, answer.content, answer.revision, row.base_revision);
	}
	schedulePush();
	return false;
}

/** Create the note server-side and adopt the result. Null once created; the
 * server's 409 when the path is already taken, and the caller pushes onto the
 * server copy instead. */
async function createRemote(row: db.NoteRow, content: string): Promise<unknown> {
	try {
		const created = await api.createNote(row.path, content);
		if (await adoptPushed(row, content, created)) {
			emitNoteEvent({ type: 'note', path: row.path, revision: created.revision });
		}
		return null;
	} catch (err) {
		if (statusOf(err) === 409) return err;
		throw err;
	}
}

/** The server will not take this note (a bad path, a name it refuses), so a
 * retry can never land. Keep the row and say so on every pass. */
async function reportRefused(row: db.NoteRow, err: unknown): Promise<void> {
	const detail = (err as Error).message;
	if (!refusedReported.has(row.path)) {
		refusedReported.add(row.path);
		notify(`Could not sync “${row.name}”: ${detail}`, { tone: 'error' });
	}
	await recordFailure(`push ${row.path}`, err);
}

async function pushOne(row: db.NoteRow): Promise<void> {
	let content = row.content;
	let baseRevision = row.base_revision;
	let baseContent = row.base_content;
	/** A create in this push found the path taken. */
	let taken: unknown = null;

	try {
		if (row.sync_status === 'new') {
			taken = await createRemote(row, content);
			if (!taken) return;
			// Exists remotely (made on another device too): push onto it.
			baseRevision = NEVER_MATCHES;
			baseContent = '';
		}

		for (let round = 0; round < MAX_PUSH_ROUNDS; round++) {
			let result: Note;
			try {
				result = await api.saveNote(row.path, content, baseRevision, 'reject');
			} catch (err) {
				if (statusOf(err) !== 404) throw err;
				// Deleted or renamed on another device while we held edits:
				// re-create it. Server trash still has the remote copy, so
				// keeping the user's text is the safe bias.
				const refused = await createRemote(row, content);
				if (!refused) return;
				// Not there to save, taken to create, and again after that: a
				// sibling differing only in case or Unicode form holds the
				// name (vault/notes.py _refuse_sibling).
				if (taken) {
					await reportRefused(row, refused);
					return;
				}
				taken = refused;
				baseRevision = NEVER_MATCHES;
				baseContent = '';
				continue;
			}
			if (result.status !== 'stale') {
				if (await adoptPushed(row, content, result)) {
					emitNoteEvent({ type: 'note', path: row.path, revision: result.revision });
				}
				return;
			}
			const merged = mergeThreeWay(baseContent, content, result.content);
			if (merged.ok) {
				baseContent = result.content;
				baseRevision = result.revision;
				content = merged.text;
				continue;
			}
			// Both sides changed the same text. The server version wins in place;
			// ours is parked as a conflict copy right next to it. The save MUST go
			// out with the stale base revision — a refreshed one would make it a
			// perfectly ordinary save that silently overwrites the server version.
			const parked = await api.saveNote(row.path, content, baseRevision, 'copy');
			// Text written while this was out stays pending on the old base,
			// never on theirs: the next push parks it as well.
			await db.adoptServer(
				row.path,
				parked.content,
				parked.revision,
				parked.meta,
				undefined,
				row.updated_at ?? 0
			);
			if (parked.conflict_path) {
				await db.recordConflict(row.path, parked.conflict_path);
				await applyRemoteNote(parked.conflict_path);
				window.dispatchEvent(
					new CustomEvent('exscalia:sync-conflict', {
						detail: { path: row.path, conflictPath: parked.conflict_path }
					})
				);
			}
			emitNoteEvent({ type: 'note', path: row.path, revision: parked.revision });
			return;
		}
		// Three stale rounds means the note is hot on another device — the next
		// replay (or its own live event) will pick it up. Until then Settings
		// says why the row is still pending.
		await recordFailure(
			`push ${row.path}`,
			new Error(`changed on the server during each of ${MAX_PUSH_ROUNDS} merges`)
		);
	} catch (err) {
		// The server refuses the note itself (e.g. a path without .md).
		if (statusOf(err) !== 400) throw err;
		await reportRefused(row, err);
	}
}

interface Manifest {
	notes: (NoteListItem & { revision: string })[];
	folders: string[];
}

async function pullManifest(): Promise<void> {
	const res = await apiFetch('/api/notes/manifest');
	if (res.status === 401) startOptions.onAuthExpired?.();
	// With a status, so a server error is not mistaken for a lost connection.
	if (!res.ok) throw new ApiError(res.status, `manifest ${res.status}`);
	const manifest = (await res.json()) as Manifest;

	const local = new Map<string, db.NoteHead>();
	for (const row of await db.noteHeads()) local.set(row.path, row);

	const total = manifest.notes.length;
	let done = 0;
	syncState.progressTotal = total;

	const stale: { path: string; pinned: boolean }[] = [];
	for (const entry of manifest.notes) {
		const row = local.get(entry.path);
		local.delete(entry.path);
		if (row && row.base_revision === entry.revision) {
			// Up to date; still track pin flips (they arrive without content).
			if (Boolean(row.pinned) !== entry.pinned) await db.setPinned(entry.path, entry.pinned);
			continue;
		}
		stale.push({ path: entry.path, pinned: entry.pinned });
	}

	// Anything still in `local` is gone from the server. Synced rows follow;
	// dirty/new rows stay and re-create on the next push (server trash makes
	// the remote delete recoverable, so keeping user-edited text is the safe
	// bias).
	for (const [path, row] of local) {
		if (row.sync_status === 'synced') {
			await db.removeNote(path);
			emitNoteEvent({ type: 'note_removed', path });
		}
	}

	syncState.progressDone = total - stale.length;
	const CONCURRENCY = 4;
	const queue = [...stale];
	await Promise.all(
		Array.from({ length: CONCURRENCY }, async () => {
			for (;;) {
				const next = queue.shift();
				if (!next) return;
				await applyRemoteNote(next.path, next.pinned);
				done += 1;
				syncState.progressDone = total - stale.length + done;
			}
		})
	);

	await db.replaceFolders(manifest.folders);
	syncState.progressTotal = 0;
	syncState.progressDone = 0;
}

// --- lifecycle ---------------------------------------------------------------

/** Run `task` once no other replay or push is in flight; its error propagates. */
async function singleFlight(task: () => Promise<void>): Promise<void> {
	while (inFlight) await inFlight.catch(() => {});
	const run = task();
	inFlight = run;
	try {
		await run;
	} finally {
		if (inFlight === run) inFlight = null;
	}
}

/** Push one note's pending local text now instead of at the next replay, so a
 * read never serves the server copy over it. Queued ops go first, as in a
 * replay: a rename still waiting in the queue must land before its row is
 * pushed, or the push re-creates the note beside the old one. A network
 * failure rejects, and the row stays pending. */
export async function pushPath(path: string): Promise<void> {
	await singleFlight(async () => {
		await replayOps();
		const row = await db.getNote(path);
		if (row && (row.sync_status === 'dirty' || row.sync_status === 'new')) await pushOne(row);
		await refreshPendingBadge();
	});
}

/** Delays before re-running a replay that failed while the stream stayed up. */
const RETRY_DELAYS = [5_000, 15_000, 60_000];
let retryAttempt = 0;
let retryTimer: ReturnType<typeof setTimeout> | null = null;

function clearRetry() {
	if (retryTimer) clearTimeout(retryTimer);
	retryTimer = null;
}

function scheduleRetry() {
	clearRetry();
	const delay = RETRY_DELAYS[Math.min(retryAttempt, RETRY_DELAYS.length - 1)];
	retryAttempt += 1;
	retryTimer = setTimeout(() => {
		retryTimer = null;
		void runReplay();
	}, delay);
}

/** Live events are applied and re-broadcast one at a time, in wire order. A
 * rename arrives as note_moved, note_removed, note: handled side by side, the
 * removal (two cache ops) reaches the window before the move (a fetch), and
 * an open editor reads the rename as a deletion. */
let liveChain: Promise<void> = Promise.resolve();

function applyInOrder(event: LiveNoteEvent): Promise<void> {
	const run = liveChain.then(async () => {
		await handleLiveEvent(event);
		emitNoteEvent(event);
	});
	liveChain = run.catch(() => {});
	return run;
}

/** Apply whatever arrived while a pass was out. Queued in one go, ahead of
 * anything the now-live stream delivers next. */
async function drainBuffered(): Promise<void> {
	await Promise.all(buffered.splice(0).map(applyInOrder));
}

async function runReplay(): Promise<void> {
	if (replaying) {
		// The pass already out may have read the queue or the manifest before
		// whatever this reconnect brings.
		replayRequested = true;
		return;
	}
	replaying = true;
	clearRetry();
	refusedReported.clear();
	setMode('replaying');
	let failuresBefore = failures;
	try {
		await singleFlight(async () => {
			failuresBefore = failures;
			await replayOps();
			await pushDirtyNotes();
			await pullManifest();
			await refreshPendingBadge();
			// Dropped mid-pass: stay as the stream left us; its reconnect
			// replays again.
			if (!sseLive) return;
			setMode('live');
			await drainBuffered();
		});
		retryAttempt = 0;
		// Nothing failed along the way: whatever did before is resolved.
		if (failures === failuresBefore) syncState.lastError = '';
	} catch (err) {
		const network = isNetworkError(err);
		if (network || statusOf(err) === 401) {
			await db.debugNote(`replay: ${(err as Error)?.stack ?? String(err)}`).catch(() => {});
		} else {
			await recordFailure('replay', err);
		}
		if (statusOf(err) === 401) {
			// The session is gone; the app signs out and stops the engine.
			replayRequested = false;
			return;
		}
		if (network || !sseLive) {
			// The server went away mid-replay. A dead stream will notice and
			// drive us back through connecting → replaying; a live one (a
			// desktop 500 surfaces as 'Load failed') gets the retry below.
			setMode('offline');
		} else {
			// The server answered with an error, or the cache failed. Either
			// way the server is reachable, so writes should keep going to it
			// rather than queue behind a stuck pass.
			setMode('live');
			void drainBuffered().catch(() => {});
		}
		if (sseLive) scheduleRetry();
	} finally {
		replaying = false;
	}
	if (replayRequested) {
		replayRequested = false;
		if (sseLive) await runReplay();
	}
}

/** Sync now rather than at the next reconnect: replay over the open stream
 * (after any pass already out), or reconnect at once, skipping the backoff. */
export function requestReplay(): void {
	if (!started) return;
	if (sseLive) void runReplay();
	else kickLive?.();
}

const PUSH_DEBOUNCE = 1_500;
const PUSH_INTERVAL = 60_000;
let pushTimer: ReturnType<typeof setTimeout> | null = null;

/** Push rows that landed in the cache while live (a save that hit a network
 * error, a clean merge under a live event) instead of leaving them for the
 * next reconnect. Debounced, so a burst of saves goes out as one pass. */
export function schedulePush(): void {
	if (!started) return;
	if (pushTimer) clearTimeout(pushTimer);
	pushTimer = setTimeout(() => {
		pushTimer = null;
		void pushPending();
	}, PUSH_DEBOUNCE);
}

async function pushPending(): Promise<void> {
	try {
		await singleFlight(async () => {
			// Offline nothing would land, and the reconnect's replay pushes it
			// all. Checked after any replay out ahead of this one finished.
			if (mode !== 'live') return;
			// Queued ops first, as in pushPath: a rename still waiting in the
			// queue must land before its row is pushed.
			await replayOps();
			await pushDirtyNotes();
			await refreshPendingBadge();
		});
	} catch (err) {
		// The rows stay pending for the next interval or reconnect. A lost
		// connection needs no record; anything else broke the pass itself
		// (a failure on one note was recorded by pushDirtyNotes).
		if (!isNetworkError(err)) await recordFailure('push', err);
	}
}

export interface StartOptions {
	onAuthExpired?: () => void;
}

/** A conflict is toasted once, mid-replay, where it is easy to miss. Each is
 * announced once more at the next launch, then only listed in Settings. */
async function announceConflicts(): Promise<void> {
	for (const conflict of await db.unseenConflicts()) {
		window.dispatchEvent(
			new CustomEvent('exscalia:sync-conflict', {
				detail: { path: conflict.path, conflictPath: conflict.conflict_path }
			})
		);
		await db.markConflictSeen(conflict.id);
	}
}

/** Boot the engine: serve the cache immediately, then connect. Returns a
 * stop function. */
export function startSync(options: StartOptions = {}): () => void {
	if (started) return () => {};
	if (!db.isOpen()) {
		// No cache (+layout.ts left the api network-only): relay the stream
		// as the web does, with nothing to apply it to.
		return connectLive(emitNoteEvent, { onAuthExpired: options.onAuthExpired });
	}
	started = true;
	startOptions = options;
	setMode('connecting');
	void refreshPendingBadge();
	// Ahead of the first replay, which may record new ones and toast them.
	void singleFlight(announceConflicts).catch(() => {});

	const stopLive = connectLive(
		(event) => {
			if (mode !== 'live') {
				buffered.push(event);
				return;
			}
			void applyInOrder(event).catch((err) => {
				// Network died between the event and the fetch; the SSE
				// connection is about to notice and drive the reconnect.
				if (!isNetworkError(err)) void recordFailure(`apply ${event.path}`, err);
			});
		},
		{
			onState: (state) => {
				sseLive = state === 'live';
				if (state === 'live') void runReplay();
				else {
					// The reconnect replays; a retry on a dead stream is moot.
					clearRetry();
					if (state === 'offline') setMode('offline');
					else if (mode === 'offline') setMode('connecting');
				}
			},
			onAuthExpired: options.onAuthExpired
		}
	);
	kickLive = stopLive.kick ?? null;

	// A woken laptop should retry immediately, not wait out the backoff; the
	// old socket is suspect even if it has not failed yet.
	const onOnline = () => stopLive.kick?.();
	window.addEventListener('online', onOnline);
	// Coming back to the window is the other wake signal. A healthy stream
	// (or a pass in progress) is left alone.
	const onVisible = () => {
		if (document.visibilityState === 'visible' && isOffline()) requestReplay();
	};
	document.addEventListener('visibilitychange', onVisible);
	// A push that failed while live, and ops queued by other fallbacks.
	const pushInterval = setInterval(() => {
		if (syncState.pending > 0) schedulePush();
	}, PUSH_INTERVAL);

	return () => {
		started = false;
		sseLive = false;
		kickLive = null;
		window.removeEventListener('online', onOnline);
		document.removeEventListener('visibilitychange', onVisible);
		clearInterval(pushInterval);
		clearRetry();
		if (pushTimer) clearTimeout(pushTimer);
		pushTimer = null;
		stopLive();
	};
}
