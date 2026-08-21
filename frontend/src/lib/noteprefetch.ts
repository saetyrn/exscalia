// Note bodies fetched ahead of the click, for the handful of notes the user is
// most likely to open next.
//
// This is the most careful cache in the app, because notes are the one thing
// here that is also a write path: rendering a stale body would mean editing on
// top of a revision the server has already moved past. Three rules keep that
// from happening, and the note page's own machinery catches anything they miss.
//
//   1. An entry is used once and then gone — a reload after an edit must hit
//      the server, never resurrect the copy we opened with.
//   2. An entry older than a minute is treated as absent.
//   3. A live note_changed for a path drops that path's entry immediately.
//
// And the page revalidates behind the cached render through the same safePull()
// the live stream uses, so the worst case is one wasted GET, not a stale editor.

import { api, type Note } from './api';
import { FreshStore } from './cache';

/** Long enough to cover the walk from app open to first tap; short enough that
 * a missed live event cannot leave a stale body sitting around. */
export const NOTE_PREFETCH_TTL_MS = 60_000;

const notes = new FreshStore<Note>(NOTE_PREFETCH_TTL_MS);

/** Fetch a note into the cache. Never throws — a prefetch that fails is simply
 * a prefetch that did not happen. */
export async function prefetchNote(path: string): Promise<void> {
	if (!path || notes.has(path)) return;
	try {
		notes.set(path, await api.readNote(path));
	} catch {
		// Deleted, renamed, or offline — the page fetches normally.
	}
}

/** Take the prefetched body for `path`, if one is still fresh. Consuming it
 * removes it: this is a first-render fast path, not a store. */
export function takePrefetchedNote(path: string): Note | null {
	return notes.take(path);
}

export function forgetPrefetchedNote(path: string): void {
	notes.delete(path);
}

export function clearPrefetchedNotes(): void {
	notes.clear();
}

let liveWatched = false;

/** Drop a prefetched body the moment the vault says that note changed. */
export function watchNoteLive(): void {
	if (liveWatched || typeof window === 'undefined') return;
	liveWatched = true;
	window.addEventListener('exscalia:live', (event) => {
		const detail = (event as CustomEvent).detail as { type?: string; path?: string };
		if (detail?.type !== 'note' && detail?.type !== 'note_removed') return;
		if (detail.path) notes.delete(detail.path);
	});
}
