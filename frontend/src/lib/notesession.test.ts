// The note page's save machine, scripted end to end (arch-maintainability#5).
// saveNote answers from a queue — saved, stale, conflict copy, a failed
// request, or a PUT held open while the test types — the editor is a string
// whose replaceDoc reports back through onEdit like the real one (holding it
// through an IME composition the same way), drafts live in a Map, and the
// clock is vitest's. Each case pins a regression the
// module's comments describe.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { encodePath, type Note } from './api';
import { CompositionQueue, settleHeld, type HeldReplacement } from './composequeue';
import type { LocalDraft } from './drafts';
import { createNoteSession, type NoteSession } from './notesession.svelte';

const PATH = 'trip.md';
const TAB = 'tab-a';
const BASE = ['# Trip', '', 'pack the tent', 'book the train', 'call the hut', ''].join('\n');

interface SaveCall {
	path: string;
	content: string;
	base: string;
	onConflict: 'copy' | 'reject';
	keepalive?: boolean;
}

type Reply = (call: SaveCall) => Promise<Note>;

const note = (content: string, revision: string, extra: Partial<Note> = {}): Note => ({
	path: PATH,
	content,
	meta: {},
	revision,
	...extra
});
const savedAs = (content: string, revision: string) => note(content, revision, { status: 'saved' });
const stale = (content: string, revision: string) => note(content, revision, { status: 'stale' });

function held() {
	let resolve!: (value: Note) => void;
	let reject!: (cause: unknown) => void;
	const promise = new Promise<Note>((ok, fail) => {
		resolve = ok;
		reject = fail;
	});
	return { promise, resolve, reject };
}

/** Let every promise chain the last step started run to its end. */
async function settle() {
	for (let round = 0; round < 50; round += 1) await Promise.resolve();
}

function harness() {
	const saves: SaveCall[] = [];
	const replies: Reply[] = [];
	const server = { content: BASE, revision: 'r1' };
	let revisions = 1;
	const drafts = new Map<string, LocalDraft>();
	const key = (path: string, owner: string) => `${path}\u0000${owner}`;
	let text = '';
	let session!: NoteSession;

	// Editor.svelte's IME glue: a replacement requested mid-composition waits,
	// and meanwhile onChange and getDoc report the text as it will be.
	let composing = false;
	const conflict = () => session.replacementConflict();
	const settled = () => settleHeld(ime.pendingValue, text, conflict);
	const write = (next: string, programmatic: boolean) => {
		if (next === text) return;
		text = next;
		session.onEdit(next, programmatic);
	};
	const ime = new CompositionQueue<HeldReplacement>({
		composing: () => composing,
		apply: (held) => write(settleHeld(held, text, conflict), held.programmatic),
		setTimer: (fn, ms) => setTimeout(fn, ms),
		clearTimer: (id) => clearTimeout(id)
	});

	const editor = {
		getDoc: settled,
		// Like Editor.replaceDoc: identical text dispatches nothing, anything
		// else comes back through onChange, programmatic unless told otherwise.
		replaceDoc: vi.fn(
			(next: string, options: { programmatic?: boolean; remote?: boolean } = {}) => {
				ime.request({
					base: text,
					doc: next,
					programmatic: options.programmatic ?? true,
					remote: options.remote ?? false
				});
			}
		),
		hasFocus: () => true,
		restoreFocusIfLost: vi.fn(),
		adoptPath: vi.fn()
	};
	const notify = vi.fn();
	const goto = vi.fn(async (_url: string) => {});
	const refreshNotes = vi.fn(async () => {});
	const refreshRelated = vi.fn();
	const announceSaved = vi.fn();
	const readNote = vi.fn(async (path: string) => ({
		...note(server.content, server.revision),
		path
	}));

	// The default answer: the server takes the body as sent.
	const accept: Reply = async (call) => {
		server.content = call.content;
		server.revision = `r${++revisions}`;
		return { ...savedAs(call.content, server.revision), path: call.path };
	};

	session = createNoteSession({
		path: () => PATH,
		api: {
			saveNote: (path, content, base, onConflict, options) => {
				const call = { path, content, base, onConflict, keepalive: options.keepalive };
				saves.push(call);
				return (replies.shift() ?? accept)(call);
			},
			readNote
		},
		drafts: {
			writeDraft: async (draft) => {
				drafts.set(key(draft.path, draft.owner), { ...draft });
			},
			removeDraft: async (path, owner) => {
				drafts.delete(key(path, owner));
			},
			moveDraft: async (oldPath, newPath, options = {}) => {
				for (const [id, row] of [...drafts]) {
					if (row.path !== oldPath) continue;
					if (options.owner !== undefined && row.owner !== options.owner) continue;
					drafts.delete(id);
					const moved: LocalDraft = { ...row, path: newPath };
					if (options.dropBase) {
						moved.baseRevision = '';
						delete moved.baseContent;
					}
					drafts.set(key(newPath, row.owner), moved);
				}
			}
		},
		editor: () => editor,
		notify,
		goto,
		refreshNotes,
		refreshRelated,
		announceSaved,
		clientId: TAB,
		now: () => Date.now(),
		setTimeout: (callback, ms) => setTimeout(callback, ms),
		clearTimeout: (timer) => clearTimeout(timer)
	});

	return {
		session,
		saves,
		server,
		drafts,
		editor,
		notify,
		goto,
		refreshNotes,
		refreshRelated,
		announceSaved,
		readNote,
		get text() {
			return text;
		},
		draft: (path: string, owner = TAB) => drafts.get(key(path, owner)),
		/** Open the note the way the page's load() does. */
		async open(content = BASE, draft: LocalDraft | null = null, revision = 'r1') {
			server.content = content;
			server.revision = revision;
			if (draft) drafts.set(key(draft.path, draft.owner), draft);
			await session.adoptLoaded(note(content, revision), draft);
			text = session.doc;
		},
		/** A keystroke's worth of user edit. */
		type(next: string) {
			text = next;
			session.onEdit(settled(), false);
		},
		/** Start or end an IME composition; ending it releases a held value. */
		compose(on: boolean) {
			composing = on;
			if (!on) ime.release();
		},
		reply(...next: Reply[]) {
			replies.push(...next);
		},
		/** The next PUT stays open until the test answers it. */
		hold() {
			const pending = held();
			replies.push(() => pending.promise);
			return pending;
		}
	};
}

