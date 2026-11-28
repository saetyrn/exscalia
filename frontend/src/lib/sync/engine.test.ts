// The desktop sync engine end to end, against an in-memory cache and a fake
// server. The cache mirrors db.ts's SQL (writeDirty keeps 'new', setBase
// leaves sync_status alone, adoptServer marks synced unless the row was
// written after `unlessUpdatedAfter`, insertNew upserts to 'new'); the server
// mirrors backend save_note/create_note, including the null base_revision
// escape hatch (an unconditional overwrite) the engine must never use, and
// create's refusal of a name that differs from a sibling only by case or
// Unicode form. Reconnects are driven the way live.ts drives them: the SSE
// 'ready' frame calls onState('live'), which runs a replay.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConnectOptions, LiveNoteEvent, LiveState } from '../live';
import { mergeThreeWay } from '../merge';
import type { ConflictRow, NoteHead, NoteRow, OpKind, OpRow } from './db';

interface Call {
	kind: string;
	path: string;
	content?: string;
	base?: string | null;
	onConflict?: string;
	outcome?: string;
}

const h = vi.hoisted(() => {
	// Content-addressed like the server's sha256 (equal text, equal revision);
	// the engine only ever compares revisions.
	const revisions = new Map<string, string>();
	const rev = (text: string) => {
		let revision = revisions.get(text);
		if (!revision) {
			revision = `rev${revisions.size + 1}`;
			revisions.set(text, revision);
		}
		return revision;
	};
	const nameFor = (path: string) => (path.split('/').pop() ?? path).replace(/\.md$/, '');

	class HttpError extends Error {
		status: number;
		constructor(status: number, detail: string) {
			super(detail);
			this.status = status;
		}
	}

	// --- the local cache (db.ts) ---------------------------------------------

	const cache = {
		notes: new Map<string, NoteRow>(),
		ops: [] as OpRow[],
		conflicts: [] as ConflictRow[],
		folders: [] as string[],
		lastError: null as string | null,
		/** openSyncDb succeeded (db.isOpen()). */
		open: true,
		nextOpId: 1,
		clock: 0
	};
	const tick = () => ++cache.clock;
	const copy = (row: NoteRow): NoteRow => ({ ...row });
	const pending = (row: NoteRow) => row.sync_status === 'dirty' || row.sync_status === 'new';

	// No allNotes(): the engine has no business loading every body, and a
	// call would fail the replay (see noteHeads in db.ts).
	const db = {
		isOpen: () => cache.open,
		async noteHeads(): Promise<NoteHead[]> {
			return [...cache.notes.values()]
				.filter((row) => row.sync_status !== 'deleted')
				.sort((a, b) => (a.path < b.path ? -1 : 1))
				.map(({ path, name, pinned, base_revision, sync_status, meta_json }) => ({
					path,
					name,
					pinned,
					base_revision,
					sync_status,
					meta_json
				}));
		},
		async getNote(path: string): Promise<NoteRow | null> {
			const row = cache.notes.get(path);
			return row ? copy(row) : null;
		},
		async adoptServer(
			path: string,
			content: string,
			revision: string,
			meta: Record<string, unknown>,
			pinned?: boolean,
			unlessUpdatedAfter?: number
		): Promise<boolean> {
			const existing = cache.notes.get(path);
			if (
				unlessUpdatedAfter != null &&
				existing?.updated_at != null &&
				existing.updated_at > unlessUpdatedAfter
			) {
				return false;
			}
			const now = tick();
			cache.notes.set(path, {
				path,
				name: nameFor(path),
				pinned: pinned === undefined ? (existing?.pinned ?? 0) : pinned ? 1 : 0,
				content,
				revision,
				base_content: content,
				base_revision: revision,
				meta_json: JSON.stringify(meta ?? {}),
				sync_status: 'synced',
				fetched_at: now,
				updated_at: now
			});
			return true;
		},
		async rebasePending(
			path: string,
			baseContent: string,
			baseRevision: string,
			fromRevision: string | null
		): Promise<void> {
			const row = cache.notes.get(path);
			if (!row || !pending(row) || row.base_revision !== fromRevision) return;
			Object.assign(row, {
				base_content: baseContent,
				base_revision: baseRevision,
				sync_status: 'dirty'
			});
		},
		async writeDirty(path: string, content: string, revision: string): Promise<void> {
			const row = cache.notes.get(path);
			if (!row) return;
			row.content = content;
			row.revision = revision;
			row.updated_at = tick();
			row.sync_status = row.sync_status === 'new' ? 'new' : 'dirty';
		},
		async insertNew(path: string, content: string, revision: string): Promise<void> {
			const now = tick();
			const row = cache.notes.get(path);
			if (row) {
				Object.assign(row, { content, revision, sync_status: 'new', updated_at: now });
				return;
			}
			cache.notes.set(path, {
				path,
				name: nameFor(path),
				pinned: 0,
				content,
				revision,
				base_content: '',
				base_revision: null,
				meta_json: '{}',
				sync_status: 'new',
				fetched_at: null,
				updated_at: now
			});
		},
		async setBase(path: string, baseContent: string, baseRevision: string): Promise<void> {
			const row = cache.notes.get(path);
			if (row) Object.assign(row, { base_content: baseContent, base_revision: baseRevision });
		},
		async setPinned(path: string, pinned: boolean): Promise<void> {
			const row = cache.notes.get(path);
			if (row) row.pinned = pinned ? 1 : 0;
		},
		async removeNote(path: string): Promise<void> {
			cache.notes.delete(path);
		},
		async pendingNotes(): Promise<NoteRow[]> {
			return [...cache.notes.values()]
				.filter(pending)
				.sort((a, b) => (a.updated_at ?? -1) - (b.updated_at ?? -1))
				.map(copy);
		},
		async pendingCount(): Promise<number> {
			return [...cache.notes.values()].filter(pending).length + cache.ops.length;
		},
		async replaceFolders(paths: string[]): Promise<void> {
			cache.folders = [...new Set(paths)];
		},
		async listOps(): Promise<OpRow[]> {
			return [...cache.ops].sort((a, b) => a.id - b.id).map((op) => ({ ...op }));
		},
		async deleteOp(id: number): Promise<void> {
			cache.ops = cache.ops.filter((op) => op.id !== id);
		},
		async recordOpFailure(id: number, error: string, counted = true): Promise<number> {
			const op = cache.ops.find((o) => o.id === id);
			if (!op) return 0;
			if (counted) op.attempts += 1;
			op.last_error = error.slice(0, 500);
			return op.attempts;
		},
		async recordConflict(path: string, conflictPath: string): Promise<void> {
			cache.conflicts.push({
				id: cache.conflicts.length + 1,
				path,
				conflict_path: conflictPath,
				created_at: tick(),
				seen: 0
			});
		},
		async unseenConflicts(): Promise<ConflictRow[]> {
			return cache.conflicts.filter((c) => !c.seen).map((c) => ({ ...c }));
		},
		async markConflictSeen(id: number): Promise<void> {
			const conflict = cache.conflicts.find((c) => c.id === id);
			if (conflict) conflict.seen = 1;
		},
		async debugNote(message: string): Promise<void> {
			cache.lastError = message;
		}
	};

	// --- the server (backend/app/api/notes.py) -------------------------------

	const server = {
		notes: new Map<string, string>(),
		pinned: new Set<string>(),
		folders: new Set<string>(),
		calls: [] as Call[],
		/** Runs before a request is answered: throw to fail it, or change the
		 * vault to stage a race. */
		before: null as ((call: Call) => void) | null,
		/** Runs after a request was answered. */
		after: null as ((call: Call) => void) | null,
		/** Awaited before a request is answered: park it on a promise to keep
		 * a replay or push in flight. */
		hold: null as ((call: Call) => Promise<void> | void) | null,
		/** What GET /api/notes/manifest answers with. */
		manifestStatus: 200
	};

	function checkPath(path: string) {
		if (!path.endsWith('.md')) throw new HttpError(400, 'Note paths must end in .md');
	}
	/** vault/notes.py _refuse_sibling: a name equal up to case and Unicode
	 * form to one already in the folder is taken. */
	function identity(path: string): string {
		const cut = path.lastIndexOf('/') + 1;
		return path.slice(0, cut) + path.slice(cut).normalize('NFC').toLowerCase();
	}
	function current(path: string): string {
		checkPath(path);
		const content = server.notes.get(path);
		if (content === undefined) throw new HttpError(404, `Note not found: ${path}`);
		return content;
	}
	function noteOf(path: string, content: string) {
		return { path, content, meta: {}, revision: rev(content), status: 'saved' as const };
	}
	function conflictPathFor(path: string): string {
		const stem = path.replace(/\.md$/, ' (conflict)');
		let candidate = `${stem}.md`;
		for (let n = 2; server.notes.has(candidate); n++) candidate = `${stem} ${n}.md`;
		return candidate;
	}

	async function handle<T>(call: Call, answer: () => T): Promise<T> {
		server.calls.push(call);
		await Promise.resolve();
		await server.hold?.(call);
		try {
			server.before?.(call);
			const result = answer();
			const status = (result as { status?: unknown }).status;
			call.outcome = typeof status === 'string' ? status : 'ok';
			server.after?.(call);
			return result;
		} catch (err) {
			call.outcome = err instanceof HttpError ? String(err.status) : 'network';
			throw err;
		}
	}

	const netApi = {
		readNote: (path: string) => handle({ kind: 'read', path }, () => noteOf(path, current(path))),
		createNote: (path: string, content = '') =>
			handle({ kind: 'create', path, content }, () => {
				checkPath(path);
				const taken = [...server.notes.keys()].find((p) => identity(p) === identity(path));
				if (taken) throw new HttpError(409, `Note already exists: ${taken}`);
				server.notes.set(path, content);
				return noteOf(path, content);
			}),
		saveNote: (
			path: string,
			content: string,
			baseRevision?: string | null,
			onConflict: 'copy' | 'reject' = 'copy'
		) =>
			handle({ kind: 'save', path, content, base: baseRevision ?? null, onConflict }, () => {
				const existing = current(path);
				const revision = rev(existing);
				// Same gate as save_note: a null base skips the staleness check.
				if (baseRevision != null && baseRevision !== revision) {
					if (onConflict === 'reject') return { ...noteOf(path, existing), status: 'stale' as const };
					const conflictPath = conflictPathFor(path);
					server.notes.set(conflictPath, content);
					return {
						...noteOf(path, existing),
						status: 'conflict_copy' as const,
						conflict_path: conflictPath,
						conflict_revision: rev(content)
					};
				}
				server.notes.set(path, content);
				return noteOf(path, content);
			}),
		deleteNote: (path: string) =>
			handle({ kind: 'delete', path }, () => {
				current(path);
				server.notes.delete(path);
				return { ok: true };
			}),
		renameNote: (path: string, newPath: string) =>
			handle({ kind: 'rename', path }, () => {
				const content = current(path);
				checkPath(newPath);
				if (server.notes.has(newPath)) throw new HttpError(409, `Note already exists: ${newPath}`);
				server.notes.delete(path);
				server.notes.set(newPath, content);
				return { path: newPath };
			}),
		pinNote: (path: string, pinned: boolean) =>
			handle({ kind: 'pin', path }, () => {
				current(path);
				if (pinned) server.pinned.add(path);
				else server.pinned.delete(path);
				return { ok: true };
			}),
		createFolder: (path: string) =>
			handle({ kind: 'create_folder', path }, () => {
				server.folders.add(path);
				return { ok: true };
			}),
		renameFolder: (path: string, newPath: string) =>
			handle({ kind: 'rename_folder', path }, () => {
				server.folders.delete(path);
				server.folders.add(newPath);
				return { ok: true };
			}),
		deleteFolder: (path: string) =>
			handle({ kind: 'delete_folder', path }, () => {
				server.folders.delete(path);
				return { ok: true };
			})
	};

	/** GET /api/notes/manifest, the only raw fetch the engine makes. */
	const apiFetch = (url: string) =>
		handle({ kind: 'manifest', path: url }, () => {
			if (server.manifestStatus !== 200) {
				return { ok: false, status: server.manifestStatus, json: async () => ({}) };
			}
			const body = {
				notes: [...server.notes.keys()].sort().map((path) => ({
					path,
					name: nameFor(path),
					pinned: server.pinned.has(path),
					revision: rev(server.notes.get(path)!)
				})),
				folders: [...server.folders].sort()
			};
			return { ok: true, status: 200, json: async () => body };
		});

	// --- live.ts, stores, toasts ---------------------------------------------

	const live = {
		onEvent: null as ((event: LiveNoteEvent) => void) | null,
		onState: null as ((state: LiveState) => void) | null,
		kick: vi.fn()
	};
	function connectLive(onEvent: (event: LiveNoteEvent) => void, options: ConnectOptions = {}) {
		live.onEvent = onEvent;
		live.onState = options.onState ?? null;
		return Object.assign(() => {}, { kick: live.kick });
	}

	const syncState = { mode: 'live', pending: 0, progressDone: 0, progressTotal: 0, lastError: '' };
	const notify = vi.fn();

	function reset() {
		cache.notes.clear();
		cache.ops = [];
		cache.conflicts = [];
		cache.folders = [];
		cache.lastError = null;
		cache.open = true;
		cache.nextOpId = 1;
		cache.clock = 0;
		server.notes.clear();
		server.pinned.clear();
		server.folders.clear();
		server.calls = [];
		server.before = null;
		server.after = null;
		server.hold = null;
		server.manifestStatus = 200;
		live.onEvent = null;
		live.onState = null;
		live.kick.mockReset();
		Object.assign(syncState, {
			mode: 'live',
			pending: 0,
			progressDone: 0,
			progressTotal: 0,
			lastError: ''
		});
		notify.mockReset();
	}

	return {
		rev,
		tick,
		HttpError,
		cache,
		db,
		server,
		netApi,
		apiFetch,
		live,
		connectLive,
		syncState,
		notify,
		reset
	};
});

