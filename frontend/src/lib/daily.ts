// Opening today's daily note — shared by the sidebar's Daily button and the
// command palette, which each used to carry their own copy and drifted: the
// palette always POSTed and waited for the note list before navigating, the
// button (HANDOFF "Daily button is instant") only did that on the day's first
// press.

import { goto } from '$app/navigation';
import { api, encodePath } from './api';
import { DAILY_DIR } from './preloadplan';
import { refreshNotes, vault } from './stores.svelte';

/** Today on the device's calendar, YYYY-MM-DD — not the server's, and not
 * UTC (toISOString() rolls a day early in the evening west of Greenwich). */
function localDate(now: Date): string {
	const month = String(now.getMonth() + 1).padStart(2, '0');
	const day = String(now.getDate()).padStart(2, '0');
	return `${now.getFullYear()}-${month}-${day}`;
}

/** Today's daily note path, e.g. `daily/2026-09-23.md`. */
export function todayPath(now = new Date()): string {
	return `${DAILY_DIR}/${localDate(now)}.md`;
}

/** Open today's daily note, creating it first only if the vault lacks it.
 * Resolves to the note's path once navigation has finished. */
export async function openToday(): Promise<string> {
	const now = new Date();
	const path = todayPath(now);
	// The note list already knows whether today exists — in the common case
	// navigate straight there with zero extra round-trips. Only the first
	// press of the day needs the get-or-create call, and the note list can
	// learn about the new note after navigation instead of before it.
	if (!vault.notes.some((n) => n.path === path)) {
		await api.openDaily(localDate(now));
		void refreshNotes();
	}
	await goto(`/note/${encodePath(path)}`);
	return path;
}
