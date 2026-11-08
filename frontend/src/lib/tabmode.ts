// Tab must always indent.
//
// CodeMirror ships an accessibility escape hatch that this app's Escape-heavy
// UI trips constantly: pressing Escape arms a two-second window in which Tab
// bypasses every keymap and moves browser focus instead, and `toggleTabFocusMode`
// (Ctrl-m, Shift-Alt-m on macOS, bound by defaultKeymap) turns the same thing on
// permanently until the view is rebuilt. Nothing in the app ever turned it back
// off — that is the "tabbing doesn't work sometimes" bug.
//
// Both doors are closed here: Escape releases the mode, and the toggle binding
// is swallowed before defaultKeymap can see it.

import { Prec, type Extension } from '@codemirror/state';
import { EditorView, keymap, type KeyBinding } from '@codemirror/view';

/** The slice of EditorView this module needs — small enough that the handler
 *  can be unit-tested against a stub. */
export interface TabFocusHost {
	setTabFocusMode(to?: boolean | number): void;
}

/** Release tab-focus mode whenever Escape is pressed.
 *
 *  The clearing runs in a microtask on purpose: CodeMirror's own keydown
 *  handler arms the two-second window *after* DOM event handlers registered
 *  through `domEventHandlers` get their turn, so clearing synchronously would
 *  be overwritten. A microtask lands after the whole keydown dispatch, so it
 *  always wins — and it also releases the sticky permanent state.
 *
 *  Always returns false: Escape belongs to the command palette, the selection
 *  toolbar and every dialog, and must never be consumed here. */
export function clearTabFocusOnEscape(event: KeyboardEvent, view: TabFocusHost): boolean {
	if (event.key === 'Escape') queueMicrotask(() => view.setTabFocusMode(false));
	return false;
}

const escapeReleasesTab = EditorView.domEventHandlers({ keydown: clearTabFocusOnEscape });

/** Swallow the tab-focus toggle so defaultKeymap's binding is unreachable.
 *  Exported for the test that pins the key names. */
export const tabFocusToggleBinding: KeyBinding = {
	key: 'Ctrl-m',
	mac: 'Shift-Alt-m',
	run: () => true
};

const swallowTabFocusToggle = Prec.highest(keymap.of([tabFocusToggleBinding]));

/** Both halves of the fix, for the editor's extension list. */
export const tabAlwaysIndents: Extension[] = [escapeReleasesTab, swallowTabFocusToggle];