const mine = BASE.replace('pack the tent', 'pack the tent and stove');

beforeEach(() => {
	vi.useFakeTimers();
	vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
	vi.useRealTimers();
	vi.restoreAllMocks();
});

describe('autosave', () => {
	it('sends one PUT 800 ms after typing, then drops the draft', async () => {
		const h = harness();
		await h.open();
		h.type(mine);
		expect(h.session.saveState).toBe('dirty');

		await vi.advanceTimersByTimeAsync(799);
		expect(h.saves).toHaveLength(0);
		// The crash draft lands first, batched at 300 ms.
		expect(h.draft(PATH)).toMatchObject({ content: mine, baseRevision: 'r1', baseContent: BASE });

		await vi.advanceTimersByTimeAsync(1);
		expect(h.saves).toEqual([
			{ path: PATH, content: mine, base: 'r1', onConflict: 'reject', keepalive: true }
		]);
		await settle();
		expect(h.session.saveState).toBe('saved');
		expect(h.session.note?.content).toBe(mine);
		expect(h.session.serverRevision).toBe('r2');
		expect(h.drafts.size).toBe(0);
		expect(h.announceSaved).toHaveBeenCalledOnce();
		expect(h.refreshRelated).toHaveBeenCalledOnce();

		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(1);
	});

	it('leaves keepalive off for a body browsers would refuse', async () => {
		const h = harness();
		await h.open();
		h.type(BASE + 'x'.repeat(70_000));
		await vi.advanceTimersByTimeAsync(800);
		expect(h.saves[0].keepalive).toBe(false);
	});

	it('never counts a programmatic change as an edit', async () => {
		const h = harness();
		await h.open();
		const version = h.session.editVersion;
		h.session.onEdit(mine, true);
		expect(h.session.doc).toBe(mine);
		expect(h.session.editVersion).toBe(version);
		expect(h.session.saveState).toBe('saved');
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(0);
		expect(h.drafts.size).toBe(0);
	});

	it('hands typing that overlapped a PUT to the next 800 ms debounce', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		const more = mine + 'buy stamps\n';
		h.type(more);
		await vi.advanceTimersByTimeAsync(100);
		first.resolve(savedAs(mine, 'r2'));
		await settle();

		// No back-to-back PUT for the overlap (fe-editor#10).
		expect(h.saves).toHaveLength(1);
		expect(h.session.saveState).toBe('dirty');
		await vi.advanceTimersByTimeAsync(799);
		expect(h.saves).toHaveLength(1);
		await vi.advanceTimersByTimeAsync(1);
		expect(h.saves[1]).toMatchObject({ content: more, base: 'r2' });
		await settle();
		expect(h.session.saveState).toBe('saved');
	});

	it('advances the merge base on a save typing overlapped, so the next pull merges', async () => {
		const h = harness();
		await h.open('line one\n');
		const first = h.hold();
		h.type('line one A\n');
		await vi.advanceTimersByTimeAsync(800);
		h.type('line one AB\n');
		first.resolve(savedAs('line one A\n', 'r2'));
		await settle();
		// The base is the server's body even though the editor is ahead of it.
		expect(h.session.note?.content).toBe('line one A\n');
		expect(h.session.serverRevision).toBe('r2');

		// Another device edits the same line before our next save. Against the
		// stale base ('line one\n') both sides inserted at the same point: a
		// false 'pick one' conflict.
		h.server.content = 'X line one A\n';
		h.server.revision = 'r3';
		await h.session.pull();
		expect(h.session.remoteConflict).toBeNull();
		expect(h.text).toBe('X line one AB\n');
		expect(h.notify).toHaveBeenCalledWith('Merged an outside edit into your draft.');

		await vi.advanceTimersByTimeAsync(300);
		expect(h.saves[1]).toMatchObject({ content: 'X line one AB\n', base: 'r3' });
	});

	it('drops the armed draft when a save acknowledges the latest version', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		const flushing = h.session.flush();
		expect(h.saves).toHaveLength(1);
		const more = mine + 'buy stamps\n';
		h.type(more);
		// Another device saved meanwhile, so the merged retry goes at once,
		// while the draft timer the typing armed is still pending.
		const theirs = BASE.replace('# Trip', '# Trip north');
		first.resolve(stale(theirs, 'r2'));
		await flushing;

		// The retry sent the rest and it was acknowledged.
		expect(h.saves[1]).toMatchObject({
			content: more.replace('# Trip', '# Trip north'),
			base: 'r2'
		});
		expect(h.session.saveState).toBe('saved');
		expect(h.drafts.size).toBe(0);
		// Left armed, the 300 ms timer wrote the saved text back as a draft
		// and the next open offered it for recovery (fe-editor#7).
		await vi.advanceTimersByTimeAsync(1000);
		expect(h.drafts.size).toBe(0);
	});
});

