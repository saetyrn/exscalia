// Desktop only: the sync engine. Owns the live SSE connection, the local
// cache, and the replay of offline work. States:
//
//   offline → connecting → replaying → live
//
// On every (re)connect it first replays queued structural ops, then pushes
// dirty notes (three-way merging when the server moved underneath them), then
// diffs the server manifest against the cache — so by the time it reports
// `live`, the cache and the vault agree.

import { apiFetch, type Note, type NoteListItem } from '../api';
import { connectLive, type LiveNoteEvent } from '../live';
import { mergeThreeWay } from '../merge';
import { syncState } from '../stores.svelte';
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
const buffered: LiveNoteEvent[] = [];

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
		// The server changed a note we also changed. Merge; clean results are
		// adopted as new local content, conflicted ones stay dirty and will be
		// resolved by the push path.
		const merged = mergeThreeWay(row.base_content, row.content, note.content);
		await db.setBase(path, note.content, note.revision);
		if (merged.ok) {
			await db.writeDirty(path, merged.text, await computeRevision(merged.text));
		}
		return;
	}
	await db.adoptServer(path, note.content, note.revision, note.meta, pinned);
}

async function handleLiveEvent(event: LiveNoteEvent): Promise<void> {
	if (event.type === 'note') {
		await applyRemoteNote(event.path);
	} else {
		const row = await db.getNote(event.path);
		if (row && (row.sync_status === 'dirty' || row.sync_status === 'new')) return;
		await db.removeNote(event.path);
	}
}

// --- replay: queued ops, then dirty notes, then manifest ---------------------

const MAX_OP_ATTEMPTS = 3;

class Unreachable extends Error {}

function isNetworkError(err: unknown): boolean {
	return !(err instanceof Object && 'status' in err && (err as { status: number }).status > 0);
}

async function replayOps(): Promise<void> {
	for (const op of await db.listOps()) {
		try {
			await replayOneOp(op);
			await db.deleteOp(op.id);
		} catch (err) {
			if (isNetworkError(err)) throw new Unreachable();
			// A real 4xx/5xx: the operation itself is bad. Retry a few times
			// (the server may have been mid-restart), then drop it — the note
			// rows themselves are still intact and will re-push.
			const attempts = await db.recordOpFailure(op.id, (err as Error).message);
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
				await db.adoptServer(op.path, created.content, created.revision, created.meta);
			} catch (err) {
				const status = (err as { status?: number }).status;
				if (status === 409 || status === 400) {
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

async function pushDirtyNotes(): Promise<void> {
	for (const row of await db.pendingNotes()) {
		try {
			await pushOne(row);
		} catch (err) {
			await db.debugNote(`push ${row.path}: ${(err as Error).stack ?? String(err)}`);
			if (isNetworkError(err)) throw new Unreachable();
			// A 4xx on one note (bad path etc.) should not strand the rest.
		}
	}
}

async function pushOne(row: db.NoteRow): Promise<void> {
	let content = row.content;
	let baseRevision = row.base_revision;
	let baseContent = row.base_content;

	if (row.sync_status === 'new') {
		try {
			const created = await api.createNote(row.path, content);
			await db.adoptServer(row.path, created.content, created.revision, created.meta);
			emitNoteEvent({ type: 'note', path: row.path, revision: created.revision });
			return;
		} catch (err) {
			const status = (err as { status?: number }).status;
			if (status !== 409 && status !== 400) throw err;
			// Exists remotely: fall through to the save path, merging from empty.
			baseRevision = null;
			baseContent = '';
		}
	}

	for (let round = 0; round < MAX_PUSH_ROUNDS; round++) {
		const result = await api.saveNote(row.path, content, baseRevision, 'reject');
		if (result.status !== 'stale') {
			await db.adoptServer(row.path, result.content, result.revision, result.meta);
			emitNoteEvent({ type: 'note', path: row.path, revision: result.revision });
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
		await db.adoptServer(row.path, parked.content, parked.revision, parked.meta);
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
	// replay (or its own live event) will pick it up.
}

interface Manifest {
	notes: (NoteListItem & { revision: string })[];
	folders: string[];
}

async function pullManifest(): Promise<void> {
	const res = await apiFetch('/api/notes/manifest');
	if (!res.ok) throw new Error(`manifest ${res.status}`);
	const manifest = (await res.json()) as Manifest;

	const local = new Map<string, db.NoteRow>();
	for (const row of await db.allNotes()) local.set(row.path, row);

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

async function runReplay(): Promise<void> {
	if (replaying) return;
	replaying = true;
	setMode('replaying');
	try {
		await replayOps();
		await pushDirtyNotes();
		await pullManifest();
		await refreshPendingBadge();
		setMode('live');
		// Apply whatever arrived while we were replaying.
		while (buffered.length) {
			const event = buffered.shift()!;
			await handleLiveEvent(event);
			emitNoteEvent(event);
		}
	} catch {
		// Server went away mid-replay; the SSE connection will notice and
		// drive us back through connecting → replaying when it returns.
		setMode('offline');
	} finally {
		replaying = false;
	}
}

export interface StartOptions {
	onAuthExpired?: () => void;
}

/** Boot the engine: serve the cache immediately, then connect. Returns a
 * stop function. */
export function startSync(options: StartOptions = {}): () => void {
	if (started) return () => {};
	started = true;
	setMode('connecting');
	void refreshPendingBadge();

	const stopLive = connectLive(
		(event) => {
			if (mode !== 'live') {
				buffered.push(event);
				return;
			}
			void handleLiveEvent(event)
				.then(() => emitNoteEvent(event))
				.catch(() => {
					// Network died between the event and the fetch; the SSE
					// connection is about to notice and drive the reconnect.
				});
		},
		{
			onState: (state) => {
				if (state === 'live') void runReplay();
				else if (state === 'offline') setMode('offline');
				else if (mode === 'offline') setMode('connecting');
			},
			onAuthExpired: options.onAuthExpired
		}
	);

	// A woken laptop should retry immediately, not wait out the backoff.
	const onOnline = () => {
		if (mode === 'offline') setMode('connecting');
	};
	window.addEventListener('online', onOnline);

	return () => {
		started = false;
		window.removeEventListener('online', onOnline);
		stopLive();
	};
}