vi.mock('./db', () => h.db);
vi.mock('./offline-api', () => ({ netApi: h.netApi }));
vi.mock('../live', () => ({ connectLive: h.connectLive, CLIENT_ID: 'test-client' }));
vi.mock('../api', () => ({ apiFetch: h.apiFetch, ApiError: h.HttpError }));
vi.mock('../stores.svelte', () => ({ syncState: h.syncState }));
vi.mock('../ui.svelte', () => ({ notify: h.notify }));

let engine: typeof import('./engine');
let stop: (() => void) | null = null;
let events: { type: string; detail: unknown }[] = [];

beforeEach(async () => {
	h.reset();
	vi.stubGlobal('window', new EventTarget());
	vi.stubGlobal('document', Object.assign(new EventTarget(), { visibilityState: 'visible' }));
	events = [];
	for (const type of ['exscalia:sync-mode', 'exscalia:live', 'exscalia:sync-conflict']) {
		window.addEventListener(type, (e) => events.push({ type, detail: (e as CustomEvent).detail }));
	}
	// The engine keeps its mode, started flag and event buffer in module
	// state; every test gets a fresh copy.
	vi.resetModules();
	engine = await import('./engine');
	stop = engine.startSync();
});

afterEach(() => {
	stop?.();
	stop = null;
	vi.unstubAllGlobals();
});

// --- seeding -------------------------------------------------------------------

function put(path: string, fields: Partial<NoteRow>) {
	h.cache.notes.set(path, {
		path,
		name: (path.split('/').pop() ?? path).replace(/\.md$/, ''),
		pinned: 0,
		content: '',
		revision: null,
		base_content: '',
		base_revision: null,
		meta_json: '{}',
		sync_status: 'synced',
		fetched_at: null,
		updated_at: h.tick(),
		...fields
	});
}

/** A note the server and the cache agree on. */
function synced(path: string, content: string) {
	h.server.notes.set(path, content);
	put(path, {
		content,
		revision: h.rev(content),
		base_content: content,
		base_revision: h.rev(content)
	});
}