describe('Mod-S', () => {
	it('saves what was typed before it and hands later typing to the debounce', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		const flushing = h.session.flush();
		const more = mine + 'buy stamps\n';
		h.type(more);
		first.resolve(savedAs(mine, 'r2'));
		await flushing;

		// Draining to "nothing left" sent one PUT per round trip for as long
		// as typing continued after the shortcut.
		expect(h.saves).toHaveLength(1);
		expect(h.session.saveState).toBe('dirty');
		await vi.advanceTimersByTimeAsync(799);
		expect(h.saves).toHaveLength(1);
		await vi.advanceTimersByTimeAsync(1);
		expect(h.saves[1]).toMatchObject({ content: more, base: 'r2' });
		await settle();
		expect(h.session.saveState).toBe('saved');
	});

	it('pressed during an autosave PUT, still saves what was typed before it', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		const second = h.hold();
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		expect(h.saves).toHaveLength(1);
		const more = mine + 'buy stamps\n';
		h.type(more);
		const flushing = h.session.flush();
		first.resolve(savedAs(mine, 'r2'));
		await settle();
		// Typed before the shortcut, so it goes at once.
		expect(h.saves[1]).toMatchObject({ content: more, base: 'r2' });

		const after = more + 'post it\n';
		h.type(after);
		second.resolve(savedAs(more, 'r3'));
		await flushing;
		expect(h.saves).toHaveLength(2);
		await vi.advanceTimersByTimeAsync(800);
		expect(h.saves[2]).toMatchObject({ content: after, base: 'r3' });
	});
});

