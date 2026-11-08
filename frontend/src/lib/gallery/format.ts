// Small words the Gallery shows: how long since a board was saved to, and
// how many pictures it holds.

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Pinterest's short age: "now", "5m", "3h", "4d", "2w", "6mo", "1y". */
export function shortAge(iso: string | null | undefined, now: number = Date.now()): string {
	if (!iso) return '';
	const then = Date.parse(iso);
	if (!Number.isFinite(then)) return '';
	const gap = Math.max(0, now - then);
	if (gap < MINUTE) return 'now';
	if (gap < HOUR) return `${Math.floor(gap / MINUTE)}m`;
	if (gap < DAY) return `${Math.floor(gap / HOUR)}h`;
	if (gap < 7 * DAY) return `${Math.floor(gap / DAY)}d`;
	if (gap < 30 * DAY) return `${Math.floor(gap / (7 * DAY))}w`;
	if (gap < 365 * DAY) return `${Math.floor(gap / (30 * DAY))}mo`;
	return `${Math.floor(gap / (365 * DAY))}y`;
}

export function pictureCount(n: number): string {
	if (n >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1).replace(/\.0$/, '')}k pictures`;
	return n === 1 ? '1 picture' : `${n} pictures`;
}

/** Image files out of a drop, a paste or a picker; anything else is left. */
export function imageFiles(files: Iterable<File> | ArrayLike<File> | null | undefined): File[] {
	if (!files) return [];
	return Array.from(files as ArrayLike<File>).filter((f) =>
		/^image\/(png|jpe?g|gif|webp)$/i.test(f.type)
	);
}
