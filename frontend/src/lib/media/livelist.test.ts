// The live-list primitive the media and Paper pages share: which load may
// land, what a failure leaves on screen, and how a burst of live events
// becomes one fetch.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LiveNoteEvent } from '$lib/live';

const { createLiveList, isMediaNoteEvent: isMedia } = await import('./livelist.svelte');

/** A promise the test decides the fate of, so loads can settle out of order. */
function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

function fire(target: EventTarget, detail: Partial<LiveNoteEvent> | null) {
	target.dispatchEvent(new CustomEvent('exscalia:live', { detail }));
}

describe('load', () => {
	it('lets only the newest load land', async () => {
		const older = deferred<string[]>();
		const newer = deferred<string[]>();
		const fetcher = vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
		const list = createLiveList<string[]>(fetcher, { match: isMedia, initial: [] });

		const first = list.load();
		const second = list.load();
		newer.resolve(['new']);
		await second;
		older.resolve(['old']);
		await first;

		expect(list.data).toEqual(['new']);
		expect(list.failed).toBe(false);
	});

	it('ignores a superseded failure', async () => {
		const older = deferred<string[]>();
		const fetcher = vi
			.fn()
			.mockReturnValueOnce(older.promise)
			.mockResolvedValueOnce(['new']);
		const list = createLiveList<string[]>(fetcher, { match: isMedia, initial: [] });

		const first = list.load();
		await list.load();
		older.reject(new Error('offline'));
		await first;

		expect(list.data).toEqual(['new']);
		expect(list.failed).toBe(false);
	});

	it('keeps the last good data on failure and clears failed on the next success', async () => {
		const fetcher = vi
			.fn()
			.mockRejectedValueOnce(new Error('offline'))
			.mockRejectedValueOnce(new Error('offline'))
			.mockResolvedValueOnce(['b']);
		const list = createLiveList<string[]>(fetcher, { match: isMedia, initial: ['a'] });

		await list.load();
		expect(list.data).toEqual(['a']);
		expect(list.failed).toBe(true);

		// A second failure is still a failure, not a blank page.
		await list.load();
		expect(list.data).toEqual(['a']);
		expect(list.failed).toBe(true);

		await list.load();
		expect(list.data).toEqual(['b']);
		expect(list.failed).toBe(false);
	});

	it('treats a null from the loader as a load that already failed', () => {
		const fetcher = vi.fn();
		expect(createLiveList(fetcher, { match: isMedia, initial: null }).failed).toBe(true);
		expect(createLiveList(fetcher, { match: isMedia }).failed).toBe(false);
		const loaded = createLiveList(fetcher, { match: isMedia, initial: ['a'] });
		expect(loaded.failed).toBe(false);
		expect(loaded.data).toEqual(['a']);
		expect(fetcher).not.toHaveBeenCalled();
	});

	it('reports a load on the wire and lets set() paint over a failure', async () => {
		const gate = deferred<string[]>();
		const list = createLiveList<string[]>(() => gate.promise, { match: isMedia, initial: null });

		expect(list.pending).toBe(false);
		const done = list.load();
		expect(list.pending).toBe(true);
		list.set(['written']);
		expect(list.data).toEqual(['written']);
		expect(list.failed).toBe(false);
		gate.resolve(['fetched']);
		await done;
		expect(list.pending).toBe(false);
		expect(list.data).toEqual(['fetched']);
	});
});

describe('isMediaNoteEvent', () => {
	it('takes note writes and removals under media/ only', () => {
		expect(isMedia({ type: 'note', path: 'media/a.md' })).toBe(true);
		expect(isMedia({ type: 'note_removed', path: 'media/a.md' })).toBe(true);
		expect(isMedia({ type: 'note', path: 'notes/media/a.md' })).toBe(false);
		expect(isMedia({ type: 'paper', path: 'media/a.md' })).toBe(false);
	});
});

describe('listen', () => {
	beforeEach(() => {
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it('coalesces a burst inside the delay into one fetch', async () => {
		const target = new EventTarget();
		const fetcher = vi.fn().mockResolvedValue(['x']);
		const list = createLiveList<string[]>(fetcher, { match: isMedia, initial: [], target });
		const stop = list.listen();

		fire(target, { type: 'note', path: 'media/a.md' });
		await vi.advanceTimersByTimeAsync(400);
		fire(target, { type: 'note_removed', path: 'media/b.md' });
		await vi.advanceTimersByTimeAsync(599);
		expect(fetcher).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(1);
		expect(fetcher).toHaveBeenCalledTimes(1);
		expect(list.data).toEqual(['x']);
		stop();
	});

	it('honours a custom delay', async () => {
		const target = new EventTarget();
		const fetcher = vi.fn().mockResolvedValue(['x']);
		const list = createLiveList<string[]>(fetcher, { match: isMedia, delay: 400, target });
		const stop = list.listen();

		fire(target, { type: 'note', path: 'media/a.md' });
		await vi.advanceTimersByTimeAsync(400);
		expect(fetcher).toHaveBeenCalledTimes(1);
		stop();
	});

	it('does not fetch for an event that does not match', async () => {
		const target = new EventTarget();
		const fetcher = vi.fn().mockResolvedValue(['x']);
		const list = createLiveList<string[]>(fetcher, { match: isMedia, initial: [], target });
		const stop = list.listen();

		fire(target, { type: 'note', path: 'notes/a.md' });
		fire(target, { type: 'paper', path: 'paper/book/pages/p1' });
		fire(target, null);
		await vi.advanceTimersByTimeAsync(1000);
		expect(fetcher).not.toHaveBeenCalled();
		stop();
	});

	it('asks match again when the burst is due', async () => {
		const target = new EventTarget();
		const fetcher = vi.fn().mockResolvedValue({ revision: 'r2' });
		let onScreen = 'r1';
		const list = createLiveList(fetcher, {
			match: (d) => d.type === 'note' && d.revision !== onScreen,
			delay: 400,
			target
		});
		const stop = list.listen();

		// The echo of our own write beats its response; by the deadline the
		// response is on screen and the echo carries nothing new.
		fire(target, { type: 'note', path: 'media/a.md', revision: 'r2' });
		onScreen = 'r2';
		await vi.advanceTimersByTimeAsync(400);
		expect(fetcher).not.toHaveBeenCalled();

		// A burst with one real change in it still loads.
		fire(target, { type: 'note', path: 'media/a.md', revision: 'r3' });
		fire(target, { type: 'note', path: 'media/a.md', revision: 'r4' });
		onScreen = 'r3';
		await vi.advanceTimersByTimeAsync(400);
		expect(fetcher).toHaveBeenCalledTimes(1);
		stop();
	});

	it('cleanup removes the listener and cancels a pending timer', async () => {
		const target = new EventTarget();
		const fetcher = vi.fn().mockResolvedValue(['x']);
		const list = createLiveList<string[]>(fetcher, { match: isMedia, initial: [], target });
		const stop = list.listen();

		fire(target, { type: 'note', path: 'media/a.md' });
		expect(vi.getTimerCount()).toBe(1);
		stop();
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(1000);
		expect(fetcher).not.toHaveBeenCalled();

		fire(target, { type: 'note', path: 'media/a.md' });
		expect(vi.getTimerCount()).toBe(0);
		await vi.advanceTimersByTimeAsync(1000);
		expect(fetcher).not.toHaveBeenCalled();
	});
});