// fe-editor#6: a replacement the editor holds for an IME composition. The
// session moves its base to the replacement's revision at once, so what it
// saves meanwhile has to carry the replacement, and an overlap has to ask.
describe('a replacement held for an IME composition', () => {
	it('saves the composed text with the outside edit it is holding', async () => {
		const h = harness();
		await h.open();
		h.compose(true);
		const theirs = BASE.replace('call the hut', 'call the hut on Friday');
		h.server.content = theirs;
		h.server.revision = 'r2';
		await h.session.pull();
		expect(h.text).toBe(BASE);
		expect(h.session.serverRevision).toBe('r2');

		h.type(BASE.replace('pack the tent', 'pack the tent かな'));
		const both = theirs.replace('pack the tent', 'pack the tent かな');
		expect(h.session.doc).toBe(both);
		await vi.advanceTimersByTimeAsync(800);
		// The composed text alone, on base r2, reverted their edit.
		expect(h.saves).toEqual([
			expect.objectContaining({ content: both, base: 'r2' })
		]);
		await settle();
		expect(h.session.saveState).toBe('saved');

		h.compose(false);
		expect(h.text).toBe(both);
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(1);
		expect(h.session.remoteConflict).toBeNull();
	});

	it('drafts the text with the held edit in it', async () => {
		const h = harness();
		await h.open();
		h.compose(true);
		const theirs = BASE.replace('call the hut', 'call the hut on Friday');
		h.server.content = theirs;
		h.server.revision = 'r2';
		await h.session.pull();
		h.type(BASE.replace('pack the tent', 'pack the tent かな'));
		await vi.advanceTimersByTimeAsync(300);
		expect(h.draft(PATH)).toMatchObject({
			content: theirs.replace('pack the tent', 'pack the tent かな'),
			baseRevision: 'r2',
			baseContent: theirs
		});
	});

	it('asks instead of saving over an outside edit the composition overlaps', async () => {
		const h = harness();
		await h.open();
		h.compose(true);
		const theirs = BASE.replace('book the train', 'book the bus');
		h.server.content = theirs;
		h.server.revision = 'r2';
		await h.session.pull();

		const composed = BASE.replace('book the train', 'book the 電車');
		h.type(composed);
		expect(h.session.remoteConflict).toMatchObject({ content: theirs, revision: 'r2' });
		expect(h.session.saveState).toBe('dirty');
		// The hold cap lets go of the replacement; nothing is saved over theirs.
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.text).toBe(composed);
		expect(h.saves).toHaveLength(0);

		h.session.keepMine();
		await vi.advanceTimersByTimeAsync(120);
		await settle();
		expect(h.saves).toEqual([expect.objectContaining({ content: composed, base: 'r2' })]);
		expect(h.session.remoteConflict).toBeNull();
	});

	it('does not ask over the server normalizing our own save', async () => {
		const h = harness();
		await h.open();
		const typed = BASE.replace('call the hut', 'call the hut x');
		const normalized = BASE.replace('call the hut', 'call the hut x [1]');
		h.reply(async (call) => ({ ...savedAs(normalized, 'r2'), path: call.path }));
		h.compose(true);
		h.type(typed);
		await vi.advanceTimersByTimeAsync(800);
		await settle();
		expect(h.text).toBe(typed);
		expect(h.session.doc).toBe(normalized);

		// Composed right where the server wrote: the next save is normalized
		// again, so nothing is lost by keeping the composition.
		const more = BASE.replace('call the hut', 'call the hut xy');
		h.type(more);
		expect(h.session.remoteConflict).toBeNull();
		await vi.advanceTimersByTimeAsync(800);
		expect(h.saves[1]).toMatchObject({ content: more, base: 'r2' });
	});

	it('Take theirs puts the outside edit back once the composition ends', async () => {
		const h = harness();
		await h.open();
		h.compose(true);
		const theirs = BASE.replace('book the train', 'book the bus');
		h.server.content = theirs;
		h.server.revision = 'r2';
		await h.session.pull();
		h.type(BASE.replace('book the train', 'book the 電車'));
		expect(h.session.remoteConflict).not.toBeNull();

		// Tapping the banner ends the composition.
		h.compose(false);
		h.session.takeTheirs();
		expect(h.text).toBe(theirs);
		expect(h.session.saveState).toBe('saved');
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(0);
	});
});

