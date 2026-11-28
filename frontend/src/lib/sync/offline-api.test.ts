// The desktop wrappers installed over `api`: every wrapper must accept and
// forward every argument the network method takes (TypeScript accepts a
// narrower arrow, which is how createNote once dropped `template` and
// saveNote its `options`), and the
// offline fallbacks must render templates from the cache the way the server
// would, since the queued create replays that content verbatim. The note
// reads and saves must never let the server copy replace unsynced local text.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LocalDraft } from '../drafts';
import type { Note } from '../api';
import { mergeThreeWay } from '../merge';
import type { NoteRow, SyncStatus } from './db';

const state = vi.hoisted(() => ({
	offline: false,
	notes: new Map<string, NoteRow>(),
	ops: [] as { op: string; path: string }[],
	pushPath: vi.fn(async (_path: string) => {}),
	schedulePush: vi.fn(),
	readDraft: vi.fn(async (_path: string): Promise<LocalDraft | null> => null),
	writeDraft: vi.fn(async (_draft: LocalDraft) => {}),
	row(path: string, content: string, revision: string, sync_status: SyncStatus): NoteRow {
		return {
			path,
			name: (path.split('/').pop() ?? path).replace(/\.md$/, ''),
			pinned: 0,
			content,
			revision,
			base_content: sync_status === 'synced' ? content : '',
			base_revision: sync_status === 'synced' ? revision : null,
			meta_json: '{}',
			sync_status,
			fetched_at: null,
			updated_at: null
		};
	}
}));

// Mirrors db.ts: writeDirty keeps the base and stamps updated_at, and
// adoptServer skips a row written after `unlessUpdatedAfter`.
vi.mock('./db', () => {
	const noop = async () => {};
	return {
		getNote: async (path: string) => {
			const row = state.notes.get(path);
			return row ? { ...row } : null;
		},
		allNotes: async () => [...state.notes.values()].filter((r) => r.sync_status !== 'deleted'),
		allFolders: async () => [],
		adoptServer: async (
			path: string,
			content: string,
			revision: string,
			_meta?: unknown,
			_pinned?: boolean,
			unlessUpdatedAfter?: number
		) => {
			const at = state.notes.get(path)?.updated_at;
			if (unlessUpdatedAfter != null && at != null && at > unlessUpdatedAfter) return;
			state.notes.set(path, {
				...state.row(path, content ?? '', revision ?? '', 'synced'),
				updated_at: Date.now()
			});
		},
		insertNew: async (path: string, content: string, revision: string) => {
			state.notes.set(path, { ...state.row(path, content, revision, 'new'), updated_at: Date.now() });
		},
		writeDirty: async (path: string, content: string, revision: string) => {
			const row = state.notes.get(path);
			if (!row) return;
			state.notes.set(path, {
				...row,
				content,
				revision,
				updated_at: Date.now(),
				sync_status: row.sync_status === 'new' ? 'new' : 'dirty'
			});
		},
		enqueueOp: async (op: string, path: string) => {
			state.ops.push({ op, path });
		},
		removeNote: noop,
		markDeleted: noop,
		moveNote: noop,
		rewriteOpPaths: noop,
		setPinned: noop,
		addFolder: noop,
		renameFolderTree: noop,
		removeFolderTree: noop
	};
});

vi.mock('./engine', () => ({
	isOffline: () => state.offline,
	computeRevision: async (s: string) => 'rev-' + s.length,
	pushPath: state.pushPath,
	refreshPendingBadge: async () => {},
	schedulePush: state.schedulePush
}));

vi.mock('../drafts', () => ({ readDraft: state.readDraft, writeDraft: state.writeDraft }));

import { api, ApiError } from '../api';
import * as db from './db';
import { installOfflineApi, netApi } from './offline-api';

type Fn = (...args: unknown[]) => Promise<unknown>;
const apiFns = api as unknown as Record<string, Fn>;
const netFns = netApi as unknown as Record<string, Fn>;
/** The real api.ts methods, before any test stubs netApi. */
const original = { ...netFns };

/** How many parameters a function declares. Function.length stops at the
 * first defaulted one, so it reads saveNote's five as three. */
function declaredParams(fn: Fn): number {
	const src = fn.toString();
	let depth = 0;
	let commas = 0;
	let any = false;
	for (let i = src.indexOf('(') + 1; i < src.length; i++) {
		const c = src[i];
		if ('([{'.includes(c)) depth += 1;
		else if (')]}'.includes(c)) {
			if (depth === 0) break;
			depth -= 1;
		} else if (c === ',' && depth === 0) commas += 1;
		else if (!/\s/.test(c)) any = true;
	}
	return any ? commas + 1 : 0;
}