/** An offline edit of `base`, not yet pushed. The server is left alone. */
function dirty(path: string, base: string, mine: string) {
	put(path, {
		content: mine,
		revision: h.rev(mine),
		base_content: base,
		base_revision: h.rev(base),
		sync_status: 'dirty'
	});
}

/** A note created offline (a new note, or openDaily's local stub), with its
 * queued create op — what offline-api's localSave leaves behind. */
function createdOffline(path: string, content: string) {
	put(path, { content, revision: h.rev(content), sync_status: 'new' });
	queue('create', path);
}

function queue(op: OpKind, path: string, payload: Record<string, unknown> | null = null) {
	h.cache.ops.push({
		id: h.cache.nextOpId++,
		op,
		path,
		new_path: null,
		payload_json: payload ? JSON.stringify(payload) : null,
		created_at: 0,
		attempts: 0,
		last_error: null
	});
}

// --- driving -------------------------------------------------------------------

/** Captured before any test fakes timers: settling must never move the clock. */
const realSetTimeout = globalThis.setTimeout;

/** Let fire-and-forget work (a live event, the post-replay drain) finish. */
async function settle() {
	for (let i = 0; i < 5; i++) await new Promise((resolve) => realSetTimeout(resolve, 0));
}

/** One reconnect: SSE 'ready' → replay. Resolves with the mode it ends in. */
async function reconnect(): Promise<string> {
	const done = new Promise<string>((resolve) => {
		const onMode = (e: Event) => {
			const mode = (e as CustomEvent).detail as string;
			if (mode !== 'live' && mode !== 'offline') return;
			window.removeEventListener('exscalia:sync-mode', onMode);
			resolve(mode);
		};
		window.addEventListener('exscalia:sync-mode', onMode);
	});
	h.live.onState!('live');
	const mode = await done;
	await settle();
	return mode;
}

/** A request parked until `open()`: keeps a replay or push in flight. */
function gate() {
	let open!: () => void;
	const opened = new Promise<void>((resolve) => (open = resolve));
	return { opened, open };
}

const saves = (path: string) =>
	h.server.calls.filter((c) => c.kind === 'save' && c.path === path);
const manifests = () => h.server.calls.filter((c) => c.kind === 'manifest').length;
const row = (path: string) => h.cache.notes.get(path);

// Two offline edits of the same words: a genuine conflict.
const BASE = '# Trip\n\nMeet at noon.\n';
const MINE = '# Trip\n\nMeet at one.\n';
const THEIRS = '# Trip\n\nMeet at two.\n';

describe('push', () => {
	it('sends a clean edit with its base revision and adopts the answer', async () => {
		const base = '# A\n\nfirst\n';
		const mine = '# A\n\nfirst\nsecond\n';
		synced('a.md', base);
		dirty('a.md', base, mine);

		expect(await reconnect()).toBe('live');

		expect(saves('a.md')).toEqual([
			expect.objectContaining({ base: h.rev(base), onConflict: 'reject', outcome: 'saved' })
		]);
		expect(h.server.notes.get('a.md')).toBe(mine);
		expect(row('a.md')).toMatchObject({
			sync_status: 'synced',
			content: mine,
			base_content: mine,
			base_revision: h.rev(mine)
		});
		expect(h.syncState.pending).toBe(0);
	});

	it('merges a stale push cleanly and retries with the refreshed base', async () => {
		const base = 'alpha\nbeta\ngamma\n';
		const mine = 'alpha edited here\nbeta\ngamma\n';
		const theirs = 'alpha\nbeta\ngamma edited there\n';
		const merged = 'alpha edited here\nbeta\ngamma edited there\n';
		expect(mergeThreeWay(base, mine, theirs)).toEqual({ ok: true, text: merged });
		h.server.notes.set('a.md', theirs);
		dirty('a.md', base, mine);

		expect(await reconnect()).toBe('live');

		expect(saves('a.md')).toEqual([
			expect.objectContaining({ base: h.rev(base), content: mine, outcome: 'stale' }),
			expect.objectContaining({ base: h.rev(theirs), content: merged, outcome: 'saved' })
		]);
		expect(h.server.notes.get('a.md')).toBe(merged);
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: merged });
	});

	it('parks a conflicting edit as a copy, sent with the original base', async () => {
		expect(mergeThreeWay(BASE, MINE, THEIRS).ok).toBe(false);
		h.server.notes.set('trip.md', THEIRS);
		dirty('trip.md', BASE, MINE);

		expect(await reconnect()).toBe('live');

		const [first, second] = saves('trip.md');
		expect(first).toMatchObject({ base: h.rev(BASE), onConflict: 'reject', outcome: 'stale' });
		// The stale answer carried the server's revision; reusing it would make
		// the copy save an ordinary overwrite.
		expect(second).toMatchObject({
			base: h.rev(BASE),
			onConflict: 'copy',
			outcome: 'conflict_copy'
		});
		expect(saves('trip.md')).toHaveLength(2);
		expect(h.server.notes.get('trip.md')).toBe(THEIRS);
		expect(h.server.notes.get('trip (conflict).md')).toBe(MINE);
		expect(h.cache.conflicts).toMatchObject([
			{ path: 'trip.md', conflict_path: 'trip (conflict).md' }
		]);
		expect(events).toContainEqual({
			type: 'exscalia:sync-conflict',
			detail: { path: 'trip.md', conflictPath: 'trip (conflict).md' }
		});
		expect(row('trip.md')).toMatchObject({ sync_status: 'synced', content: THEIRS });
		expect(row('trip (conflict).md')).toMatchObject({ sync_status: 'synced', content: MINE });
	});
});

describe('a note that changes under every merge', () => {
	it('stays pending and says why', async () => {
		synced('a.md', 'a\n');
		dirty('a.md', 'a\n', 'a\nmine\n');
		let n = 0;
		h.server.before = (call) => {
			// Another device saves ahead of every round.
			if (call.kind === 'save') h.server.notes.set('a.md', `remote ${++n}\na\n`);
		};

		expect(await reconnect()).toBe('live');

		expect(saves('a.md').map((c) => c.outcome)).toEqual(['stale', 'stale', 'stale']);
		expect(row('a.md')).toMatchObject({ sync_status: 'dirty' });
		expect(h.syncState.lastError).toBe('push a.md: changed on the server during each of 3 merges');
	});
});

describe('offline create that collides with a server note', () => {
	const DAILY = 'daily/2026-09-22.md';
	const STUB = '# 2026-09-22\n\n';

	it('never pushes with a null base; identical text just adopts', async () => {
		h.server.notes.set(DAILY, STUB);
		createdOffline(DAILY, STUB);

		expect(await reconnect()).toBe('live');

		expect(h.server.calls.filter((c) => c.kind === 'save' && c.base === null)).toEqual([]);
		expect(saves(DAILY).map((c) => c.outcome)).toEqual(['stale', 'saved']);
		expect(saves(DAILY)[1].base).toBe(h.rev(STUB));
		expect(h.server.notes.get(DAILY)).toBe(STUB);
		expect([...h.server.notes.keys()]).toEqual([DAILY]);
		expect(row(DAILY)).toMatchObject({ sync_status: 'synced', content: STUB });
		expect(h.cache.ops).toEqual([]);
	});

	it('keeps the server note and parks differing local text as a copy', async () => {
		const filled = '# 2026-09-22\n\nCalled the clinic.\nInvoice 4471 paid.\n';
		h.server.notes.set(DAILY, filled);
		createdOffline(DAILY, STUB);

		expect(await reconnect()).toBe('live');

		// The fake server honours a null base as an unconditional overwrite,
		// exactly like save_note — so the filled note surviving proves none
		// was sent.
		expect(h.server.calls.filter((c) => c.kind === 'save' && c.base === null)).toEqual([]);
		expect(saves(DAILY).map((c) => [c.onConflict, c.outcome])).toEqual([
			['reject', 'stale'],
			['copy', 'conflict_copy']
		]);
		expect(h.server.notes.get(DAILY)).toBe(filled);
		expect(h.server.notes.get('daily/2026-09-22 (conflict).md')).toBe(STUB);
		expect(row(DAILY)).toMatchObject({ sync_status: 'synced', content: filled });
		expect(h.cache.conflicts).toHaveLength(1);
	});
});

