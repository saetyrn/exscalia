// The two primitives the preloaded caches are built from.
//
// Neither knows anything about runes, so they can be tested straight; the
// `.svelte.ts` stores wrap them and hand a write callback in. Both exist for
// the same reason: a payload fetched ahead of time is only worth having if the
// page can trust it, which means one request per burst and a hard limit on how
// old a cached answer is allowed to be before it is refetched instead.

/** Callbacks a store hands `dedupe` so it can mirror the request into $state. */
export interface DedupeHooks<T> {
	/** A request just went on the wire. */
	onStart?: () => void;
	/** A request came back. Fires before the caller's `.then`. */
	onValue?: (value: T) => void;
	/** The request finished, either way. */
	onSettled?: () => void;
}

/**
 * Wrap a fetch so overlapping callers share one request.
 *
 * The preloader and a page mount routinely ask for the same payload within a
 * frame of each other, and a live-event burst asks several times in a row —
 * without this, "preload it early" would mean "fetch it twice".
 *
 * The returned promise rejects exactly as the underlying fetch does; callers
 * keep whatever error handling they already had.
 */
export function dedupe<T>(fetcher: () => Promise<T>, hooks: DedupeHooks<T> = {}): () => Promise<T> {
	let inflight: Promise<T> | null = null;
	return () => {
		if (inflight) return inflight;
		hooks.onStart?.();
		inflight = fetcher()
			.then((value) => {
				hooks.onValue?.(value);
				return value;
			})
			.finally(() => {
				// Cleared on failure too — one bad request must not wedge the slot
				// shut for the rest of the session.
				inflight = null;
				hooks.onSettled?.();
			});
		return inflight;
	};
}

/**
 * A keyed store whose entries expire.
 *
 * Used for prefetched note bodies, where serving something stale is worse than
 * serving nothing: an entry past its time is simply not there, and the caller
 * falls back to the fetch it would have made anyway.
 */
export class FreshStore<T> {
	#entries = new Map<string, { value: T; at: number }>();
	#ttl: number;
	#now: () => number;

	constructor(ttlMs: number, now: () => number = Date.now) {
		this.#ttl = ttlMs;
		this.#now = now;
	}

	set(key: string, value: T): void {
		this.#entries.set(key, { value, at: this.#now() });
	}

	/** The value if it is present and still fresh, else null. A stale entry is
	 * dropped on the way out rather than left to be re-examined. */
	get(key: string): T | null {
		const entry = this.#entries.get(key);
		if (!entry) return null;
		if (this.#now() - entry.at > this.#ttl) {
			this.#entries.delete(key);
			return null;
		}
		return entry.value;
	}

	/** Read and remove in one step, for values that may only be used once. */
	take(key: string): T | null {
		const value = this.get(key);
		this.#entries.delete(key);
		return value;
	}

	has(key: string): boolean {
		return this.get(key) !== null;
	}

	delete(key: string): void {
		this.#entries.delete(key);
	}

	clear(): void {
		this.#entries.clear();
	}

	get size(): number {
		return this.#entries.size;
	}
}