function cache(path: string, content: string, status: SyncStatus = 'synced') {
	state.notes.set(path, state.row(path, content, 'r', status));
}

const MEETING = [
	'---',
	'title: meeting',
	'created: 2026-01-01T00:00:00Z',
	'modified: 2026-01-01T00:00:00Z',
	'---',
	'',
	'# {{title}}',
	'',
	'Date: {{date}}',
	'Stamp: {{now}}',
	'',
	'## Attendees',
	''
].join('\n');

const DAILY = ['---', 'title: daily', '---', '', '# {{title}}', '', '## Log', ''].join('\n');

beforeEach(() => {
	state.offline = false;
	state.notes.clear();
	state.ops.length = 0;
	state.pushPath.mockReset();
	state.schedulePush.mockReset();
	state.readDraft.mockReset();
	state.readDraft.mockResolvedValue(null);
	state.writeDraft.mockReset();
	// Local noon, so the device date is 2026-09-22 in any timezone.
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(new Date(2026, 8, 22, 12, 0, 0));
});

afterEach(() => {
	Object.assign(netApi, original);
	vi.useRealTimers();
});

describe('installOfflineApi wrappers', () => {
	it('count defaulted parameters too', () => {
		expect(original.saveNote.length).toBe(3);
		expect(declaredParams(original.saveNote)).toBe(5);
		expect(declaredParams(original.listNotes)).toBe(0);
	});

	it('accept at least as many parameters as the network methods they wrap', () => {
		installOfflineApi();
		const wrapped = Object.keys(original).filter((k) => apiFns[k] !== original[k]);
		expect(wrapped).toEqual(expect.arrayContaining(['createNote', 'openDaily', 'saveNote']));
		for (const k of wrapped) {
			expect(declaredParams(apiFns[k]), k).toBeGreaterThanOrEqual(declaredParams(original[k]));
		}
	});

	it('forward every positional argument to the network method', async () => {
		installOfflineApi();
		const wrapped = Object.keys(original).filter((k) => apiFns[k] !== original[k]);
		for (const k of wrapped) {
			const fake = vi.fn(async () => ({ status: 'x', content: '', revision: '', meta: {} }));
			netFns[k] = fake;
			const args = Array.from({ length: declaredParams(original[k]) }, (_, i) => `${k}-arg${i}`);
			await apiFns[k](...args);
			expect(fake, k).toHaveBeenCalledTimes(1);
			expect(fake.mock.calls[0].slice(0, args.length), k).toEqual(args);
		}
	});

	it('passes the template through to the server when online', async () => {
		const created: Note = { path: 'meeting.md', content: 'x', meta: {}, revision: 'r1' };
		const fake = vi.fn(async () => created);
		netApi.createNote = fake;
		installOfflineApi();
		await expect(api.createNote('meeting.md', undefined, 'meeting')).resolves.toBe(created);
		expect(fake).toHaveBeenCalledWith('meeting.md', undefined, 'meeting');
		expect(state.ops).toEqual([]);
	});
});

describe('offline createNote', () => {
	beforeEach(() => {
		state.offline = true;
		installOfflineApi();
	});

	it('renders a cached template and queues the create with that content', async () => {
		cache('templates/meeting.md', MEETING);
		const note = await api.createNote('work/meeting 2.md', undefined, 'meeting');
		const expected = [
			'# meeting 2',
			'',
			'Date: 2026-09-22',
			`Stamp: ${new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')}`,
			'',
			'## Attendees',
			''
		].join('\n');
		expect(note.content).toBe(expected);
		expect(state.notes.get('work/meeting 2.md')).toMatchObject({
			content: expected,
			sync_status: 'new'
		});
		expect(state.ops).toEqual([{ op: 'create', path: 'work/meeting 2.md' }]);
	});

	it('keeps explicit content over the template', async () => {
		cache('templates/meeting.md', MEETING);
		const note = await api.createNote('meeting.md', 'mine', 'meeting');
		expect(note.content).toBe('mine');
	});

	it('creates an empty note when the template is not cached or is deleted', async () => {
		cache('templates/gone.md', MEETING, 'deleted');
		expect((await api.createNote('a.md', undefined, 'missing')).content).toBe('');
		expect((await api.createNote('b.md', undefined, 'gone')).content).toBe('');
		expect((await api.createNote('c.md', undefined, '../templates/meeting')).content).toBe('');
	});

	it('leaves a template without frontmatter intact', async () => {
		cache('templates/plain.md', '# {{title}}\n---\nbody\n');
		expect((await api.createNote('plain copy.md', undefined, 'plain')).content).toBe(
			'# plain copy\n---\nbody\n'
		);
	});
});