describe('a remote change that conflicts with a pending edit', () => {
	it('leaves the base alone when a pin op refetches the note mid-replay', async () => {
		h.server.notes.set('trip.md', THEIRS);
		dirty('trip.md', BASE, MINE);
		queue('pin', 'trip.md', { pinned: true });
		const basesAtSave: (string | null | undefined)[] = [];
		h.server.before = (call) => {
			if (call.kind === 'save') basesAtSave.push(row('trip.md')?.base_revision);
		};

		expect(await reconnect()).toBe('live');

		// The pin op's refetch ran applyRemoteNote before the push pass.
		const kinds = h.server.calls.filter((c) => c.path === 'trip.md').map((c) => c.kind);
		expect(kinds.slice(0, 3)).toEqual(['pin', 'read', 'save']);
		expect(basesAtSave[0]).toBe(h.rev(BASE));
		expect(saves('trip.md').map((c) => [c.base, c.onConflict, c.outcome])).toEqual([
			[h.rev(BASE), 'reject', 'stale'],
			[h.rev(BASE), 'copy', 'conflict_copy']
		]);
		expect(h.server.notes.get('trip.md')).toBe(THEIRS);
		expect(h.server.notes.get('trip (conflict).md')).toBe(MINE);
	});

	it('leaves the base alone on a live event, so the next push parks a copy', async () => {
		synced('trip.md', BASE);
		expect(await reconnect()).toBe('live');

		// A save fell back to the cache, then another device saved over it.
		await h.db.writeDirty('trip.md', MINE, h.rev(MINE));
		h.server.notes.set('trip.md', THEIRS);
		h.live.onEvent!({ type: 'note', path: 'trip.md', revision: h.rev(THEIRS) });
		await settle();

		expect(row('trip.md')).toMatchObject({
			sync_status: 'dirty',
			content: MINE,
			base_content: BASE,
			base_revision: h.rev(BASE)
		});

		expect(await reconnect()).toBe('live');

		expect(saves('trip.md').map((c) => [c.onConflict, c.outcome])).toEqual([
			['reject', 'stale'],
			['copy', 'conflict_copy']
		]);
		expect(h.server.notes.get('trip.md')).toBe(THEIRS);
		expect(h.server.notes.get('trip (conflict).md')).toBe(MINE);
	});
});

describe('a pending edit whose server note is gone', () => {
	it('re-creates the note from the local text', async () => {
		dirty('gone.md', 'v1\n', 'v1\nwritten offline\n');

		expect(await reconnect()).toBe('live');

		const calls = h.server.calls.filter((c) => c.path === 'gone.md');
		expect(calls.map((c) => [c.kind, c.outcome])).toEqual([
			['save', '404'],
			['create', 'saved']
		]);
		expect(h.server.notes.get('gone.md')).toBe('v1\nwritten offline\n');
		expect(row('gone.md')).toMatchObject({ sync_status: 'synced' });
		expect(h.syncState.pending).toBe(0);
	});

	it('falls into the collision path when the note reappears before the create', async () => {
		dirty('gone.md', 'v1\n', 'v1\nwritten offline\n');
		h.server.before = (call) => {
			if (call.kind === 'create' && call.path === 'gone.md')
				h.server.notes.set('gone.md', 'Recreated on the phone.\n');
		};

		expect(await reconnect()).toBe('live');

		expect(h.server.calls.filter((c) => c.kind === 'save' && c.base === null)).toEqual([]);
		expect(h.server.notes.get('gone.md')).toBe('Recreated on the phone.\n');
		expect(h.server.notes.get('gone (conflict).md')).toBe('v1\nwritten offline\n');
		expect(row('gone.md')).toMatchObject({ sync_status: 'synced' });
	});
});

describe('a note the server refuses', () => {
	it('says so once per pass, keeps the row, and still pushes the rest', async () => {
		createdOffline('Scratch', 'loose thought\n');
		put('legacy.txt', {
			content: 'old\n',
			base_content: 'older\n',
			base_revision: h.rev('older\n'),
			sync_status: 'dirty'
		});
		synced('a.md', 'a\n');
		dirty('a.md', 'a\n', 'a\nb\n');

		expect(await reconnect()).toBe('live');

		expect(h.notify.mock.calls).toEqual([
			['Could not sync “Scratch”: Note paths must end in .md', { tone: 'error' }],
			['Could not sync “legacy.txt”: Note paths must end in .md', { tone: 'error' }]
		]);
		// No re-create from the save path, no save from the create path.
		expect(h.server.calls.filter((c) => c.path === 'Scratch').map((c) => c.kind)).toEqual([
			'create',
			'create'
		]);
		expect(h.server.calls.filter((c) => c.path === 'legacy.txt').map((c) => c.kind)).toEqual([
			'save'
		]);
		expect(row('Scratch')).toMatchObject({ sync_status: 'new', content: 'loose thought\n' });
		expect(row('legacy.txt')).toMatchObject({ sync_status: 'dirty', content: 'old\n' });
		expect(h.server.notes.get('a.md')).toBe('a\nb\n');
		// A 400 is not "already exists": the create op counts a failure.
		expect(h.cache.ops).toMatchObject([{ op: 'create', path: 'Scratch', attempts: 1 }]);

		expect(await reconnect()).toBe('live');
		expect(h.notify).toHaveBeenCalledTimes(4);
		expect(h.cache.ops).toMatchObject([{ attempts: 2 }]);
	});
});

describe('a note whose name the server holds under another case', () => {
	it('is reported as refused instead of retried in silence', async () => {
		h.server.notes.set('Ideas.md', 'theirs\n');
		createdOffline('ideas.md', 'mine\n');
		// Renamed by case on another device while edited here.
		h.server.notes.set('Plans.md', 'plans\n');
		dirty('plans.md', 'plans\n', 'plans\nmore\n');

		expect(await reconnect()).toBe('live');

		const trail = (path: string) =>
			h.server.calls.filter((c) => c.path === path).map((c) => [c.kind, c.outcome]);
		// The queued create, then the push: taken to create, missing to save.
		expect(trail('ideas.md')).toEqual([
			['create', '409'],
			['create', '409'],
			['save', '404'],
			['create', '409']
		]);
		expect(trail('plans.md')).toEqual([
			['save', '404'],
			['create', '409'],
			['save', '404'],
			['create', '409']
		]);
		expect(h.notify.mock.calls).toHaveLength(2);
		expect(h.notify.mock.calls).toEqual(
			expect.arrayContaining([
				['Could not sync “ideas”: Note already exists: Ideas.md', { tone: 'error' }],
				['Could not sync “plans”: Note already exists: Plans.md', { tone: 'error' }]
			])
		);
		expect(h.syncState.lastError).toMatch(/^push (ideas|plans)\.md: Note already exists/);
		expect(row('ideas.md')).toMatchObject({ sync_status: 'new', content: 'mine\n' });
		expect(row('plans.md')).toMatchObject({ sync_status: 'dirty', content: 'plans\nmore\n' });
		expect(h.server.notes.get('Ideas.md')).toBe('theirs\n');
		expect(h.server.notes.get('Plans.md')).toBe('plans\n');
	});
});

