// What the preloader decides to fetch, kept apart from the fetching itself.
//
// These are the judgement calls — which posters, which notes, whether to run at
// all — and they are the parts that must not be wrong, so they live here as
// plain functions with no side effects and no browser behind them.

import type { MediaHub } from './api';

/** Posters warmed per pass. The hub shows far fewer above the fold; this is the
 * bound that keeps a large watchlist from turning a preload into a download. */
export const MAX_WARM_COVERS = 30;
/** How many of the recent list to pull bodies for. The home screen shows three. */
export const MAX_WARM_NOTES = 3;
/** Where the backend files daily notes; mirrors DAILY_DIR in app/api/meta.py. */
export const DAILY_DIR = 'daily';

/** The one thing we ask `navigator` about. `connection` is not in every lib.dom,
 * and taking it structurally is also what lets a test hand one over. */
export interface SaveDataSource {
	connection?: { saveData?: boolean };
}

/** True when the browser is on Data Saver. Speculative fetching is exactly the
 * thing that setting exists to stop, so we honour it by doing nothing at all. */
export function saveDataEnabled(
	source: SaveDataSource | undefined = globalThis.navigator as SaveDataSource | undefined
): boolean {
	return source?.connection?.saveData === true;
}

/** Every URL worth warming from a hub payload, most-visible first, capped. */
export function coverUrls(hub: MediaHub, limit = MAX_WARM_COVERS): string[] {
	const urls: string[] = [];
	const add = (url: string | null | undefined) => {
		if (url && !urls.includes(url)) urls.push(url);
	};
	// The profile art is the largest thing on the page and the first thing drawn.
	add(hub.profile.avatar);
	add(hub.profile.banner);
	for (const item of hub.favorites) add(item.cover);
	for (const entry of hub.recent) add(entry.item.cover);
	for (const item of hub.watchlist) add(item.cover);
	return urls.slice(0, limit);
}

/** Today's daily note path, by the browser's calendar. The server files these
 * in its own timezone, so a mismatch across midnight just means the path is not
 * in the vault listing and nothing is prefetched — which is the safe answer. */
export function todaysDailyPath(now = new Date()): string {
	const month = String(now.getMonth() + 1).padStart(2, '0');
	const day = String(now.getDate()).padStart(2, '0');
	return `${DAILY_DIR}/${now.getFullYear()}-${month}-${day}.md`;
}

/**
 * The notes worth having in hand: the top of the recent list, plus today's
 * daily — but the daily *only* when the vault already lists it.
 *
 * That guard is the whole point. The app opens a daily note through a
 * get-or-create POST; asking for one that does not exist would silently write a
 * note into the vault as a side effect of the user opening the app. Everything
 * here is filtered against the same listing, so a path renamed or deleted since
 * it was recorded is dropped instead of fetched into a 404.
 */
export function notesToPrefetch(
	recent: string[],
	existing: Set<string>,
	dailyPath: string,
	limit = MAX_WARM_NOTES
): string[] {
	const paths = recent.slice(0, limit).filter((path) => existing.has(path));
	if (existing.has(dailyPath) && !paths.includes(dailyPath)) paths.push(dailyPath);
	return paths;
}
