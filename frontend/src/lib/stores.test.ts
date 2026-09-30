// Saving the sidebar arrangement: optimistic apply, whole-list PUT, rollback on
// error. The interesting cases are the unhappy ones — a save that fails while
// the re-read fails too (offline), and two saves whose replies cross. Also the
// per-path bookkeeping that follows notes when they move or go to the Trash.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const stub = vi.hoisted(() => ({
	noteOrder: vi.fn(),
	saveNoteOrder: vi.fn(),
	folderOrder: vi.fn(),
	saveFolderOrder: vi.fn(),
	listNotes: vi.fn(),
	listFolders: vi.fn(),
	noteThread: vi.fn(),
	bindNoteThread: vi.fn(),
	getThread: vi.fn(),
	createThread: vi.fn(),
	listThreads: vi.fn(async () => ({ threads: [] }))
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

// Drafts live in IndexedDB, which node does not have.
const drafts = vi.hoisted(() => ({
	moveDraft: vi.fn(async () => {}),
	removeAllDrafts: vi.fn(async () => {})
}));
vi.mock('./drafts', () => drafts);

// Thread pointers, recents and tabs persist to localStorage.
const storage = new Map<string, string>();
vi.stubGlobal('localStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

const {
	vault,
	saveNoteOrder,
	saveFolderOrder,
	refreshNoteOrder,
	refreshNotes,
	afterPathsMoved,
	afterPathsRemoved,
	ensureNoteThread,
	forgetNoteThread,
	moveNoteThread,
	noteChatFor,
	recentNotes,
	rememberNote,
	restoreNoteThread
} = await import('./stores.svelte');
const { ApiError } = await import('./api');
const { openBackgroundTab, tabs } = await import('./tabs.svelte');

/** A promise the test decides the fate of, so state can be inspected mid-flight. */
function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

const offline = () => Promise.reject(new TypeError('Load failed'));

describe('saveNoteOrder', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vault.noteOrder = ['a.md', 'b.md', 'c.md'];
	});

	it('rolls back locally when the re-read fails as well', async () => {
		stub.saveNoteOrder.mockImplementation(offline);
		stub.noteOrder.mockImplementation(offline);

		const saving = saveNoteOrder(['c.md', 'a.md', 'b.md']);
		expect(vault.noteOrder).toEqual(['c.md', 'a.md', 'b.md']);
		await expect(saving).rejects.toThrow('Load failed');
		expect(vault.noteOrder).toEqual(['a.md', 'b.md', 'c.md']);
	});

	it('rolls back before the re-read answers, then takes what the server has', async () => {
		const reread = deferred<{ order: string[] }>();
		stub.saveNoteOrder.mockImplementation(offline);
		stub.noteOrder.mockReturnValue(reread.promise);

		await expect(saveNoteOrder(['c.md', 'a.md', 'b.md'])).rejects.toThrow();
		expect(vault.noteOrder).toEqual(['a.md', 'b.md', 'c.md']);
		expect(stub.noteOrder).toHaveBeenCalledTimes(1);

		reread.resolve({ order: ['b.md', 'a.md', 'c.md'] });
		await vi.waitFor(() => expect(vault.noteOrder).toEqual(['b.md', 'a.md', 'c.md']));
	});

	it('keeps the later save when the earlier reply arrives last', async () => {
		const first = deferred<{ order: string[] }>();
		const second = deferred<{ order: string[] }>();
		stub.saveNoteOrder.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

		const one = saveNoteOrder(['b.md', 'a.md', 'c.md']);
		const two = saveNoteOrder(['c.md', 'b.md', 'a.md']);
		second.resolve({ order: ['c.md', 'b.md', 'a.md'] });
		await two;
		first.resolve({ order: ['b.md', 'a.md', 'c.md'] });
		await one;
		expect(vault.noteOrder).toEqual(['c.md', 'b.md', 'a.md']);
	});

	it('does not roll back over a later save when an earlier one fails', async () => {
		const first = deferred<{ order: string[] }>();
		const second = deferred<{ order: string[] }>();
		stub.saveNoteOrder.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

		const one = saveNoteOrder(['b.md', 'a.md', 'c.md']);
		const two = saveNoteOrder(['c.md', 'b.md', 'a.md']);
		first.reject(new Error('conflict'));
		await expect(one).rejects.toThrow('conflict');
		expect(vault.noteOrder).toEqual(['c.md', 'b.md', 'a.md']);
		// The later save reconciles on its own; no re-read to race it.
		expect(stub.noteOrder).not.toHaveBeenCalled();

		second.resolve({ order: ['c.md', 'b.md', 'a.md'] });
		await two;
		expect(vault.noteOrder).toEqual(['c.md', 'b.md', 'a.md']);
	});

	it('ignores a re-read that lands after a save began', async () => {
		const reread = deferred<{ order: string[] }>();
		stub.noteOrder.mockReturnValue(reread.promise);
		stub.saveNoteOrder.mockReturnValue(new Promise(() => {}));

		const refreshing = refreshNoteOrder();
		void saveNoteOrder(['c.md', 'b.md', 'a.md']);
		reread.resolve({ order: ['a.md', 'b.md', 'c.md'] });
		await refreshing;
		expect(vault.noteOrder).toEqual(['c.md', 'b.md', 'a.md']);
	});
});

