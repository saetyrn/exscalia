// The draft an import hands from the add page to the review page. The root
// layout remounts the page on every pathname change, so it cannot ride in
// component state. Like the recipe app, it is also kept in this tab's
// sessionStorage, so a reload of the review page shows the draft again (as
// imported; edits made since are the form's and go with the reload). With no
// draft at all the review page is an empty form to type into.
import type { Draft } from '../types';

export interface Handoff {
	draft: Draft;
	/** A JPEG data URL of the first photo, for the recipe's card. */
	thumb: string | null;
	/** That photo once uploaded, so a second Save does not upload it again. */
	thumbUrl: string | null;
}

const KEY = 'exscalia:recipes:draft';

let current: Handoff | null = $state(null);

function store(value: Handoff | null): void {
	try {
		if (value) sessionStorage.setItem(KEY, JSON.stringify(value));
		else sessionStorage.removeItem(KEY);
	} catch {
		// Private mode or full: the draft lives until a reload.
	}
}

function valid(value: unknown): value is Handoff {
	const v = value as Handoff | null;
	return (
		!!v &&
		typeof v === 'object' &&
		!!v.draft &&
		typeof v.draft === 'object' &&
		!!v.draft.recipe &&
		typeof v.draft.recipe === 'object'
	);
}

/** Hand a new draft to the review page. */
export function setDraft(draft: Draft, thumb: string | null = null): void {
	current = { draft, thumb, thumbUrl: null };
	store(current);
}

/** The draft waiting for review, from this page load or the tab's storage. */
export function readDraft(): Handoff | null {
	if (current) return current;
	try {
		const raw = sessionStorage.getItem(KEY);
		const parsed: unknown = raw ? JSON.parse(raw) : null;
		if (valid(parsed)) {
			current = {
				draft: {
					recipe: parsed.draft.recipe,
					missing: Array.isArray(parsed.draft.missing) ? parsed.draft.missing.map(String) : [],
					method: parsed.draft.method
				},
				thumb: typeof parsed.thumb === 'string' ? parsed.thumb : null,
				thumbUrl: typeof parsed.thumbUrl === 'string' ? parsed.thumbUrl : null
			};
		}
	} catch {
		// Unreadable: no draft.
	}
	return current;
}

/** Remember the uploaded card photo for this draft. */
export function setThumbUrl(url: string): void {
	if (!current) return;
	current.thumbUrl = url;
	store(current);
}

/** The draft became a recipe, or was abandoned for a new one. */
export function clearDraft(): void {
	current = null;
	store(null);
}
