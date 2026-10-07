// Is the soft keyboard up, and how far up the screen does it reach?
//
// One measurement, shared. The logic below started inside MobileEditBar,
// which was the only thing that cared; the mobile tab bar has to get out of
// the keyboard's way too, and two independent visualViewport watchers would
// have been two chances to disagree about the same screen.
//
// Nothing here is as simple as it looks — browsers report the keyboard two
// incompatible ways, and the fallbacks below are the result of both being
// wrong in different situations. Change carefully.

/** Below this much lost viewport height, the change is a URL bar collapsing
 *  or an address-bar reveal, not a keyboard. */
const KEYBOARD_MIN = 120;

export const coarsePointer =
	typeof window !== 'undefined' && typeof window.matchMedia === 'function'
		? window.matchMedia('(pointer: coarse)').matches
		: false;

/** `up`: a keyboard is on screen. `inset`: how much of the layout viewport it
 *  covers, for anything that has to sit on top of it. Zero under the resize
 *  model, where the page is simply shorter and nothing is covered. */
export const softKeyboard = $state({ up: false, inset: 0 });

// The tallest layout viewport seen since the page was laid out at this
// width. Browsers that resize the layout viewport for the keyboard (Android
// with `interactive-widget=resizes-content`) leave visualViewport matching
// window.innerHeight, so the only evidence the keyboard is up is that the
// window itself got shorter than it has ever been.
let restingHeight = 0;
let restingWidth = 0;
let watching = false;

/** Re-read the viewport. Exported because focus changes move the keyboard
 *  without always firing a viewport event of their own. */
export function measureKeyboard(): void {
	if (typeof window === 'undefined') return;
	const viewport = window.visualViewport;
	if (window.innerWidth !== restingWidth) {
		// Rotation, or a desktop window resize: the old resting height
		// describes a screen that no longer exists.
		restingWidth = window.innerWidth;
		restingHeight = window.innerHeight;
	} else if (window.innerHeight > restingHeight) {
		restingHeight = window.innerHeight;
	}
	// The keyboard as an overlay: visualViewport is shorter than the layout
	// viewport, and the difference is what a bar has to clear.
	const overlap = viewport
		? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)
		: 0;
	// The keyboard as a resize: nothing overlaps, the page is simply shorter.
	const shrink = Math.max(0, restingHeight - window.innerHeight);
	softKeyboard.inset = overlap > KEYBOARD_MIN ? overlap : 0;
	softKeyboard.up = overlap > KEYBOARD_MIN || shrink > KEYBOARD_MIN;
}

/**
 * Start watching, once per page. A no-op with a mouse: there is no soft
 * keyboard to find, and the resize model would mistake a window drag for one.
 *
 * The listeners are never removed — the state is module-scoped and outlives
 * every component that reads it, so there is nothing to tear down.
 */
export function watchSoftKeyboard(): void {
	if (watching || !coarsePointer || typeof window === 'undefined') return;
	watching = true;
	restingWidth = window.innerWidth;
	restingHeight = window.innerHeight;
	measureKeyboard();
	window.visualViewport?.addEventListener('resize', measureKeyboard);
	window.visualViewport?.addEventListener('scroll', measureKeyboard);
	window.addEventListener('resize', measureKeyboard);
}
