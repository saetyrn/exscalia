/** The version-history list, as plain functions of its newest-first rows:
 * appending an older page, the size change each row made, and where a new
 * day starts. Pinned down in historylist.test.ts; the route renders them.
 */

import type { HistoryVersion } from './api';

type Row = Pick<HistoryVersion, 'sha' | 'size' | 'deleted' | 'committed_at'>;

/** Append an older page. A save between the two fetches shifts the window
 *  toward older versions, so the page can open with rows already listed. */
export function appendOlder<T extends Pick<HistoryVersion, 'sha'>>(
	versions: readonly T[],
	older: readonly T[]
): T[] {
	const seen = new Set(versions.map((version) => version.sha));
	return [...versions, ...older.filter((version) => !seen.has(version.sha))];
}

/** Bytes this version added or removed against the next-older one ("+312 B",
 *  "−45 B"). Null for the last loaded row and wherever the note was absent. */
export function sizeDelta(versions: readonly Row[], index: number): string | null {
	const version = versions[index];
	const older = versions[index + 1];
	if (!version || !older || version.deleted || older.deleted) return null;
	const delta = version.size - older.size;
	const bytes = Math.abs(delta);
	const amount = bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
	return delta > 0 ? `+${amount}` : delta < 0 ? `−${amount}` : amount;
}

// The browser's calendar day, like the row's own toLocaleString() — the ISO
// date prefix is the committing server's day, which rolls over at a different
// hour.
function localDay(committedAt: string): string {
	return new Date(committedAt).toDateString();
}

/** A day header's label when this row starts a new local day, else null. */
export function dayBreak(versions: readonly Row[], index: number): string | null {
	const at = versions[index].committed_at;
	if (index > 0 && localDay(versions[index - 1].committed_at) === localDay(at)) return null;
	return new Date(at).toLocaleDateString(undefined, {
		weekday: 'short',
		month: 'short',
		day: 'numeric',
		year: 'numeric'
	});
}