describe('replay', () => {
	it('stops at a network error with nothing lost', async () => {
		synced('a.md', 'a\n');
		dirty('a.md', 'a\n', 'a\nedit\n');
		synced('b.md', 'b\n');
		dirty('b.md', 'b\n', 'b\nedit\n');
		// Synced locally but missing from the server: the manifest pass would
		// delete it, so it must never be reached.
		put('stale-synced.md', { content: 'keep\n', base_content: 'keep\n' });
		h.server.before = (call) => {
			if (call.kind === 'save' && call.path === 'b.md') throw new TypeError('Load failed');
		};

		expect(await reconnect()).toBe('offline');

		expect(h.server.notes.get('a.md')).toBe('a\nedit\n');
		expect(h.server.notes.get('b.md')).toBe('b\n');
		expect(row('b.md')).toMatchObject({ sync_status: 'dirty', content: 'b\nedit\n' });
		expect(row('stale-synced.md')).toBeDefined();
		expect(h.server.calls.some((c) => c.kind === 'manifest')).toBe(false);
		expect(engine.engineMode()).toBe('offline');
	});

	it('keeps a queued op untouched when the network drops during op replay', async () => {
		queue('create_folder', 'Projects');
		dirty('a.md', 'a\n', 'a\nedit\n');
		h.server.before = (call) => {
			if (call.kind === 'create_folder') throw new TypeError('Load failed');
		};

		expect(await reconnect()).toBe('offline');

		expect(h.cache.ops).toMatchObject([{ op: 'create_folder', attempts: 0 }]);
		expect(saves('a.md')).toEqual([]);
		expect(row('a.md')).toMatchObject({ sync_status: 'dirty' });
	});

	it('drops an op the server keeps refusing only after three attempts', async () => {
		queue('create_folder', 'bad//name');
		h.server.before = (call) => {
			if (call.kind === 'create_folder') throw new h.HttpError(400, 'Invalid folder path');
		};

		expect(await reconnect()).toBe('live');
		expect(h.cache.ops).toMatchObject([{ attempts: 1, last_error: 'Invalid folder path' }]);
		expect(await reconnect()).toBe('live');
		expect(h.cache.ops).toMatchObject([{ attempts: 2 }]);
		expect(await reconnect()).toBe('live');
		expect(h.cache.ops).toEqual([]);
	});

	it('never drops an op for a server error, only for refusals', async () => {
		queue('create_folder', 'bad//name');
		queue('create_folder', 'Projects');
		h.server.before = (call) => {
			if (call.kind !== 'create_folder') return;
			if (call.path === 'bad//name') throw new h.HttpError(400, 'Invalid folder path');
			throw new h.HttpError(500, 'Internal Server Error');
		};

		for (let pass = 0; pass < 3; pass++) expect(await reconnect()).toBe('live');
		expect(await reconnect()).toBe('live');

		expect(h.cache.ops).toMatchObject([
			{ path: 'Projects', attempts: 0, last_error: 'Internal Server Error' }
		]);
		expect(h.syncState.pending).toBe(1);
		expect(h.syncState.lastError).toBe('create_folder Projects: Internal Server Error');

		h.server.before = null;
		expect(await reconnect()).toBe('live');

		expect(h.cache.ops).toEqual([]);
		expect(h.server.folders.has('Projects')).toBe(true);
		expect(h.syncState.lastError).toBe('');
	});

	it('reports a cache failure instead of passing for offline', async () => {
		synced('a.md', 'a\n');
		dirty('a.md', 'a\n', 'a\nedit\n');
		// tauri-plugin-sql rejects with a bare string, not an Error.
		const broken = vi
			.spyOn(h.db, 'pendingNotes')
			.mockImplementation(() => Promise.reject('no such column: last_error'));

		expect(await reconnect()).toBe('live');

		expect(engine.engineMode()).toBe('live');
		expect(engine.isOffline()).toBe(false);
		expect(h.syncState.lastError).toBe('replay: no such column: last_error');
		expect(h.cache.lastError).toContain('no such column: last_error');
		expect(row('a.md')).toMatchObject({ sync_status: 'dirty' });

		broken.mockRestore();
		expect(await reconnect()).toBe('live');

		expect(h.server.notes.get('a.md')).toBe('a\nedit\n');
		expect(h.syncState.lastError).toBe('');
	});

	it('records a push that failed on one note and pushes the rest', async () => {
		synced('a.md', 'a\n');
		dirty('a.md', 'a\n', 'a\nedit\n');
		synced('b.md', 'b\n');
		dirty('b.md', 'b\n', 'b\nedit\n');
		h.server.before = (call) => {
			if (call.kind === 'save' && call.path === 'a.md')
				throw new h.HttpError(500, 'Internal Server Error');
		};

		expect(await reconnect()).toBe('live');

		expect(h.server.notes.get('b.md')).toBe('b\nedit\n');
		expect(row('a.md')).toMatchObject({ sync_status: 'dirty' });
		expect(h.syncState.pending).toBe(1);
		expect(h.syncState.lastError).toBe('push a.md: Internal Server Error');

		// The save lands, but the cache fails to take the answer.
		h.server.before = null;
		const adopt = vi
			.spyOn(h.db, 'adoptServer')
			.mockImplementationOnce(() => Promise.reject('database is locked'));
		expect(await reconnect()).toBe('live');
		adopt.mockRestore();

		expect(h.server.notes.get('a.md')).toBe('a\nedit\n');
		expect(row('a.md')).toMatchObject({ sync_status: 'dirty' });
		expect(h.syncState.lastError).toBe('push a.md: database is locked');

		expect(await reconnect()).toBe('live');
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: 'a\nedit\n' });
		expect(h.syncState.lastError).toBe('');
	});

	it('follows manifest removals for synced rows and keeps pending ones', async () => {
		synced('here.md', 'here\n');
		put('gone.md', { content: 'gone\n', base_content: 'gone\n', base_revision: h.rev('gone\n') });
		dirty('kept.md', 'kept\n', 'kept\nedit\n');
		// The push of the pending row fails (server hiccup), so it is still
		// pending when the manifest says the note is gone.
		h.server.before = (call) => {
			if (call.path === 'kept.md') throw new h.HttpError(500, 'boom');
		};

		expect(await reconnect()).toBe('live');

		expect(row('gone.md')).toBeUndefined();
		expect(events).toContainEqual({
			type: 'exscalia:live',
			detail: { type: 'note_removed', path: 'gone.md' }
		});
		expect(row('kept.md')).toMatchObject({ sync_status: 'dirty', content: 'kept\nedit\n' });
		expect(row('here.md')).toMatchObject({ sync_status: 'synced' });
	});

	it('applies a live event that arrives mid-replay only once live', async () => {
		synced('n.md', 'v1\n');
		let modeAtEvent = '';
		h.server.after = (call) => {
			if (call.kind !== 'manifest') return;
			// Another device saves after the manifest was taken.
			h.server.notes.set('n.md', 'v2\n');
			modeAtEvent = engine.engineMode();
			h.live.onEvent!({ type: 'note', path: 'n.md', revision: h.rev('v2\n') });
		};
		let contentWhenLive: string | undefined;
		window.addEventListener('exscalia:sync-mode', (e) => {
			if ((e as CustomEvent).detail === 'live') contentWhenLive = row('n.md')?.content;
		});

		expect(await reconnect()).toBe('live');

		expect(modeAtEvent).toBe('replaying');
		expect(contentWhenLive).toBe('v1\n');
		expect(row('n.md')).toMatchObject({ content: 'v2\n', base_revision: h.rev('v2\n') });
		const liveAt = events.findIndex((e) => e.type === 'exscalia:sync-mode' && e.detail === 'live');
		const appliedAt = events.findIndex(
			(e) =>
				e.type === 'exscalia:live' && (e.detail as LiveNoteEvent).path === 'n.md'
		);
		expect(appliedAt).toBeGreaterThan(liveAt);
	});

	it('runs exactly one more pass for reconnects that land mid-replay', async () => {
		synced('a.md', 'a\n');
		const held = gate();
		let holding = true;
		h.server.hold = (call) => (call.kind === 'manifest' && holding ? held.opened : undefined);

		const first = reconnect();
		await settle();
		expect(manifests()).toBe(1);
		// The stream dropped and came back, twice, while the manifest was out.
		for (const state of ['offline', 'connecting', 'live', 'offline', 'connecting', 'live']) {
			h.live.onState!(state as LiveState);
		}
		holding = false;
		held.open();
		await first;
		await settle();

		expect(manifests()).toBe(2);
		expect(engine.engineMode()).toBe('live');
	});

	it('does not report live when the stream dropped during the pass', async () => {
		const held = gate();
		h.server.hold = (call) => (call.kind === 'manifest' ? held.opened : undefined);

		h.live.onState!('live');
		await settle();
		h.live.onState!('offline');
		held.open();
		await settle();

		expect(manifests()).toBe(1);
		expect(engine.engineMode()).toBe('offline');
	});
});