describe('stale saves', () => {
	it('merges the live document, including typing during the PUT, and retries', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		const typedDuring = mine.replace('call the hut', 'call the hut twice');
		h.type(typedDuring);
		const theirs = BASE.replace('book the train', 'book the night train');
		first.resolve(stale(theirs, 'r2'));
		await settle();

		const merged = typedDuring.replace('book the train', 'book the night train');
		// fe-editor#1: the retry carries their edit AND what was typed mid-PUT.
		expect(h.saves).toHaveLength(2);
		expect(h.saves[1]).toMatchObject({ content: merged, base: 'r2', onConflict: 'reject' });
		expect(h.text).toBe(merged);
		expect(h.session.saveState).toBe('saved');

		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(2);
	});

	it('freezes saving on overlapping edits until Keep mine', async () => {
		const h = harness();
		await h.open();
		const theirs = BASE.replace('book the train', 'book the bus');
		h.reply(async () => stale(theirs, 'r2'));
		const ours = BASE.replace('book the train', 'book the ferry');
		h.type(ours);
		await vi.advanceTimersByTimeAsync(800);
		await settle();
		expect(h.session.remoteConflict?.content).toBe(theirs);
		expect(h.session.saveState).toBe('dirty');

		// Typing under the banner, the debounce, a retry and Mod+S all hold off:
		// the loop used to decide the conflict for the user.
		h.type(ours + 'more\n');
		await vi.advanceTimersByTimeAsync(60_000);
		await h.session.flush();
		expect(h.saves).toHaveLength(1);
		expect(await h.session.settledForMutation()).toBe(false);

		h.session.keepMine();
		expect(h.session.remoteConflict).toBeNull();
		await vi.advanceTimersByTimeAsync(120);
		await settle();
		// Re-based onto theirs, so the server accepts it.
		expect(h.saves[1]).toMatchObject({ content: ours + 'more\n', base: 'r2' });
		expect(h.session.saveState).toBe('saved');
	});

	it('Take theirs adopts their text and forgets the draft', async () => {
		const h = harness();
		await h.open();
		const theirs = BASE.replace('book the train', 'book the bus');
		h.reply(async () => stale(theirs, 'r2'));
		h.type(BASE.replace('book the train', 'book the ferry'));
		await vi.advanceTimersByTimeAsync(800);
		await settle();
		expect(h.draft(PATH)).toBeDefined();

		h.session.takeTheirs();
		await settle();
		expect(h.text).toBe(theirs);
		expect(h.session.doc).toBe(theirs);
		expect(h.session.serverRevision).toBe('r2');
		expect(h.session.saveState).toBe('saved');
		expect(h.drafts.size).toBe(0);
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(1);
	});
});

