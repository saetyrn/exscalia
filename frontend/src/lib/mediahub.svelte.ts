// The /api/media/hub payload, cached at module scope so it outlives the page.
//
// The app shell remounts the page on every navigation ({#key routeKey} in the
// root layout), so a component-local copy dies the moment you leave /media and
// the next visit pays for the round-trip again — which is why the Media tab in
// the mobile bar showed "Loading…" every single time. Holding the payload here
// lets the page paint from the last one and revalidate behind it, and lets the
// preloader fill it before the first visit.
//
// Display only. Every media mutation still goes through the API and the vault's
// note_changed broadcast; nothing in this file is ever a write path.

import { api, type MediaHub } from './api';
import { dedupe } from './cache';

export const mediaHub = $state({
	/** Last payload that landed, or null when none ever has. */
	hub: null as MediaHub | null,
	/** When `hub` landed (epoch ms), 0 while empty. */
	loadedAt: 0,
	/** True while a fetch is on the wire — a revalidation, usually. */
	refreshing: false
});

/** Fetch the hub into the cache. Concurrent callers share one request, so a
 * preload racing a page mount makes one; the returned promise rejects exactly
 * as the fetch does, leaving callers their own error path. */
export const loadHub: () => Promise<MediaHub> = dedupe(() => api.mediaHub(), {
	onStart: () => (mediaHub.refreshing = true),
	onValue: (payload) => {
		mediaHub.hub = payload;
		mediaHub.loadedAt = Date.now();
	},
	onSettled: () => (mediaHub.refreshing = false)
});

/** The cached payload, or null. Callers that must not fetch read this. */
export function cachedHub(): MediaHub | null {
	return mediaHub.hub;
}

let liveWatched = false;
let liveTimer: ReturnType<typeof setTimeout> | null = null;

/** Refresh the cache whenever the vault reports a media note changing.
 *
 * Idempotent: the hub page and the preloader both ask for it and only the first
 * installs, so a rewrite burst (an OMNIA bulk log fires one event per note)
 * costs exactly one refetch no matter who is listening. */
export function watchMediaLive(): void {
	if (liveWatched || typeof window === 'undefined') return;
	liveWatched = true;
	window.addEventListener('exscalia:live', (event) => {
		const detail = (event as CustomEvent).detail as { type?: string; path?: string };
		if (detail?.type !== 'note' && detail?.type !== 'note_removed') return;
		if (!detail.path?.startsWith('media/')) return;
		if (liveTimer) clearTimeout(liveTimer);
		liveTimer = setTimeout(() => {
			liveTimer = null;
			void loadHub().catch(() => {
				// A failed revalidation leaves the last good payload on screen.
			});
		}, 600);
	});
}
