// Month views for /calendar, cached the same way the media hub is.
//
// The calendar draws its grid from the date alone, so it never showed a
// spinner — but its route loader awaited the month payload, which held the
// navigation itself. Cached, the page opens on the frame the tab is tapped and
// the dots settle in behind it.
//
// Display only: creating and editing days still goes through the API.

import { api, type MonthView } from './api';
import { dedupe } from './cache';

/** `YYYY-MM`, the key every month payload is filed under. */
export function monthKey(year: number, month: number): string {
	return `${year.toString().padStart(4, '0')}-${month.toString().padStart(2, '0')}`;
}

export const calendarMonths = $state({
	/** Keyed by monthKey(); a month absent here has never been fetched. */
	byMonth: {} as Record<string, MonthView>
});

/** One deduper per month, so two months in flight at once don't collide. */
const loaders = new Map<string, () => Promise<MonthView>>();

export function loadMonth(year: number, month: number): Promise<MonthView> {
	const key = monthKey(year, month);
	let loader = loaders.get(key);
	if (!loader) {
		loader = dedupe(() => api.monthView(year, month), {
			onValue: (view) => (calendarMonths.byMonth[key] = view)
		});
		loaders.set(key, loader);
	}
	return loader();
}

/** The cached month, or null. Callers that must not fetch read this. */
export function cachedMonth(year: number, month: number): MonthView | null {
	return calendarMonths.byMonth[monthKey(year, month)] ?? null;
}

let liveWatched = false;
let liveTimer: ReturnType<typeof setTimeout> | null = null;

/** Any note change can move a dot on the grid — a daily note appearing, a task
 * finishing, an event landing — so a change anywhere refreshes whichever months
 * are cached. Idempotent, and debounced so a bulk edit costs one pass. */
export function watchCalendarLive(): void {
	if (liveWatched || typeof window === 'undefined') return;
	liveWatched = true;
	window.addEventListener('exscalia:live', (event) => {
		const detail = (event as CustomEvent).detail as { type?: string };
		if (detail?.type !== 'note' && detail?.type !== 'note_removed' && detail?.type !== 'note_moved')
			return;
		if (liveTimer) clearTimeout(liveTimer);
		liveTimer = setTimeout(() => {
			liveTimer = null;
			for (const key of Object.keys(calendarMonths.byMonth)) {
				const [year, month] = key.split('-').map(Number);
				void loadMonth(year, month).catch(() => {
					// The rendered month stays; the next change tries again.
				});
			}
		}, 600);
	});
}