describe('conflict copies and failures', () => {
	it('moves the draft to the conflict copy and opens it once', async () => {
		const h = harness();
		await h.open();
		const copy = 'trip (conflict 2026-09-23).md';
		h.reply(async () => note(mine, 'c1', { path: copy, status: 'conflict_copy', conflict_path: copy }));
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		await settle();

		expect(h.draft(PATH)).toBeUndefined();
		const moved = h.draft(copy);
		expect(moved).toMatchObject({ path: copy, content: mine, baseRevision: '' });
		expect(moved?.baseContent).toBeUndefined();
		expect(h.refreshNotes).toHaveBeenCalledOnce();
		expect(h.goto).toHaveBeenCalledOnce();
		expect(h.goto).toHaveBeenCalledWith(`/note/${encodePath(copy)}`);
		expect(h.notify).toHaveBeenCalledWith(
			'Another device changed this note. Your version was saved as a conflict copy.',
			expect.objectContaining({ tone: 'error', actionLabel: 'Open copy' })
		);
	});

	it('backs off 2.2, 4.4, 8.8, 17.6, 30, 30 s on network errors, then stops and says so once', async () => {
		const h = harness();
		await h.open();
		const offline: Reply = async () => {
			throw new TypeError('Failed to fetch');
		};
		h.reply(...Array(20).fill(offline));
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		await settle();
		expect(h.saves).toHaveLength(1);
		expect(h.session.saveState).toBe('error');

		const gaps = [2200, 4400, 8800, 17_600, 30_000, 30_000];
		for (const [index, gap] of gaps.entries()) {
			await vi.advanceTimersByTimeAsync(gap - 1);
			expect(h.saves).toHaveLength(index + 1);
			await vi.advanceTimersByTimeAsync(1);
			await settle();
			expect(h.saves).toHaveLength(index + 2);
		}
		expect(h.notify).toHaveBeenCalledOnce();
		expect(h.notify).toHaveBeenCalledWith(
			'Could not save — check your connection, then edit to retry.',
			{ tone: 'error' }
		);

		await vi.advanceTimersByTimeAsync(60 * 60_000);
		expect(h.saves).toHaveLength(7);
		expect(h.notify).toHaveBeenCalledOnce();
		// The draft is what survives.
		expect(h.draft(PATH)?.content).toBe(mine);
	});
});

describe('a page torn down mid-save', () => {
	it('schedules no retry and tells no one when the PUT fails', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		h.session.dispose();
		first.reject(new TypeError('Failed to fetch'));
		await settle();

		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(60 * 60_000);
		expect(h.saves).toHaveLength(1);
		expect(h.notify).not.toHaveBeenCalled();
		expect(h.goto).not.toHaveBeenCalled();
		expect(h.draft(PATH)?.content).toBe(mine);
	});

	it('does not merge a stale result nobody will see', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		h.session.dispose();
		first.resolve(stale(BASE.replace('book the train', 'book the bus'), 'r2'));
		await settle();
		expect(h.saves).toHaveLength(1);
		expect(h.text).toBe(mine);
		expect(vi.getTimerCount()).toBe(0);
	});

	it('does not navigate to a conflict copy', async () => {
		const h = harness();
		await h.open();
		const copy = 'trip (conflict).md';
		const first = h.hold();
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		h.session.dispose();
		first.resolve(note(mine, 'c1', { path: copy, status: 'conflict_copy', conflict_path: copy }));
		await settle();
		expect(h.goto).not.toHaveBeenCalled();
		expect(h.draft(copy)?.content).toBe(mine);
	});

	it('drains what was typed and removes the draft once it is saved', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		await vi.advanceTimersByTimeAsync(800);
		const more = mine + 'buy stamps\n';
		h.type(more);
		h.session.dispose();
		// dispose() lands the pending draft.
		expect(h.draft(PATH)?.content).toBe(more);
		first.resolve(savedAs(mine, 'r2'));
		await settle();

		// No debounce will come for a dead page, so the loop goes again at once.
		expect(h.saves[1]).toMatchObject({ content: more, base: 'r2' });
		expect(h.drafts.size).toBe(0);
		expect(h.refreshRelated).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);
	});
});

describe('settledForMutation', () => {
	it('is true once a flush lands everything', async () => {
		const h = harness();
		await h.open();
		expect(await h.session.settledForMutation()).toBe(true);
		h.type(mine);
		expect(await h.session.settledForMutation()).toBe(true);
		expect(h.saves).toHaveLength(1);
		expect(h.notify).not.toHaveBeenCalled();
	});

	it('is true when typing carries on during its save', async () => {
		// /task clears its line and settles; a fast typist is already on the
		// next word. Checking the live version failed the command and lost it.
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		const settling = h.session.settledForMutation();
		h.type(mine + 'buy stamps\n');
		first.resolve(savedAs(mine, 'r2'));
		expect(await settling).toBe(true);
		expect(h.saves).toHaveLength(1);
		expect(h.notify).not.toHaveBeenCalled();
		// The later typing still goes with the next debounce.
		await vi.advanceTimersByTimeAsync(800);
		expect(h.saves[1]).toMatchObject({ content: mine + 'buy stamps\n', base: 'r2' });
	});

	it('is false while a recovered draft waits for a decision', async () => {
		const h = harness();
		const draft: LocalDraft = {
			path: PATH,
			owner: 'tab-b',
			content: mine,
			baseRevision: 'r1',
			baseContent: BASE,
			updatedAt: 1
		};
		await h.open(BASE, draft);
		expect(h.session.recoveryPending).toBe(true);
		expect(h.session.saveState).toBe('dirty');
		expect(await h.session.settledForMutation()).toBe(false);
		expect(h.saves).toHaveLength(0);
		expect(h.notify).toHaveBeenCalledWith('Save this note before changing it from here.', {
			tone: 'error'
		});
	});

	it('is false after a failed save', async () => {
		const h = harness();
		await h.open();
		h.reply(async () => {
			throw new TypeError('Failed to fetch');
		});
		h.type(mine);
		expect(await h.session.settledForMutation()).toBe(false);
		expect(h.session.saveState).toBe('error');
	});
});

