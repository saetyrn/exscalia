// Background tab preloading.
//
// The bottom tab bar promises four instant destinations, and two of them were
// not: Home and /notes render from the vault store loaded at boot, but /media
// fetched its hub on every mount and /omnia arrived with an empty thread rail.
// The same held for /calendar and for the notes on the home screen's recent
// list. This module fills all of those caches while the user is still looking
// at whatever they opened the app on, so the first tap has nothing left to
// fetch.
//
// Everything here is speculative work. It runs at idle, one request at a time,
// never on Data Saver, and every failure is swallowed — a preload that does not
// happen must cost nothing but the round-trip it was trying to save. Nothing
// here creates or writes anything: the daily note in particular is reached
// through a get-or-create POST, so it is prefetched only when the vault already
// lists it, and never through that endpoint.

import type { MediaHub } from './api';
import { loadMonth, watchCalendarLive } from './calendarview.svelte';
import { loadHub, watchMediaLive } from './mediahub.svelte';
import { prefetchNote, watchNoteLive } from './noteprefetch';
import { coverUrls, notesToPrefetch, saveDataEnabled, todaysDailyPath } from './preloadplan';
import { isDesktop } from './runtime';
import {
	omniaThreads,
	openOmniaThread,
	overseerChat,
	recentNotes,
	refreshThreads,
	vault
} from './stores.svelte';

/** Fallback delay when requestIdleCallback is missing (Safari, older WebKit). */
const IDLE_FALLBACK_MS = 1500;
/** Away this long and the caches are worth rebuilding on return. */
const STALE_AFTER_HIDDEN_MS = 5 * 60 * 1000;

/** requestIdleCallback is still missing from Safari's lib.dom in places, so it
 * is read off the window structurally rather than assumed. */
interface IdleWindow {
	requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
	cancelIdleCallback?: (handle: number) => void;
}

/** Run `task` when the browser is next idle; returns a canceller. */
function whenIdle(task: () => void): () => void {
	const view = window as unknown as IdleWindow;
	if (typeof view.requestIdleCallback === 'function') {
		// The timeout matters: on a page that never goes idle the callback would
		// otherwise never fire and the tabs would stay cold.
		const handle = view.requestIdleCallback(task, { timeout: IDLE_FALLBACK_MS * 2 });
		return () => view.cancelIdleCallback?.(handle);
	}
	const timer = setTimeout(task, IDLE_FALLBACK_MS);
	return () => clearTimeout(timer);
}

/** Decoded images are held until the batch is replaced: an <img> nobody
 * references can be collected mid-flight, cancelling the very request we
 * wanted the HTTP cache to keep. */
let warmed: HTMLImageElement[] = [];

function warmCovers(hub: MediaHub): void {
	// Desktop attachments need a bearer header an <img> cannot send; that build
	// swaps them for blob URLs at render time instead, so there is nothing here
	// for the browser cache to hold.
	if (isDesktop) return;
	warmed = coverUrls(hub).map((url) => {
		const image = new Image();
		image.decoding = 'async';
		image.src = url;
		return image;
	});
}

async function warmOmnia(): Promise<void> {
	// The /omnia page owns its own loading while it is mounted — stepping on it
	// could replace a transcript the user is mid-conversation in.
	if (window.location.pathname.startsWith('/omnia')) return;
	// The thread list is the sidebar rail; without it /omnia opens blank and
	// fills a beat later.
	await refreshThreads();
	// The page opens whichever thread was last active, falling back to the most
	// recent one — warm exactly that transcript and nothing else.
	const target = overseerChat.sessionId ?? omniaThreads.items[0]?.id;
	if (!target) return;
	if (overseerChat.sessionId === target && overseerChat.messages.length > 0) return;
	await openOmniaThread(target).catch(() => {
		// A deleted thread just means the page falls back on mount, as before.
	});
}

async function warmNotes(): Promise<void> {
	// refreshNotes() runs at auth-ok; if it has not landed yet there is nothing
	// to check paths against, and guessing is exactly what must not happen here.
	if (vault.notes.length === 0) return;
	const existing = new Set(vault.notes.map((note) => note.path));
	for (const path of notesToPrefetch(recentNotes.paths, existing, todaysDailyPath())) {
		await prefetchNote(path);
	}
}

let running = false;

/** One pass, in the order the tab bar is likely to be tapped. Re-entrant
 * callers are dropped. */
async function runPreload(): Promise<void> {
	if (running || saveDataEnabled()) return;
	running = true;
	try {
		// Sequential on purpose. This is work queued behind whatever the user is
		// actually doing, so it takes one connection at a time rather than
		// contending with the page they are looking at.
		const hub = await loadHub().catch(() => null);
		await warmOmnia();
		const now = new Date();
		await loadMonth(now.getFullYear(), now.getMonth() + 1).catch(() => null);
		await warmNotes();
		if (hub) whenIdle(() => warmCovers(hub));
	} finally {
		running = false;
	}
}

/**
 * Start preloading. Call once the app is authenticated and mounted; the
 * returned function tears the listeners down.
 *
 * Not gated to mobile — the desktop shell switches between the same views and
 * gets the same benefit.
 */
export function startPreload(): () => void {
	if (typeof window === 'undefined') return () => {};
	if (saveDataEnabled()) return () => {};

	// Keep every cache honest about vault changes, whoever ends up reading them.
	watchMediaLive();
	watchCalendarLive();
	watchNoteLive();

	const cancelIdle = whenIdle(() => void runPreload());

	// Coming back after a long absence, the caches describe a vault that may
	// have moved on — on another device, or through OMNIA's own background work.
	let hiddenAt = 0;
	const onVisibility = () => {
		if (document.visibilityState === 'hidden') {
			hiddenAt = Date.now();
			return;
		}
		const away = hiddenAt ? Date.now() - hiddenAt : 0;
		hiddenAt = 0;
		if (away > STALE_AFTER_HIDDEN_MS) void runPreload();
	};
	document.addEventListener('visibilitychange', onVisibility);

	return () => {
		cancelIdle();
		document.removeEventListener('visibilitychange', onVisibility);
	};
}