describe('saveFolderOrder', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vault.folderOrder = ['work', 'home'];
	});

	it('rolls back locally when the re-read fails as well', async () => {
		stub.saveFolderOrder.mockImplementation(offline);
		stub.folderOrder.mockImplementation(offline);

		await expect(saveFolderOrder(['home', 'work'])).rejects.toThrow('Load failed');
		expect(vault.folderOrder).toEqual(['work', 'home']);
	});

	it('keeps the later save when the earlier reply arrives last', async () => {
		const first = deferred<{ order: string[] }>();
		const second = deferred<{ order: string[] }>();
		stub.saveFolderOrder.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

		const one = saveFolderOrder(['home', 'work']);
		const two = saveFolderOrder(['work', 'home', 'archive']);
		second.resolve({ order: ['work', 'home', 'archive'] });
		await two;
		first.resolve({ order: ['home', 'work'] });
		await one;
		expect(vault.folderOrder).toEqual(['work', 'home', 'archive']);
	});
});

describe('refreshNotes', () => {
	const note = (path: string, modified: string) => ({
		path,
		name: path.replace(/\.md$/, ''),
		pinned: false,
		board: false,
		created: '2026-09-01T10:00:00Z',
		modified
	});

	beforeEach(() => {
		vi.clearAllMocks();
		vault.notes = [note('a.md', '2026-09-20T10:00:00Z')];
		vault.folders = ['work'];
	});

	// The live echo of every save used to hand the sidebar and the palette a
	// fresh array to re-derive from, even when nothing had changed.
	it('keeps the current lists when the server answers with the same data', async () => {
		const notes = vault.notes;
		const folders = vault.folders;
		stub.listNotes.mockResolvedValue([note('a.md', '2026-09-20T10:00:00Z')]);
		stub.listFolders.mockResolvedValue(['work']);

		await refreshNotes();
		expect(vault.notes).toBe(notes);
		expect(vault.folders).toBe(folders);
		expect(vault.loaded).toBe(true);
	});

	it('takes whichever list actually changed', async () => {
		const folders = vault.folders;
		stub.listNotes.mockResolvedValue([note('a.md', '2026-09-23T08:00:00Z')]);
		stub.listFolders.mockResolvedValue(['work']);

		await refreshNotes();
		expect(vault.notes[0].modified).toBe('2026-09-23T08:00:00Z');
		expect(vault.folders).toBe(folders);
	});
});