describe('offline openDaily', () => {
	beforeEach(() => {
		state.offline = true;
		installOfflineApi();
	});

	it('renders the cached daily template with the date as title', async () => {
		cache('templates/daily.md', DAILY);
		await expect(api.openDaily()).resolves.toEqual({
			path: 'daily/2026-09-22.md',
			created: true
		});
		expect(state.notes.get('daily/2026-09-22.md')?.content).toBe('# 2026-09-22\n\n## Log\n');
		expect(state.ops).toEqual([{ op: 'create', path: 'daily/2026-09-22.md' }]);
	});

	it('uses the requested date for an explicit day', async () => {
		cache('templates/daily.md', DAILY);
		await api.openDaily('2026-01-05');
		expect(state.notes.get('daily/2026-01-05.md')?.content).toBe('# 2026-01-05\n\n## Log\n');
	});

	it('falls back to the heading stub without a daily template', async () => {
		await api.openDaily();
		expect(state.notes.get('daily/2026-09-22.md')?.content).toBe('# 2026-09-22\n\n');
	});

	it('opens an existing daily note without rewriting it', async () => {
		cache('templates/daily.md', DAILY);
		cache('daily/2026-09-22.md', 'kept');
		await expect(api.openDaily()).resolves.toEqual({
			path: 'daily/2026-09-22.md',
			created: false
		});
		expect(state.notes.get('daily/2026-09-22.md')?.content).toBe('kept');
		expect(state.ops).toEqual([]);
	});
});

