// The decisions behind the media item page's mobile action bar, kept out of
// the component so they can be tested without a DOM.
//
// Everything here is pure: it reads an item plus its log and answers "what
// would you be logging next?", "what does this form POST?", "where does an
// item go when it leaves the watchlist?". The component owns the API calls.

import type { MediaDetail, MediaItem, MediaStatus, MediaType } from '$lib/api';

export type LogEventLite = MediaDetail['events'][number];

/** Life-cycle order, which is also the order the status sheet lists them:
 * you plan a thing, you start it, then it ends one way or the other. */
export const STATUS_ORDER: MediaStatus[] = ['planned', 'active', 'finished', 'dropped'];

/** Today by the browser's own clock, YYYY-MM-DD. Never toISOString(): that is
 * UTC, so a 23:00 log in a positive offset would land on tomorrow. */
export function todayISO(now: Date = new Date()): string {
	const month = String(now.getMonth() + 1).padStart(2, '0');
	const day = String(now.getDate()).padStart(2, '0');
	return `${now.getFullYear()}-${month}-${day}`;
}

/** The newest canonical progress value ("page 120", "S02E05", "episode 4"),
 * from the item's projected `progress` column or, failing that, its log. */
function latestProgress(
	item: Pick<MediaItem, 'progress'> | null,
	events: LogEventLite[]
): string | null {
	return item?.progress ?? events.find((e) => e.kind === 'progress')?.value ?? null;
}

/** What to put in the log form before the user touches it.
 *
 * Books prefill their CURRENT page — the number you are about to type over,
 * not a guess at where you stopped. TV prefills the NEXT episode, because
 * that one is derivable and is almost always what you just watched. */
export function suggestedProgress(
	mediaType: MediaType,
	item: Pick<MediaItem, 'progress'> | null,
	events: LogEventLite[]
): string {
	const latest = latestProgress(item, events);
	if (mediaType === 'book') {
		const page = latest?.match(/^page\s+(\d+)$/i);
		return page ? page[1] : '';
	}
	if (mediaType === 'tv') {
		const se = latest?.match(/^s(\d{1,2})\s*e(\d{1,3})$/i);
		if (se) {
			return `S${String(Number(se[1])).padStart(2, '0')}E${String(Number(se[2]) + 1).padStart(2, '0')}`;
		}
		const word = latest?.match(/^episode\s+(\d+)$/i);
		if (word) return String(Number(word[1]) + 1);
		return 'S01E01';
	}
	return '';
}

export interface LogDraft {
	/** Page or episode, as typed. Ignored by movies and games. */
	value: string;
	/** A game's session note. */
	note: string;
	/** Books, TV and games can end their entry here instead of advancing it. */
	finished: boolean;
	/** 1–10, the stored scale; null when the user set no stars. */
	rating: number | null;
	/** YYYY-MM-DD, or '' to let the server stamp its own today. */
	date: string;
}

export interface LogRequest {
	kind: 'progress' | 'note' | 'watched' | 'finished';
	value?: string;
	text?: string;
	date?: string;
	rating?: number;
}

export function emptyDraft(
	mediaType: MediaType,
	item: Pick<MediaItem, 'progress'> | null,
	events: LogEventLite[]
): LogDraft {
	return {
		value: suggestedProgress(mediaType, item, events),
		note: '',
		finished: false,
		rating: null,
		date: todayISO()
	};
}

/** Turn the sheet's fields into the POST body, or into the message to show.
 *
 * `date` is omitted when it is still the browser's today, so the ordinary
 * case keeps the server's own timezone as the authority for the stamp — the
 * same log line the desktop quick-log writes. Only a date the user actually
 * changed travels with the request. */
export function buildLogRequest(
	mediaType: MediaType,
	draft: LogDraft,
	today: string = todayISO()
): { body: LogRequest } | { error: string } {
	const date = draft.date && draft.date !== today ? draft.date : undefined;
	const rating = draft.rating ?? undefined;

	if (mediaType === 'movie') {
		// Watching a film is finishing it; there is no half-way to log.
		return { body: { kind: 'watched', date, rating } };
	}
	if (draft.finished) {
		return { body: { kind: 'finished', date, rating } };
	}

	const value = draft.value.trim();
	if (mediaType === 'book') {
		if (!/^\d+$/.test(value) || Number(value) < 1) return { error: 'Enter your current page.' };
		return { body: { kind: 'progress', value, date } };
	}
	if (mediaType === 'tv') {
		if (!/^s?\d{1,2}\s*e\d{1,3}$/i.test(value) && !/^\d+$/.test(value)) {
			return { error: 'Enter an episode like S02E05 (or a number).' };
		}
		return { body: { kind: 'progress', value, date } };
	}
	const text = draft.note.trim();
	if (!text) return { error: 'Write a short note about the session.' };
	return { body: { kind: 'note', text, date } };
}

/** Where an item lands when it comes off the watchlist.
 *
 * There is no "no status" — the four are planned/active/finished/dropped — so
 * leaving the watchlist has to name one, and it should not lie. In one
 * session the toggle is exactly reversible: `remembered` is whatever the item
 * was when it went ON the watchlist. Otherwise the item's own history
 * decides: anything already underway goes back to active, and something never
 * started goes to dropped, which is what "took it off my list" means.
 *
 * `finished` is deliberately never chosen: a status PATCH to finished appends
 * a fresh "finished" log line, and a toggle must not invent history. */
export function unwatchlistStatus(
	item: Pick<MediaItem, 'started' | 'progress'> | null,
	events: LogEventLite[],
	remembered: MediaStatus | null = null
): MediaStatus {
	if (remembered === 'active' || remembered === 'dropped') return remembered;
	if (item?.started || item?.progress) return 'active';
	const underway = events.some(
		(e) => e.kind === 'progress' || e.kind === 'started' || e.kind === 'watched'
	);
	return underway ? 'active' : 'dropped';
}