describe('afterPathsMoved and afterPathsRemoved', () => {
	const threadKey = (path: string) => `exscalia.omnia.noteThread:${path}`;
	const tabUrls = () => tabs.list.map((tab) => tab.url);

	beforeEach(() => {
		vi.clearAllMocks();
		storage.clear();
		recentNotes.paths = [];
	});

	it('carries the draft, conversation, tabs and recents to the new path', async () => {
		storage.set(threadKey('a/One.md'), 'thread-1');
		const chat = noteChatFor('a/One.md');
		rememberNote('a/One.md');
		rememberNote('b.md');
		openBackgroundTab('/note/a/One.md');

		await afterPathsMoved([['a/One.md', 'z/One.md']]);

		expect(drafts.moveDraft).toHaveBeenCalledWith('a/One.md', 'z/One.md');
		expect(storage.get(threadKey('z/One.md'))).toBe('thread-1');
		expect(storage.has(threadKey('a/One.md'))).toBe(false);
		expect(noteChatFor('z/One.md')).toBe(chat);
		expect(recentNotes.paths).toEqual(['b.md', 'z/One.md']);
		expect(JSON.parse(storage.get('exscalia.recentNotes')!)).toEqual(['b.md', 'z/One.md']);
		expect(tabUrls()).toContain('/note/z/One.md');
		expect(tabUrls()).not.toContain('/note/a/One.md');
	});

	it('forgets them for notes sent to the Trash', async () => {
		storage.set(threadKey('gone.md'), 'thread-2');
		rememberNote('gone.md');
		rememberNote('kept.md');
		openBackgroundTab('/note/gone.md');

		await afterPathsRemoved(['gone.md']);

		expect(drafts.removeAllDrafts).toHaveBeenCalledWith('gone.md');
		expect(storage.has(threadKey('gone.md'))).toBe(false);
		expect(recentNotes.paths).toEqual(['kept.md']);
		expect(tabUrls()).not.toContain('/note/gone.md');
	});

	it('has the conversation at the new path before the draft is moved', async () => {
		const chat = noteChatFor('early.md');
		let seen: unknown = null;
		drafts.moveDraft.mockImplementationOnce(async () => {
			seen = noteChatFor('later.md');
		});
		await afterPathsMoved([['early.md', 'later.md']]);
		expect(seen).toBe(chat);
	});

	it('leaves everything alone for a pair that did not move', async () => {
		storage.set(threadKey('same.md'), 'thread-3');
		await afterPathsMoved([['same.md', 'same.md']]);
		expect(drafts.moveDraft).not.toHaveBeenCalled();
		expect(storage.get(threadKey('same.md'))).toBe('thread-3');
	});
});

// The in-memory transcripts are capped at 20, oldest first. Each case starts
// by filling the cache with 20 fresh chats, so what is evicted is known.
describe('the note chat cache', () => {
	const fill = (prefix: string, count: number) => {
		for (let i = 0; i < count; i++) noteChatFor(`${prefix}-${i}.md`);
	};

	it('keeps one slot for a chat that moved, so the cap still reaches it', () => {
		fill('before-move', 20);
		const chat = noteChatFor('moving.md');
		moveNoteThread('moving.md', 'moved.md');

		fill('after-move', 19);
		expect(noteChatFor('moved.md')).toBe(chat);
		// The 21st chat pushes out the oldest, which is now the moved one.
		noteChatFor('after-move-19.md');
		expect(noteChatFor('moved.md')).not.toBe(chat);
	});

	it('does not evict a re-opened chat early after its note was forgotten', () => {
		fill('before-forget', 20);
		noteChatFor('again.md');
		forgetNoteThread('again.md');
		const chat = noteChatFor('again.md');

		// 20 chats in all: nothing may go yet, least of all the newest.
		fill('after-forget', 19);
		expect(noteChatFor('again.md')).toBe(chat);
	});
});