describe('unsynced local text on desktop', () => {
	const BASE = '# Trip\n\nMeet at noon.\n';
	const OFFLINE = BASE + 'Bring the tickets.\n';
	const MORE = OFFLINE + 'Leave by eleven.\n';

	// The server side of api.readNote/saveNote: content-addressed revisions,
	// and save_note's staleness gate.
	const server = new Map<string, string>();
	let fail: Error | null = null;
	const srvRev = (text: string) => `srv-${text.length}-${text.split('\n').length}`;
	const serverNote = (path: string, content: string): Note => ({
		path,
		content,
		meta: {},
		revision: srvRev(content),
		status: 'saved'
	});
	let reads = 0;

	/** The engine's push, with one merge round when the server moved
	 * (engine.test.ts covers the rest). */
	async function enginePush(path: string) {
		const row = state.notes.get(path);
		if (!row || (row.sync_status !== 'dirty' && row.sync_status !== 'new')) return;
		let result = await netApi.saveNote(path, row.content, row.base_revision, 'reject');
		if (result.status === 'stale') {
			const merged = mergeThreeWay(row.base_content, row.content, result.content);
			if (!merged.ok) return;
			result = await netApi.saveNote(path, merged.text, result.revision, 'reject');
		}
		if (result.status === 'saved') {
			await db.adoptServer(path, result.content, result.revision, result.meta);
		}
	}

	beforeEach(() => {
		server.clear();
		fail = null;
		reads = 0;
		netApi.readNote = async (path) => {
			reads += 1;
			if (fail) throw fail;
			const content = server.get(path);
			if (content === undefined) throw new ApiError(404, `Note not found: ${path}`);
			return serverNote(path, content);
		};
		netApi.saveNote = async (path, content, baseRevision, onConflict = 'copy') => {
			if (fail) throw fail;
			const existing = server.get(path);
			if (existing === undefined) throw new ApiError(404, `Note not found: ${path}`);
			if (baseRevision != null && baseRevision !== srvRev(existing)) {
				if (onConflict === 'reject') return { ...serverNote(path, existing), status: 'stale' };
				throw new Error('conflict copies are out of scope here');
			}
			server.set(path, content);
			return serverNote(path, content);
		};
		installOfflineApi();
	});

	/** A note the server and the cache agree on. */
	async function synced(path: string, content: string) {
		server.set(path, content);
		await db.adoptServer(path, content, srvRev(content), {});
	}

	/** A save while live that hits 'Load failed' (a server 500 on desktop). */
	async function fallbackSave(path: string, content: string, baseRevision: string) {
		fail = new TypeError('Load failed');
		const result = await api.saveNote(path, content, baseRevision, 'reject');
		fail = null;
		return result;
	}

	it('a fallback save keeps the server revision, not the local sha', async () => {
		await synced('trip.md', BASE);

		const result = await fallbackSave('trip.md', OFFLINE, srvRev(BASE));

		expect(result).toMatchObject({ status: 'saved', content: OFFLINE, revision: srvRev(BASE) });
		expect(state.notes.get('trip.md')).toMatchObject({
			content: OFFLINE,
			sync_status: 'dirty',
			base_revision: srvRev(BASE)
		});
	});

	it('asks the engine to push a fallback save made while live, not one made offline', async () => {
		await synced('trip.md', BASE);

		await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		expect(state.schedulePush).toHaveBeenCalledTimes(1);

		// Offline the reconnect's replay pushes it; nothing to schedule.
		state.offline = true;
		await api.saveNote('trip.md', MORE, srvRev(BASE), 'reject');
		expect(state.schedulePush).toHaveBeenCalledTimes(1);
		expect(state.notes.get('trip.md')).toMatchObject({ content: MORE, sync_status: 'dirty' });
	});

	it('the same editor saving again lands the fallback text', async () => {
		await synced('trip.md', BASE);
		const fell = await fallbackSave('trip.md', OFFLINE, srvRev(BASE));

		const next = await api.saveNote('trip.md', MORE, fell.revision, 'reject');

		expect(next.status).toBe('saved');
		expect(server.get('trip.md')).toBe(MORE);
		expect(state.notes.get('trip.md')).toMatchObject({ content: MORE, sync_status: 'synced' });
	});

	it('pushes the fallback text before the editor saves over a moved server copy', async () => {
		await synced('trip.md', BASE);
		const fell = await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		// Another device edits while the editor keeps typing.
		server.set('trip.md', BASE.replace('noon', 'one'));
		state.pushPath.mockImplementation(enginePush);

		// The editor's next save and its stale merge, as notesession runs
		// them: the fallback's answer is its base.
		const stale = await api.saveNote('trip.md', MORE, fell.revision, 'reject');
		expect(state.pushPath).toHaveBeenCalledWith('trip.md');
		expect(stale.status).toBe('stale');
		const merged = mergeThreeWay(fell.content, MORE, stale.content);
		expect(merged.ok).toBe(true);
		const saved = await api.saveNote('trip.md', merged.ok ? merged.text : '', stale.revision, 'reject');

		expect(saved.status).toBe('saved');
		const text = server.get('trip.md');
		expect(text).toContain('Meet at one.');
		expect(text).toContain('Bring the tickets.');
		expect(text).toContain('Leave by eleven.');
		expect(state.notes.get('trip.md')).toMatchObject({ content: text, sync_status: 'synced' });
	});

	it('saves straight through without a push when the row is synced', async () => {
		await synced('trip.md', BASE);

		await api.saveNote('trip.md', OFFLINE, srvRev(BASE), 'reject');

		expect(state.pushPath).not.toHaveBeenCalled();
		expect(server.get('trip.md')).toBe(OFFLINE);
	});

	it('falls back to the cache when the push first hits a dead connection', async () => {
		await synced('trip.md', BASE);
		const fell = await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		state.pushPath.mockRejectedValue(new TypeError('Load failed'));

		const result = await api.saveNote('trip.md', MORE, fell.revision, 'reject');

		expect(result).toMatchObject({ status: 'saved', content: MORE, revision: srvRev(BASE) });
		expect(server.get('trip.md')).toBe(BASE);
		expect(state.notes.get('trip.md')).toMatchObject({ content: MORE, sync_status: 'dirty' });
	});

	it('fallback, reopen, save: the offline sentence reaches the server', async () => {
		await synced('trip.md', BASE);
		await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		state.pushPath.mockImplementation(enginePush);

		// Reopen (the preloader and +page.ts both read through here).
		const reopened = await api.readNote('trip.md');

		expect(state.pushPath).toHaveBeenCalledWith('trip.md');
		expect(reads).toBe(0);
		expect(reopened).toMatchObject({ content: OFFLINE, revision: srvRev(OFFLINE) });
		expect(state.notes.get('trip.md')).toMatchObject({ sync_status: 'synced' });

		// The editor's next autosave builds on what it was handed.
		const saved = await api.saveNote('trip.md', MORE, reopened.revision, 'reject');
		expect(saved.status).toBe('saved');
		expect(server.get('trip.md')).toBe(MORE);
		expect(server.get('trip.md')).toContain('Bring the tickets.');
		expect(state.writeDraft).not.toHaveBeenCalled();
	});

	it('opens the server copy and offers the local text as a draft when the push cannot land', async () => {
		await synced('trip.md', BASE);
		await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		const THEIRS = BASE + 'Changed on the phone.\n';
		server.set('trip.md', THEIRS);
		// Three stale rounds: the engine leaves the row pending.
		state.pushPath.mockResolvedValue(undefined);

		const opened = await api.readNote('trip.md');

		expect(opened).toMatchObject({ content: THEIRS, revision: srvRev(THEIRS) });
		expect(state.writeDraft).toHaveBeenCalledTimes(1);
		// With its base text, so recovery merges onto theirs (or asks)
		// instead of saving the local text over them.
		expect(state.writeDraft.mock.calls[0][0]).toMatchObject({
			path: 'trip.md',
			content: OFFLINE,
			baseRevision: srvRev(BASE),
			baseContent: BASE
		});
		expect(state.notes.get('trip.md')).toMatchObject({ content: OFFLINE, sync_status: 'dirty' });
	});

	it('treats a push the server refused like one that could not land', async () => {
		await synced('trip.md', BASE);
		await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		state.pushPath.mockRejectedValue(new ApiError(500, 'Internal Server Error'));

		const opened = await api.readNote('trip.md');

		expect(opened.content).toBe(BASE);
		expect(state.writeDraft.mock.calls[0][0]).toMatchObject({ content: OFFLINE });
		expect(state.notes.get('trip.md')).toMatchObject({ sync_status: 'dirty' });
	});

	it('keeps a draft the editor already holds', async () => {
		await synced('trip.md', BASE);
		await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		state.readDraft.mockResolvedValue({
			path: 'trip.md',
			owner: 'editor',
			content: MORE,
			baseRevision: srvRev(BASE),
			updatedAt: 1
		});

		await api.readNote('trip.md');

		expect(state.writeDraft).not.toHaveBeenCalled();
	});

	it('serves the cache with the base revision when the push hits a dead connection', async () => {
		await synced('trip.md', BASE);
		await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		state.pushPath.mockRejectedValue(new TypeError('Load failed'));

		const opened = await api.readNote('trip.md');

		expect(reads).toBe(0);
		expect(opened).toMatchObject({ content: OFFLINE, revision: srvRev(BASE) });
		expect(state.notes.get('trip.md')).toMatchObject({ content: OFFLINE, sync_status: 'dirty' });
	});

	it('reads a cached row with the revision the server issued', async () => {
		await synced('trip.md', BASE);
		await fallbackSave('trip.md', OFFLINE, srvRev(BASE));
		await db.insertNew('draft.md', 'offline only', 'rev-local');
		state.offline = true;

		expect(await api.readNote('trip.md')).toMatchObject({
			content: OFFLINE,
			revision: srvRev(BASE)
		});
		expect((await api.readNote('draft.md')).revision).toBe('');
		expect(state.pushPath).not.toHaveBeenCalled();
	});

	it('still adopts the server copy of a synced row without pushing', async () => {
		await synced('trip.md', BASE);
		server.set('trip.md', MORE);

		const opened = await api.readNote('trip.md');

		expect(opened.content).toBe(MORE);
		expect(state.pushPath).not.toHaveBeenCalled();
		expect(state.notes.get('trip.md')).toMatchObject({ content: MORE, sync_status: 'synced' });
	});

	it('leaves a row the engine made dirty while the save was out', async () => {
		await synced('trip.md', BASE);
		const save = netApi.saveNote;
		netApi.saveNote = async (...args) => {
			// A live event's merge lands in the row mid-request.
			vi.setSystemTime(Date.now() + 1000);
			await db.writeDirty('trip.md', OFFLINE, 'rev-local');
			return save(...args);
		};

		const result = await api.saveNote('trip.md', MORE, srvRev(BASE), 'reject');

		expect(result.status).toBe('saved');
		expect(state.notes.get('trip.md')).toMatchObject({
			content: OFFLINE,
			sync_status: 'dirty',
			base_revision: srvRev(BASE)
		});
	});
});
