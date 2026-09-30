// Shared wording and card/timeline formatting for the media pages.
import type { MediaItem, MediaStatus, MediaType } from '$lib/api';

// The name to show for an item: its custom alias when set, else the real title.
export function displayTitle(item: { title: string; alias?: string | null }): string {
	return item.alias?.trim() || item.title;
}

// A tracked title only counts as the same item when it is the same kind: the
// book "Dune" must not stop the film "Dune" from being added or logged.
export function trackedAs(
	match: { tracked: boolean; path?: string; item?: MediaItem | null },
	type: MediaType
): boolean {
	return match.tracked && !!match.path && (!match.item || match.item.media_type === type);
}

export const TYPE_WORD: Record<MediaType, string> = {
	book: 'Book',
	movie: 'Movie',
	tv: 'TV',
	game: 'Game'
};

export function statusLabel(status: '' | MediaStatus, type: '' | MediaType): string {
	if (status === '') return 'Any status';
	// Planned IS the watchlist — one concept, one word, everywhere.
	if (status === 'planned') return 'Watchlist';
	if (status === 'active') {
		if (type === 'book') return 'Reading';
		if (type === 'game') return 'Playing';
		if (type === 'movie' || type === 'tv') return 'Watching';
		return 'Active';
	}
	return status.charAt(0).toUpperCase() + status.slice(1);
}

export function progressNumber(progress: string | null): number | null {
	if (!progress) return null;
	const match = progress.match(/^(?:page|episode)\s+(\d+)$/i) ?? progress.match(/^\d+$/);
	return match ? Number(match[1] ?? match[0]) : null;
}

export function cardProgress(item: MediaItem): string | null {
	const current = progressNumber(item.progress);

	if (item.media_type === 'book') {
		if (item.status === 'active') return current == null ? null : `Page ${current}`;
		return statusLabel(item.status, item.media_type);
	}
	if (item.media_type === 'movie') {
		return null;
	}
	if (item.media_type === 'tv') {
		// SxxEyy is a position, not a count: the total is series-wide, so the
		// in-season episode over it would read low and restart every season.
		const position = item.progress?.match(/^S\d+E\d+$/i);
		if (position && item.status !== 'finished') return position[0].toUpperCase();
		const watched = current ?? (item.status === 'finished' ? item.total : 0);
		if (item.total != null && watched != null) {
			return `${Math.min(watched, item.total)}/${item.total} Watched`;
		}
		if (current != null) return `${current} Watched`;
		return statusLabel(item.status, item.media_type);
	}
	if (item.status === 'active') return 'In progress';
	return statusLabel(item.status, item.media_type);
}

// Ratings are stored 1–10 (whole numbers, log grammar unchanged) but always
// SHOWN as five stars with halves: 7 → ★★★½. The two scales map exactly.
export function starText(rating10: number | string | null): string {
	const value = Number(rating10);
	if (!Number.isFinite(value) || value < 1) return '';
	return '★'.repeat(Math.floor(value / 2)) + (value % 2 ? '½' : '');
}

export function starTitle(rating10: number): string {
	const stars = rating10 / 2;
	return `${stars} star${stars === 1 ? '' : 's'}`;
}

export interface LogLike {
	at: string;
	kind: string;
	value: string | null;
	text: string;
	path?: string;
}

/** Same-day "started" + "finished" pairs collapse into the finished entry,
 * tagged with its mate so renderers phrase them as one completed sitting
 * ("Watched") and deletion can remove both underlying lines. Keyed by date
 * plus path so multi-item feeds never merge across different media. */
export function mergeDayBookends<T extends LogLike>(events: T[]): (T & { bookend?: T })[] {
	const used = new Set<T>();
	const out: (T & { bookend?: T })[] = [];
	for (const event of events) {
		if (used.has(event)) continue;
		if (event.kind === 'finished') {
			const mate = events.find(
				(other) =>
					other !== event &&
					!used.has(other) &&
					other.kind === 'started' &&
					other.at === event.at &&
					(other.path ?? '') === (event.path ?? '')
			);
			if (mate) {
				used.add(mate);
				out.push({ ...event, bookend: mate });
				continue;
			}
		}
		out.push(event);
	}
	return out;
}

/** The one-sitting verb: started and finished on the same day. */
export function completionVerb(mediaType: string | null | undefined): string {
	if (mediaType === 'book') return 'Read';
	if (mediaType === 'game') return 'Played';
	return 'Watched';
}

export function eventPhrase(
	event: { kind: string; value: string | null; text: string; bookend?: unknown },
	mediaType?: string | null
): string {
	if (event.kind === 'finished' && event.bookend) {
		const verb = completionVerb(mediaType).toLowerCase();
		return event.value ? `${verb}, rated ${starText(event.value)}` : verb;
	}
	if (event.kind === 'progress') return event.value ?? event.text;
	if (event.kind === 'finished')
		return event.value ? `finished, rated ${starText(event.value)}` : 'finished';
	if (event.kind === 'rated') return `rated ${starText(event.value)}`;
	if (event.kind === 'note') return event.text;
	return event.kind;
}

export function monthOf(at: string): string {
	return new Date(`${at}T00:00:00`).toLocaleDateString(undefined, {
		month: 'long',
		year: 'numeric'
	});
}

export function dayOf(at: string): string {
	return new Date(`${at}T00:00:00`).toLocaleDateString(undefined, {
		month: 'short',
		day: 'numeric'
	});
}

// Diary-style calendar chip pieces: uppercase month abbreviation + full year,
// and the zero-padded day-of-month.
export function monthAbbrev(at: string): string {
	return new Date(`${at}T00:00:00`)
		.toLocaleDateString(undefined, { month: 'short' })
		.toUpperCase();
}

export function yearOf(at: string): string {
	return new Date(`${at}T00:00:00`).toLocaleDateString(undefined, { year: 'numeric' });
}

export function dayNum(at: string): string {
	return String(new Date(`${at}T00:00:00`).getDate()).padStart(2, '0');
}