// The server binds a note to its conversation and carries it through moves;
// this browser's pointer is only a cache of that binding.
describe('note threads', () => {
	const threadKey = (path: string) => `exscalia.omnia.noteThread:${path}`;
	const thread = (id: string, text = 'hello') => ({
		id,
		title: 'Note',
		messages: [{ id: `${id}-m`, role: 'user', text, created_at: '', status: 'complete' }]
	});

	beforeEach(() => {
		vi.clearAllMocks();
		storage.clear();
		stub.getThread.mockImplementation(async (id: string) => thread(id));
	});

	it('finds a conversation the server carried through a move this browser missed', async () => {
		stub.noteThread.mockResolvedValue({ id: 't-moved' });

		await restoreNoteThread('missed/Moved.md');

		expect(stub.noteThread).toHaveBeenCalledWith('missed/Moved.md');
		expect(noteChatFor('missed/Moved.md').sessionId).toBe('t-moved');
		expect(storage.get(threadKey('missed/Moved.md'))).toBe('t-moved');
	});

	it('binds a thread only this browser remembered', async () => {
		storage.set(threadKey('legacy.md'), 't-legacy');
		stub.noteThread.mockResolvedValue({ id: null });
		stub.bindNoteThread.mockResolvedValue({ id: 't-legacy' });

		await restoreNoteThread('legacy.md');

		expect(stub.bindNoteThread).toHaveBeenCalledWith('legacy.md', 't-legacy');
		expect(noteChatFor('legacy.md').sessionId).toBe('t-legacy');
	});

	it('drops a pointer whose thread went with its note to another path', async () => {
		storage.set(threadKey('taken.md'), 't-elsewhere');
		stub.noteThread.mockResolvedValue({ id: null });
		stub.bindNoteThread.mockResolvedValue({ id: null });

		await restoreNoteThread('taken.md');

		expect(stub.getThread).not.toHaveBeenCalled();
		expect(storage.has(threadKey('taken.md'))).toBe(false);
	});

	it('falls back to the pointer while the server is unreachable', async () => {
		storage.set(threadKey('offline.md'), 't-cached');
		stub.noteThread.mockRejectedValue(new ApiError(0, 'Could not reach the server'));

		await restoreNoteThread('offline.md');

		expect(noteChatFor('offline.md').sessionId).toBe('t-cached');
		expect(storage.get(threadKey('offline.md'))).toBe('t-cached');
	});

	it.each([
		['a server restarting', new ApiError(503, 'Service Unavailable')],
		['an expired session', new ApiError(401, 'Unauthorized')]
	])('keeps the pointer through %s', async (_label, error) => {
		// A thread made before bindings: the pointer is its only record.
		const legacy = (step: string) => `legacy-${step}-${error.status}.md`;
		for (const step of ['lookup', 'bind', 'load']) {
			storage.set(threadKey(legacy(step)), `t-${step}`);
		}
		stub.noteThread.mockResolvedValue({ id: null });
		stub.bindNoteThread.mockImplementation(async (_path: string, id: string) => ({ id }));

		stub.noteThread.mockRejectedValueOnce(error);
		await restoreNoteThread(legacy('lookup'));
		stub.bindNoteThread.mockRejectedValueOnce(error);
		await restoreNoteThread(legacy('bind'));
		stub.getThread.mockRejectedValueOnce(error);
		await restoreNoteThread(legacy('load'));

		for (const step of ['lookup', 'bind', 'load']) {
			expect(storage.get(threadKey(legacy(step)))).toBe(`t-${step}`);
		}
	});

	it('drops the pointer when the server says the thread is gone', async () => {
		storage.set(threadKey('gone.md'), 't-gone');
		stub.noteThread.mockResolvedValue({ id: null });
		stub.bindNoteThread.mockRejectedValue(new ApiError(404, 'Thread not found'));

		await restoreNoteThread('gone.md');

		expect(stub.getThread).not.toHaveBeenCalled();
		expect(storage.has(threadKey('gone.md'))).toBe(false);
	});

	it('does not start a second thread when the first one fails to load', async () => {
		storage.set(threadKey('busy.md'), 't-busy');
		stub.noteThread.mockResolvedValue({ id: 't-busy' });
		stub.getThread.mockRejectedValue(new ApiError(502, 'Bad Gateway'));

		await expect(ensureNoteThread('busy.md')).rejects.toThrow('Bad Gateway');

		expect(stub.createThread).not.toHaveBeenCalled();
		expect(storage.get(threadKey('busy.md'))).toBe('t-busy');
	});

	it('creates the thread bound to the note, adopting one another device made', async () => {
		stub.noteThread.mockResolvedValue({ id: null });
		stub.createThread.mockResolvedValue(thread('t-other-device', 'from the phone'));

		const id = await ensureNoteThread('fresh/Plan.md');

		expect(stub.createThread).toHaveBeenCalledWith('Plan', 'fresh/Plan.md');
		expect(id).toBe('t-other-device');
		expect(noteChatFor('fresh/Plan.md').messages.map((m) => m.text)).toEqual(['from the phone']);
		expect(storage.get(threadKey('fresh/Plan.md'))).toBe('t-other-device');
	});

	it('keeps the media hub conversation local', async () => {
		stub.createThread.mockResolvedValue(thread('t-hub'));

		await ensureNoteThread('media:hub', 'Media');

		expect(stub.noteThread).not.toHaveBeenCalled();
		expect(stub.createThread).toHaveBeenCalledWith('Media', undefined);
	});
});
