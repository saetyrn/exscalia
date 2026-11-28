// Which recipe the menu had in focus, so coming back to /recipes (Back from a
// recipe, the sidebar, a reload of the tab) lands on it, as in the recipe
// app. Held in the module for this page load and in sessionStorage for a
// reload; either can be missing (private mode, a node test) and that only
// means the menu starts at the top.
import { clamp } from '../carousel';

const KEY = 'recipes:menu-focus';
let held: string | null = null;

function store(): Storage | null {
	try {
		return typeof sessionStorage === 'undefined' ? null : sessionStorage;
	} catch {
		return null;
	}
}

export function rememberFocus(path: string): void {
	held = path;
	try {
		store()?.setItem(KEY, path);
	} catch {
		// Private mode or a full quota: the module copy still holds it.
	}
}

export function recalledFocus(): string | null {
	if (held !== null) return held;
	try {
		return store()?.getItem(KEY) ?? null;
	} catch {
		return null;
	}
}

export function forgetFocus(): void {
	held = null;
	try {
		store()?.removeItem(KEY);
	} catch {
		// Nothing to clear.
	}
}

/** Where focus goes in `cards`: the recipe at `path` wherever it now stands,
 * or, when it is gone (deleted, renamed away), whichever recipe took its
 * place at `fallback`, kept inside the list. */
export function focusIndex(
	cards: readonly { path: string }[],
	path: string | null,
	fallback = 0
): number {
	if (!cards.length) return 0;
	const at = path === null ? -1 : cards.findIndex((card) => card.path === path);
	return at >= 0 ? at : clamp(fallback, 0, cards.length - 1);
}
