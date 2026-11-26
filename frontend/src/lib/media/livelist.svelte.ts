// A value a page shows and keeps current from the vault's live events: the
// media library, diary and watchlist, a media item, the Paper library and a
// notebook. One copy of the rules those pages used to paste in five
// variants:
//
// - Only the newest load may land. Sort changes, live refreshes and retries
//   overlap, and a slow response must not put back what a later one replaced.
// - A failed load keeps the last good value on screen and says so through
//   `failed`. Blanking it to null showed "Loading…" — false — until some
//   other note happened to change.
// - A burst of live events is one load, `delay` ms after the last of them.
//
// The page owns the effects: `$effect(() => list.listen())` installs the
// listener and the returned cleanup is the teardown.
import { untrack } from 'svelte';
import type { LiveNoteEvent } from '$lib/live';

export interface LiveListOptions<T> {
	/** Whether a live event concerns this page. Asked when the event arrives
	 * and again when the burst it joined is due: an event can stop mattering
	 * while it waits (a write's own echo, once its response is on screen). */
	match: (detail: LiveNoteEvent) => boolean;
	/** Coalescing window in ms (default 600). */
	delay?: number;
	/** What the route's loader fetched. The loaders answer null when that
	 * fetch failed, so a null here starts out `failed`. */
	initial?: T | null;
	/** Where `exscalia:live` is dispatched (default `window`). */
	target?: EventTarget;
}

export interface LiveList<T> {
	/** The last good value, or null while nothing has loaded. */
	readonly data: T | null;
	/** The newest load failed; `data` is what the one before it brought. */
	readonly failed: boolean;
	/** A load is on the wire. Not reactive: read it in handlers. */
	readonly pending: boolean;
	load(): Promise<void>;
	/** Paint a value the caller knows is current, such as a write's own
	 * response. A load already on the wire can still land after it, so a
	 * caller that cares reloads while `pending`. */
	set(value: T): void;
	/** Start listening for live events; returns the cleanup. */
	listen(): () => void;
}

/** A media note written or removed: what the hub's lists are made of. */
export const isMediaNoteEvent = (d: LiveNoteEvent): boolean =>
	(d.type === 'note' || d.type === 'note_removed' || d.type === 'note_moved') &&
	(!!d.path?.startsWith('media/') || !!d.new_path?.startsWith('media/'));

export function createLiveList<T>(
	fetcher: () => Promise<T>,
	opts: LiveListOptions<T>
): LiveList<T> {
	let data: T | null = $state(opts.initial ?? null);
	let failed = $state(opts.initial === null);
	let seq = 0;
	let inFlight = 0;

	async function load(): Promise<void> {
		const mine = ++seq;
		inFlight++;
		try {
			// Untracked: a retry effect calling load() must not subscribe to
			// whatever the fetcher happens to read (a sort, a path).
			const next = await untrack(fetcher);
			if (mine !== seq) return;
			data = next;
			failed = false;
		} catch {
			if (mine === seq) failed = true;
		} finally {
			inFlight--;
		}
	}

	function listen(): () => void {
		const target = opts.target ?? window;
		const delay = opts.delay ?? 600;
		let timer: ReturnType<typeof setTimeout> | null = null;
		let burst: LiveNoteEvent[] = [];
		const onLive = (event: Event) => {
			const detail = (event as CustomEvent<LiveNoteEvent | undefined>).detail;
			if (!detail || !opts.match(detail)) return;
			burst.push(detail);
			if (timer) clearTimeout(timer);
			timer = setTimeout(() => {
				timer = null;
				const due = burst;
				burst = [];
				if (due.some((d) => opts.match(d))) void load();
			}, delay);
		};
		target.addEventListener('exscalia:live', onLive);
		return () => {
			target.removeEventListener('exscalia:live', onLive);
			if (timer) clearTimeout(timer);
			timer = null;
			burst = [];
		};
	}

	return {
		get data() {
			return data;
		},
		get failed() {
			return failed;
		},
		get pending() {
			return inFlight > 0;
		},
		load,
		set(value: T) {
			data = value;
			failed = false;
		},
		listen
	};
}
