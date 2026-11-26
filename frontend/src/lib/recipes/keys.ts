// The recipe screens' single-key shortcuts (arrows, Escape, E, K) share one
// guard, after the recipe app's: a key belongs to the screen only when nothing
// else has a claim on it.
import { modalOpen } from '$lib/modal';
import { leaving } from './transition';

const TYPING = 'input, textarea, select, [contenteditable=""], [contenteditable="true"], .cm-editor';

/** True when a keydown is the screen's to act on: not handled already, no
 * Cmd, Ctrl or Alt chord, no slide asked for or running (the page leaving
 * stays mounted until the next one is loaded and captured, and would take the
 * key), not typed into a field (the servings slider keeps its arrows), and no
 * dialog, sheet or palette open over the page. Shift is left to the caller. */
export function plainKey(e: KeyboardEvent): boolean {
	if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return false;
	if (leaving()) return false;
	const target = e.target;
	if (typeof Element !== 'undefined' && target instanceof Element && target.closest(TYPING))
		return false;
	return !modalOpen();
}
