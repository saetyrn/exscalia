// The primitives the preloaded caches rest on: one request per burst, and a
// cached answer that expires rather than going stale under a reader.
import { describe, expect, it, vi } from 'vitest';
import { dedupe, FreshStore } from './cache';

/** A promise plus the handles to settle it, so a test can hold a fetch open. */
function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

describe('dedupe', () => {
	it('collapses overlapping callers onto one request', async () => {
		const gate = deferred<string>();
		const fetcher = vi.fn(() => gate.promise);
		const load = dedupe(fetcher);

		const a = load();
		const b = load();
		expect(a).toBe(b);
		expect(fetcher).toHaveBeenCalledTimes(1);

		gate.resolve('hub');
		expect(await a).toBe('hub');
		expect(await b).toBe('hub');
	});

	it('starts a fresh request once the previous one has settled', async () => {
		const fetcher = vi
			.fn<() => Promise<string>>()
			.mockResolvedValueOnce('first')
			.mockResolvedValueOnce('second');
		const load = dedupe(fetcher);

		expect(await load()).toBe('first');
		expect(await load()).toBe('second');
		expect(fetcher).toHaveBeenCalledTimes(2);
	});

	it('mirrors the request into the store: start, value, settled', async () => {
		const seen: string[] = [];
		const load = dedupe(() => Promise.resolve(7), {
			onStart: () => seen.push('start'),
			onValue: (value) => seen.push(`value:${value}`),
			onSettled: () => seen.push('settled')
		});

		await load();
		expect(seen).toEqual(['start', 'value:7', 'settled']);
	});

	it('does not wedge the slot shut when a request fails', async () => {
		const fetcher = vi
			.fn<() => Promise<string>>()
			.mockRejectedValueOnce(new Error('offline'))
			.mockResolvedValueOnce('recovered');
		const values: string[] = [];
		const load = dedupe(fetcher, { onValue: (value) => values.push(value) });

		await expect(load()).rejects.toThrow('offline');
		// The failure published nothing — whatever the page had stays on screen.
		expect(values).toEqual([]);
		expect(await load()).toBe('recovered');
		expect(values).toEqual(['recovered']);
	});

	it('reports the failure to every caller of the shared request', async () => {
		const load = dedupe(() => Promise.reject(new Error('boom')));
		const a = load();
		const b = load();
		await expect(a).rejects.toThrow('boom');
		await expect(b).rejects.toThrow('boom');
	});
});

describe('FreshStore', () => {
	/** A hand-cranked clock, so expiry is exact rather than timing-dependent. */
	function clocked<T>(ttl: number) {
		let now = 1_000;
		const store = new FreshStore<T>(ttl, () => now);
		return { store, tick: (ms: number) => (now += ms) };
	}

	it('serves a value while it is fresh', () => {
		const { store } = clocked<string>(60_000);
		store.set('daily/2026-08-21.md', 'body');
		expect(store.get('daily/2026-08-21.md')).toBe('body');
		expect(store.has('daily/2026-08-21.md')).toBe(true);
	});

	it('treats an expired value as absent, and drops it', () => {
		const { store, tick } = clocked<string>(60_000);
		store.set('note.md', 'body');
		tick(60_001);
		expect(store.get('note.md')).toBeNull();
		expect(store.size).toBe(0);
	});

	it('take() serves a value exactly once', () => {
		const { store } = clocked<string>(60_000);
		store.set('note.md', 'body');
		expect(store.take('note.md')).toBe('body');
		// This is what stops a reload after an edit from resurrecting the copy
		// the page opened with.
		expect(store.take('note.md')).toBeNull();
	});

	it('forgets a single entry on demand, and all of them', () => {
		const { store } = clocked<string>(60_000);
		store.set('a.md', 'a');
		store.set('b.md', 'b');
		store.delete('a.md');
		expect(store.get('a.md')).toBeNull();
		expect(store.get('b.md')).toBe('b');
		store.clear();
		expect(store.size).toBe(0);
	});

	it('re-setting a key restarts its clock', () => {
		const { store, tick } = clocked<string>(60_000);
		store.set('note.md', 'old');
		tick(59_000);
		store.set('note.md', 'new');
		tick(59_000);
		expect(store.get('note.md')).toBe('new');
	});
});