describe('flushBeforeOmnia', () => {
	it('resolves when typing carries on during its save', async () => {
		const h = harness();
		await h.open();
		const first = h.hold();
		h.type(mine);
		const flushing = h.session.flushBeforeOmnia();
		h.type(mine + 'buy stamps\n');
		first.resolve(savedAs(mine, 'r2'));
		await expect(flushing).resolves.toBeUndefined();
		expect(h.saves).toHaveLength(1);
	});

	it('throws when the save fails', async () => {
		const h = harness();
		await h.open();
		h.reply(async () => {
			throw new TypeError('Failed to fetch');
		});
		h.type(mine);
		await expect(h.session.flushBeforeOmnia()).rejects.toThrow(/before sending it to OMNIA/);
	});
});

describe('recovered drafts', () => {
	const draftFrom = (content: string, extra: Partial<LocalDraft> = {}): LocalDraft => ({
		path: PATH,
		owner: 'tab-b',
		content,
		baseRevision: 'r1',
		baseContent: BASE,
		updatedAt: 1,
		...extra
	});

	it('merges a draft onto a note that moved on, and saves it on Keep', async () => {
		const h = harness();
		const moved = BASE.replace('call the hut', 'call the hut at noon');
		await h.open(moved, draftFrom(mine), 'r2');
		const merged = mine.replace('call the hut', 'call the hut at noon');
		expect(h.session.recoveryPending).toBe(true);
		expect(h.session.doc).toBe(merged);
		expect(h.session.note?.content).toBe(moved);

		// Nothing is saved until the banner is answered.
		h.type(merged + 'more\n');
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(0);

		h.session.keepDraft();
		await vi.advanceTimersByTimeAsync(120);
		await settle();
		expect(h.saves).toEqual([
			expect.objectContaining({ content: merged + 'more\n', base: 'r2' })
		]);
		// The other tab's draft went with the save that carried it.
		expect(h.draft(PATH, 'tab-b')).toBeUndefined();
		expect(h.drafts.size).toBe(0);
	});

	it('Discard puts the loaded text back and forgets both drafts', async () => {
		const h = harness();
		await h.open(BASE, draftFrom(mine));
		h.type(mine + 'more\n');
		await h.session.discardDraft();
		expect(h.text).toBe(BASE);
		expect(h.session.saveState).toBe('saved');
		expect(h.session.recoveryPending).toBe(false);
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.drafts.size).toBe(0);
		expect(h.saves).toHaveLength(0);
	});

	it('asks when the draft and the server rewrote the same lines', async () => {
		const h = harness();
		const theirs = BASE.replace('pack the tent', 'pack the tent and bivvy');
		await h.open(theirs, draftFrom(mine), 'r2');
		expect(h.session.recoveryPending).toBe(false);
		expect(h.session.remoteConflict?.content).toBe(theirs);
		// The base stays the draft's own until someone picks a side.
		expect(h.session.note?.content).toBe(BASE);
		expect(h.session.serverRevision).toBe('r1');
		expect(h.session.doc).toBe(mine);

		h.session.keepMine();
		await vi.advanceTimersByTimeAsync(120);
		await settle();
		expect(h.saves[0]).toMatchObject({ content: mine, base: 'r2' });
		expect(h.drafts.size).toBe(0);
	});

	it('opens clean and drops a draft the server already holds', async () => {
		const h = harness();
		await h.open(mine, draftFrom(mine), 'r2');
		expect(h.session.recoveryPending).toBe(false);
		expect(h.session.remoteConflict).toBeNull();
		expect(h.session.saveState).toBe('saved');
		expect(h.drafts.size).toBe(0);
	});
});

