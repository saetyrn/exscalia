// Who this player is: a browser (kept in localStorage) and a tab within it
// (sessionStorage, so a reload keeps it). The speaker in the shared session
// is one tab of one browser, so two tabs never both play (MUSIC.md 4).
import { isDesktop } from '$lib/runtime';

function stored(storage: () => Storage, key: string): string {
	try {
		const have = storage().getItem(key);
		if (have) return have;
		const made = crypto.randomUUID().replace(/-/g, '').slice(0, 12);
		storage().setItem(key, made);
		return made;
	} catch {
		return crypto.randomUUID().replace(/-/g, '').slice(0, 12);
	}
}

const browser = typeof window !== 'undefined';
export const DEVICE_ID = browser ? stored(() => localStorage, 'exscalia.music.device') : 'server';
export const TAB_ID = browser ? stored(() => sessionStorage, 'exscalia.music.tab') : 'server';
export const SELF = `${DEVICE_ID}:${TAB_ID}`;

/** A short name for "Playing on …". */
export function deviceName(): string {
	if (!browser) return 'Unknown';
	const ua = navigator.userAgent;
	const kind = /iPhone/.test(ua)
		? 'iPhone'
		: /iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)
			? 'iPad'
			: /Android/.test(ua)
				? 'Android'
				: /Windows/.test(ua)
					? 'Windows'
					: /Mac/.test(ua)
						? 'Mac'
						: 'Browser';
	return isDesktop ? `${kind} app` : kind;
}