describe('live events', () => {
	const liveTypes = () =>
		events
			.filter((e) => e.type === 'exscalia:live')
			.map((e) => (e.detail as LiveNoteEvent).type);

	/** A REST rename on another device: the server sends the move, then the
	 * removal and the new note for older clients. */
	function renameOnServer(from: string, to: string) {
		h.server.notes.set(to, h.server.notes.get(from)!);
		h.server.notes.delete(from);
	}
	function sendRename(from: string, to: string) {
		h.live.onEvent!({ type: 'note_moved', path: from, new_path: to });
		h.live.onEvent!({ type: 'note_removed', path: from });
		h.live.onEvent!({ type: 'note', path: to, revision: h.rev(h.server.notes.get(to)!) });
	}

	it('re-broadcasts a rename in wire order while the move waits on its fetch', async () => {
		synced('old.md', 'body\n');
		expect(await reconnect()).toBe('live');
		renameOnServer('old.md', 'new.md');
		const held = gate();
		h.server.hold = (call) =>
			call.kind === 'read' && call.path === 'new.md' ? held.opened : undefined;
		events = [];

		sendRename('old.md', 'new.md');
		await settle();
		// The removal must not overtake the move and read as a deletion.
		expect(liveTypes()).toEqual([]);
		held.open();
		await settle();

		expect(liveTypes()).toEqual(['note_moved', 'note_removed', 'note']);
		expect(row('old.md')).toBeUndefined();
		expect(row('new.md')).toMatchObject({ sync_status: 'synced', content: 'body\n' });
	});

	it('broadcasts events buffered during a pass ahead of those that follow it', async () => {
		synced('old.md', 'body\n');
		const held = gate();
		h.server.hold = (call) =>
			call.kind === 'read' && call.path === 'new.md' ? held.opened : undefined;
		h.server.after = (call) => {
			if (call.kind !== 'manifest') return;
			h.server.after = null;
			renameOnServer('old.md', 'new.md');
			h.live.onEvent!({ type: 'note_moved', path: 'old.md', new_path: 'new.md' });
		};

		expect(await reconnect()).toBe('live');
		// The buffered move is still fetching when the rest arrives live.
		h.live.onEvent!({ type: 'note_removed', path: 'old.md' });
		h.live.onEvent!({ type: 'note', path: 'new.md', revision: h.rev('body\n') });
		await settle();
		expect(liveTypes()).toEqual([]);
		held.open();
		await settle();

		expect(liveTypes()).toEqual(['note_moved', 'note_removed', 'note']);
		expect(row('new.md')).toMatchObject({ sync_status: 'synced', content: 'body\n' });
	});
});

describe('conflicts', () => {
	const conflictEvents = () => events.filter((e) => e.type === 'exscalia:sync-conflict');

	/** Quit and launch again, against the same cache. */
	async function relaunch() {
		stop?.();
		stop = engine.startSync();
		await settle();
	}

	it('announces a conflict from an earlier launch once more, then marks it seen', async () => {
		h.cache.conflicts.push(
			{ id: 1, path: 'trip.md', conflict_path: 'trip (conflict).md', created_at: 1, seen: 0 },
			{ id: 2, path: 'old.md', conflict_path: 'old (conflict).md', created_at: 2, seen: 1 }
		);

		await relaunch();

		expect(conflictEvents()).toEqual([
			{
				type: 'exscalia:sync-conflict',
				detail: { path: 'trip.md', conflictPath: 'trip (conflict).md' }
			}
		]);
		expect(h.cache.conflicts.map((c) => c.seen)).toEqual([1, 1]);

		await relaunch();
		expect(conflictEvents()).toHaveLength(1);
	});

	it('toasts a new conflict as it happens and again at the next launch only', async () => {
		h.server.notes.set('trip.md', THEIRS);
		dirty('trip.md', BASE, MINE);

		expect(await reconnect()).toBe('live');
		expect(conflictEvents()).toHaveLength(1);

		await relaunch();
		expect(conflictEvents()).toHaveLength(2);
		expect(conflictEvents()[1].detail).toEqual({
			path: 'trip.md',
			conflictPath: 'trip (conflict).md'
		});

		await relaunch();
		expect(conflictEvents()).toHaveLength(2);
	});
});

describe('isNetworkError', () => {
	it('means a lost connection, not a server answer or a cache fault', () => {
		expect(engine.isNetworkError(new TypeError('Load failed'))).toBe(true);
		expect(engine.isNetworkError(new h.HttpError(0, 'Offline'))).toBe(true);
		expect(engine.isNetworkError(new h.HttpError(500, 'Internal Server Error'))).toBe(false);
		expect(engine.isNetworkError(new h.HttpError(404, 'Note not found'))).toBe(false);
		expect(engine.isNetworkError('no such column: last_error')).toBe(false);
		expect(engine.isNetworkError(new SyntaxError('Unexpected token <'))).toBe(false);
		expect(engine.isNetworkError(undefined)).toBe(false);
	});
});

describe('without a cache', () => {
	it('relays the stream and applies nothing', async () => {
		stop?.();
		h.cache.open = false;
		events = [];
		stop = engine.startSync();
		await settle();

		h.live.onEvent!({ type: 'note', path: 'a.md', revision: 'rev-remote' });
		engine.requestReplay();
		await settle();

		expect(events).toEqual([
			{ type: 'exscalia:live', detail: { type: 'note', path: 'a.md', revision: 'rev-remote' } }
		]);
		expect(h.server.calls).toEqual([]);
		expect(h.live.onState).toBeNull();
		expect(h.live.kick).not.toHaveBeenCalled();
	});
});

describe('pushPath', () => {
	it('pushes a note that fell back to the cache while live', async () => {
		synced('a.md', 'a\n');
		synced('b.md', 'b\n');
		expect(await reconnect()).toBe('live');
		// A save hit 'Load failed' (a server 500 on desktop) and landed locally.
		await h.db.writeDirty('a.md', 'a\noffline\n', h.rev('a\noffline\n'));
		events = [];

		await engine.pushPath('a.md');
		await engine.pushPath('b.md');

		expect(saves('a.md')).toEqual([
			expect.objectContaining({ base: h.rev('a\n'), content: 'a\noffline\n', outcome: 'saved' })
		]);
		expect(saves('b.md')).toEqual([]);
		expect(h.server.notes.get('a.md')).toBe('a\noffline\n');
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: 'a\noffline\n' });
		expect(events).toContainEqual({
			type: 'exscalia:live',
			detail: { type: 'note', path: 'a.md', revision: h.rev('a\noffline\n') }
		});
		expect(h.syncState.pending).toBe(0);
		expect(engine.engineMode()).toBe('live');
	});

	it('waits for an in-flight replay and pushes a dirty row once', async () => {
		synced('a.md', 'a\n');
		dirty('a.md', 'a\n', 'a\noffline\n');
		const held = gate();
		h.server.hold = (call) => (call.kind === 'save' ? held.opened : undefined);

		const replay = reconnect();
		await settle();
		expect(engine.engineMode()).toBe('replaying');
		expect(saves('a.md')).toHaveLength(1);

		let pushed = false;
		const push = engine.pushPath('a.md').then(() => (pushed = true));
		await settle();
		expect(pushed).toBe(false);
		expect(saves('a.md')).toHaveLength(1);

		held.open();
		expect(await replay).toBe('live');
		await push;

		expect(saves('a.md')).toEqual([
			expect.objectContaining({ content: 'a\noffline\n', outcome: 'saved' })
		]);
		expect(h.server.notes.get('a.md')).toBe('a\noffline\n');
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: 'a\noffline\n' });
	});

	it('holds a replay back until an on-demand push lands', async () => {
		synced('a.md', 'a\n');
		expect(await reconnect()).toBe('live');
		await h.db.writeDirty('a.md', 'a\noffline\n', h.rev('a\noffline\n'));
		h.server.calls = [];
		const held = gate();
		h.server.hold = (call) => (call.kind === 'save' ? held.opened : undefined);

		const push = engine.pushPath('a.md');
		await settle();
		// A wake reconnect while the push is out: its pending snapshot would
		// still hold the row and push it a second time.
		const replay = reconnect();
		await settle();
		expect(h.server.calls.some((c) => c.kind === 'manifest')).toBe(false);

		held.open();
		await push;
		expect(await replay).toBe('live');

		expect(saves('a.md')).toHaveLength(1);
		expect(h.server.notes.get('a.md')).toBe('a\noffline\n');
	});

	it('replays queued ops before the row, so a pending rename lands first', async () => {
		h.server.notes.set('old.md', 'v1\n');
		expect(await reconnect()).toBe('live');
		h.server.calls = [];
		// Renamed while a request failed, then edited: the row already lives
		// at the new path and the rename waits in the queue.
		put('new.md', {
			content: 'v1\nedited\n',
			revision: h.rev('v1\nedited\n'),
			base_content: 'v1\n',
			base_revision: h.rev('v1\n'),
			sync_status: 'dirty'
		});
		h.cache.ops.push({
			id: h.cache.nextOpId++,
			op: 'rename',
			path: 'old.md',
			new_path: 'new.md',
			payload_json: JSON.stringify({ new_title: null }),
			created_at: 0,
			attempts: 0,
			last_error: null
		});

		await engine.pushPath('new.md');

		expect(h.server.calls.map((c) => c.kind)).toEqual(['rename', 'save']);
		expect([...h.server.notes.entries()]).toEqual([['new.md', 'v1\nedited\n']]);
		expect(h.cache.ops).toEqual([]);
		expect(row('new.md')).toMatchObject({ sync_status: 'synced' });
	});

	it('rejects on a network failure and leaves the row pending', async () => {
		synced('a.md', 'a\n');
		expect(await reconnect()).toBe('live');
		await h.db.writeDirty('a.md', 'a\noffline\n', h.rev('a\noffline\n'));
		h.server.before = (call) => {
			if (call.kind === 'save') throw new TypeError('Load failed');
		};

		await expect(engine.pushPath('a.md')).rejects.toThrow('Load failed');

		expect(row('a.md')).toMatchObject({ sync_status: 'dirty', content: 'a\noffline\n' });
		expect(h.server.notes.get('a.md')).toBe('a\n');
	});
});