describe('pull', () => {
	it('adopts an outside edit wholesale when nothing local is pending', async () => {
		const h = harness();
		await h.open();
		const theirs = BASE.replace('book the train', 'book the bus');
		h.server.content = theirs;
		h.server.revision = 'r2';
		await h.session.pull();
		expect(h.text).toBe(theirs);
		expect(h.session.note?.content).toBe(theirs);
		expect(h.session.serverRevision).toBe('r2');
		expect(h.session.saveState).toBe('saved');
		await vi.advanceTimersByTimeAsync(10_000);
		expect(h.saves).toHaveLength(0);
	});

	it('does not read while a conflict is waiting', async () => {
		const h = harness();
		await h.open();
		h.reply(async () => stale(BASE.replace('book the train', 'book the bus'), 'r2'));
		h.type(BASE.replace('book the train', 'book the ferry'));
		await vi.advanceTimersByTimeAsync(800);
		await settle();
		await h.session.pull();
		expect(h.readNote).not.toHaveBeenCalled();
	});
});

describe('rebind after a rename', () => {
	const target = 'trips/trip.md';

	it('swaps path and revision in place and takes the realigned text', async () => {
		const h = harness();
		await h.open();
		const realigned = BASE.replace('# Trip', '# Trips');
		h.session.rebind(PATH, target, { path: target, revision: 'r5', content: realigned });
		expect(h.session.note?.path).toBe(target);
		expect(h.session.serverRevision).toBe('r5');
		expect(h.editor.adoptPath).toHaveBeenCalledWith(target);
		expect(h.text).toBe(realigned);
		expect(h.session.note?.content).toBe(realigned);
		expect(h.session.saveState).toBe('saved');

		h.type(realigned + 'more\n');
		await vi.advanceTimersByTimeAsync(800);
		expect(h.saves[0]).toMatchObject({ path: target, base: 'r5' });
	});

	it('leaves unsaved text alone', async () => {
		const h = harness();
		await h.open();
		h.type(mine);
		h.session.rebind(PATH, target, { path: target, revision: 'r5', content: BASE });
		expect(h.session.note?.path).toBe(target);
		expect(h.session.serverRevision).toBe('r1');
		expect(h.editor.replaceDoc).not.toHaveBeenCalled();
		expect(h.text).toBe(mine);
	});

	it('keeps the old base under unsaved text, so its save merges the move', async () => {
		const h = harness();
		await h.open();
		h.type(mine);
		const realigned = BASE.replace('# Trip', '# Trips');
		h.session.rebind(PATH, target, { path: target, revision: 'r5', content: realigned });
		expect(h.session.serverRevision).toBe('r1');

		// The server holds the realigned copy, so the old base is stale there.
		h.reply(async (call) => ({ ...stale(realigned, 'r5'), path: call.path }));
		await vi.advanceTimersByTimeAsync(800);
		await settle();

		const both = mine.replace('# Trip', '# Trips');
		expect(h.saves[0]).toMatchObject({ path: target, base: 'r1', onConflict: 'reject' });
		expect(h.saves[1]).toMatchObject({ path: target, base: 'r5', content: both });
		expect(h.text).toBe(both);
		expect(h.session.saveState).toBe('saved');
	});

	it('does not replace identical text', async () => {
		const h = harness();
		await h.open();
		h.session.rebind(PATH, target, { path: target, revision: 'r5', content: BASE });
		expect(h.editor.replaceDoc).not.toHaveBeenCalled();
		expect(h.editor.adoptPath).toHaveBeenCalledWith(target);
	});

	it('ignores a rename of some other note', async () => {
		const h = harness();
		await h.open();
		h.session.rebind('other.md', target, { path: target, revision: 'r5', content: 'x' });
		expect(h.session.note?.path).toBe(PATH);
		expect(h.session.serverRevision).toBe('r1');
		expect(h.editor.adoptPath).not.toHaveBeenCalled();
	});
});