describe('liveness while the stream stays up', () => {
	const onAuthExpired = vi.fn();

	beforeEach(() => {
		// Restarted under fake timers, so its retry and push timers are fake too.
		stop?.();
		vi.useFakeTimers();
		onAuthExpired.mockReset();
		stop = engine.startSync({ onAuthExpired });
	});

	afterEach(() => {
		stop?.();
		stop = null;
		vi.useRealTimers();
	});

	/** Move the clock, then let whatever the timers started finish. */
	async function advance(ms: number) {
		await vi.advanceTimersByTimeAsync(ms);
		await settle();
	}

	it('stays live on a server error and retries the replay after 5s', async () => {
		synced('a.md', 'a\n');
		h.server.manifestStatus = 500;
		h.server.after = (call) => {
			if (call.kind !== 'manifest') return;
			// Another device saves while the failing pass is out.
			h.server.notes.set('a.md', 'a\nremote\n');
			h.live.onEvent!({ type: 'note', path: 'a.md', revision: h.rev('a\nremote\n') });
		};

		expect(await reconnect()).toBe('live');

		expect(engine.isOffline()).toBe(false);
		expect(h.cache.lastError).toContain('manifest 500');
		expect(manifests()).toBe(1);
		// The event buffered during the pass is applied, not stranded.
		expect(row('a.md')).toMatchObject({ content: 'a\nremote\n', sync_status: 'synced' });
		expect(events).toContainEqual({
			type: 'exscalia:live',
			detail: { type: 'note', path: 'a.md', revision: h.rev('a\nremote\n') }
		});
		h.server.after = null;

		// Meanwhile another device added a note the failed pass never saw.
		h.server.manifestStatus = 200;
		h.server.notes.set('b.md', 'from the phone\n');
		await advance(4_999);
		expect(manifests()).toBe(1);
		await advance(1);

		expect(manifests()).toBe(2);
		expect(row('b.md')).toMatchObject({ sync_status: 'synced', content: 'from the phone\n' });
		expect(engine.engineMode()).toBe('live');
	});

	it('backs off 5s, 15s, then every 60s, and starts over after a success', async () => {
		h.server.manifestStatus = 500;
		expect(await reconnect()).toBe('live');

		const at: number[] = [];
		for (const ms of [5_000, 15_000, 60_000, 60_000]) {
			await advance(ms - 1);
			at.push(manifests());
			await advance(1);
			at.push(manifests());
		}
		expect(at).toEqual([1, 2, 2, 3, 3, 4, 4, 5]);

		h.server.manifestStatus = 200;
		await advance(60_000);
		expect(manifests()).toBe(6);
		await advance(120_000);
		expect(manifests()).toBe(6);

		h.server.manifestStatus = 500;
		expect(await reconnect()).toBe('live');
		expect(manifests()).toBe(7);
		await advance(5_000);
		expect(manifests()).toBe(8);
	});

	it('goes offline on a network error but still retries while the stream is up', async () => {
		// A 500 on desktop surfaces as 'Load failed' (HANDOFF §7).
		h.server.before = (call) => {
			if (call.kind === 'manifest') throw new TypeError('Load failed');
		};

		expect(await reconnect()).toBe('offline');
		expect(h.cache.lastError).toContain('Load failed');

		h.server.before = null;
		await advance(5_000);

		expect(manifests()).toBe(2);
		expect(engine.engineMode()).toBe('live');
	});

	it('drops the retry when the stream goes down; the reconnect replays instead', async () => {
		h.server.manifestStatus = 500;
		expect(await reconnect()).toBe('live');
		h.live.onState!('offline');

		await advance(120_000);

		expect(manifests()).toBe(1);
		expect(engine.engineMode()).toBe('offline');
	});

	it('treats a 401 from the manifest as an expired session, not an outage', async () => {
		h.server.manifestStatus = 401;

		h.live.onState!('live');
		await settle();

		expect(onAuthExpired).toHaveBeenCalledTimes(1);
		expect(engine.engineMode()).not.toBe('offline');
		await advance(180_000);
		expect(onAuthExpired).toHaveBeenCalledTimes(1);
		expect(manifests()).toBe(1);
	});

	it('pushes a row that fell back to the cache while live, with no reconnect', async () => {
		synced('a.md', 'a\n');
		expect(await reconnect()).toBe('live');
		const modeEvents = () => events.filter((e) => e.type === 'exscalia:sync-mode').length;
		const before = modeEvents();

		// What offline-api's localSave does when a save hits a dead request.
		await h.db.writeDirty('a.md', 'a\nlocal\n', h.rev('a\nlocal\n'));
		await engine.refreshPendingBadge();
		engine.schedulePush();
		// A second save in the same burst folds into one push.
		engine.schedulePush();
		await advance(1_499);
		expect(saves('a.md')).toEqual([]);
		await advance(1);

		expect(saves('a.md')).toEqual([
			expect.objectContaining({ base: h.rev('a\n'), content: 'a\nlocal\n', outcome: 'saved' })
		]);
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: 'a\nlocal\n' });
		expect(h.syncState.pending).toBe(0);
		expect(modeEvents()).toBe(before);
		expect(manifests()).toBe(1);
	});

	it('pushes the clean merge a live event leaves in a pending row', async () => {
		const base = 'alpha\nbeta\ngamma\n';
		const mine = 'alpha edited here\nbeta\ngamma\n';
		const theirs = 'alpha\nbeta\ngamma edited there\n';
		const merged = 'alpha edited here\nbeta\ngamma edited there\n';
		synced('a.md', base);
		expect(await reconnect()).toBe('live');
		await h.db.writeDirty('a.md', mine, h.rev(mine));

		h.server.notes.set('a.md', theirs);
		h.live.onEvent!({ type: 'note', path: 'a.md', revision: h.rev(theirs) });
		await settle();
		expect(row('a.md')).toMatchObject({ sync_status: 'dirty', content: merged });

		await advance(1_500);

		expect(h.server.notes.get('a.md')).toBe(merged);
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: merged });
	});

	it('keeps text written while a push is out pending on top of what it left', async () => {
		synced('a.md', 'a\n');
		expect(await reconnect()).toBe('live');
		await h.db.writeDirty('a.md', 'a\none\n', h.rev('a\none\n'));
		h.server.before = (call) => {
			if (call.kind !== 'save') return;
			h.server.before = null;
			// The editor's next save fell back to the cache mid-push.
			void h.db.writeDirty('a.md', 'a\none\ntwo\n', h.rev('a\none\ntwo\n'));
		};
		events = [];

		await engine.pushPath('a.md');

		expect(h.server.notes.get('a.md')).toBe('a\none\n');
		expect(row('a.md')).toMatchObject({
			sync_status: 'dirty',
			content: 'a\none\ntwo\n',
			base_content: 'a\none\n',
			base_revision: h.rev('a\none\n')
		});
		// No 'note' event: the page would pull and replace its text.
		expect(events.filter((e) => e.type === 'exscalia:live')).toEqual([]);

		await advance(1_500);

		expect(saves('a.md').map((c) => [c.base, c.outcome])).toEqual([
			[h.rev('a\n'), 'saved'],
			[h.rev('a\none\n'), 'saved']
		]);
		expect(h.server.notes.get('a.md')).toBe('a\none\ntwo\n');
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: 'a\none\ntwo\n' });
	});

	it('turns a create overtaken by a newer write into an edit of the created note', async () => {
		expect(await reconnect()).toBe('live');
		put('n.md', { content: 'first\n', revision: h.rev('first\n'), sync_status: 'new' });
		h.server.before = (call) => {
			if (call.kind !== 'create') return;
			h.server.before = null;
			void h.db.writeDirty('n.md', 'first\nsecond\n', h.rev('first\nsecond\n'));
		};

		await engine.pushPath('n.md');

		expect(row('n.md')).toMatchObject({
			sync_status: 'dirty',
			content: 'first\nsecond\n',
			base_revision: h.rev('first\n')
		});

		await advance(1_500);

		const calls = h.server.calls.filter((c) => c.path === 'n.md');
		expect(calls.map((c) => [c.kind, c.outcome])).toEqual([
			['create', 'saved'],
			['save', 'saved']
		]);
		expect([...h.server.notes.entries()]).toEqual([['n.md', 'first\nsecond\n']]);
		expect(row('n.md')).toMatchObject({ sync_status: 'synced', content: 'first\nsecond\n' });
	});

	it('keeps the old base when the push that was overtaken was a merge', async () => {
		const base = 'alpha\nbeta\ngamma\n';
		const mine = 'alpha edited here\nbeta\ngamma\n';
		const theirs = 'alpha\nbeta\ngamma edited there\n';
		const merged = 'alpha edited here\nbeta\ngamma edited there\n';
		const more = mine + 'delta\n';
		synced('a.md', base);
		expect(await reconnect()).toBe('live');
		await h.db.writeDirty('a.md', mine, h.rev(mine));
		h.server.notes.set('a.md', theirs);
		h.server.before = (call) => {
			if (call.kind !== 'save' || call.content !== merged) return;
			h.server.before = null;
			void h.db.writeDirty('a.md', more, h.rev(more));
		};

		await engine.pushPath('a.md');

		// `more` is not an edit of the merged text, so that is no base for it.
		expect(row('a.md')).toMatchObject({
			sync_status: 'dirty',
			content: more,
			base_revision: h.rev(base)
		});

		await advance(1_500);

		expect(h.server.notes.get('a.md')).toBe(merged + 'delta\n');
		expect(row('a.md')).toMatchObject({ sync_status: 'synced', content: merged + 'delta\n' });
		expect(h.cache.conflicts).toEqual([]);
	});

	it('leaves text written while a conflict copy was parked pending', async () => {
		synced('trip.md', BASE);
		expect(await reconnect()).toBe('live');
		await h.db.writeDirty('trip.md', MINE, h.rev(MINE));
		h.server.notes.set('trip.md', THEIRS);
		const later = MINE + 'Bring the tickets.\n';
		h.server.before = (call) => {
			if (call.kind !== 'save' || call.onConflict !== 'copy') return;
			h.server.before = null;
			void h.db.writeDirty('trip.md', later, h.rev(later));
		};

		await engine.pushPath('trip.md');

		expect(h.server.notes.get('trip (conflict).md')).toBe(MINE);
		expect(row('trip.md')).toMatchObject({
			sync_status: 'dirty',
			content: later,
			base_revision: h.rev(BASE)
		});
	});

	it('pushes pending rows on the 60s interval', async () => {
		synced('a.md', 'a\n');
		expect(await reconnect()).toBe('live');
		// Pending, but nothing scheduled a push (a failed one, or a queued op).
		await h.db.writeDirty('a.md', 'a\nlocal\n', h.rev('a\nlocal\n'));
		await engine.refreshPendingBadge();

		await advance(60_000 + 1_499);
		expect(saves('a.md')).toEqual([]);
		await advance(1);

		expect(h.server.notes.get('a.md')).toBe('a\nlocal\n');
		expect(h.syncState.pending).toBe(0);
		await advance(120_000);
		expect(saves('a.md')).toHaveLength(1);
	});

	it('does not repeat the refused-note toast on every interval push', async () => {
		put('legacy.txt', {
			content: 'old\n',
			base_content: 'older\n',
			base_revision: h.rev('older\n'),
			sync_status: 'dirty'
		});
		expect(await reconnect()).toBe('live');
		expect(h.notify).toHaveBeenCalledTimes(1);

		await advance(3 * 60_000);
		expect(saves('legacy.txt').length).toBeGreaterThan(1);
		expect(h.notify).toHaveBeenCalledTimes(1);

		expect(await reconnect()).toBe('live');
		expect(h.notify).toHaveBeenCalledTimes(2);
	});

	it('does not push while offline', async () => {
		synced('a.md', 'a\n');
		expect(await reconnect()).toBe('live');
		h.live.onState!('offline');
		await h.db.writeDirty('a.md', 'a\nlocal\n', h.rev('a\nlocal\n'));
		await engine.refreshPendingBadge();

		engine.schedulePush();
		await advance(120_000);

		expect(saves('a.md')).toEqual([]);
	});

	it('reconnects at once when the network comes back', async () => {
		expect(await reconnect()).toBe('live');

		window.dispatchEvent(new Event('online'));

		expect(h.live.kick).toHaveBeenCalledTimes(1);
	});

	it('on focus, kicks a stream that is down but leaves a healthy one alone', async () => {
		expect(await reconnect()).toBe('live');
		document.dispatchEvent(new Event('visibilitychange'));
		expect(h.live.kick).not.toHaveBeenCalled();
		expect(manifests()).toBe(1);

		h.live.onState!('offline');
		document.dispatchEvent(new Event('visibilitychange'));
		expect(h.live.kick).toHaveBeenCalledTimes(1);
	});

	it('on focus, replays over a live stream that a failed pass left offline', async () => {
		h.server.before = (call) => {
			if (call.kind === 'manifest') throw new TypeError('Load failed');
		};
		expect(await reconnect()).toBe('offline');
		h.server.before = null;

		document.dispatchEvent(new Event('visibilitychange'));
		await settle();

		expect(h.live.kick).not.toHaveBeenCalled();
		expect(manifests()).toBe(2);
		expect(engine.engineMode()).toBe('live');
	});

	it('requestReplay replays over a live stream and reconnects a dead one', async () => {
		expect(await reconnect()).toBe('live');
		engine.requestReplay();
		await settle();
		expect(manifests()).toBe(2);
		expect(h.live.kick).not.toHaveBeenCalled();

		h.live.onState!('offline');
		engine.requestReplay();
		expect(h.live.kick).toHaveBeenCalledTimes(1);
		expect(manifests()).toBe(2);
	});

	it('clears its timers when stopped', async () => {
		h.server.manifestStatus = 500;
		expect(await reconnect()).toBe('live');
		engine.schedulePush();
		stop!();
		stop = null;

		expect(vi.getTimerCount()).toBe(0);
	});
});
