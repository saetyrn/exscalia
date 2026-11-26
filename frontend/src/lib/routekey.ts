// The layout remounts the page on every pathname change ({#key routeKey}).
// A rename moves the URL under a note that is already correctly on screen —
// remounting would blank the editor for a round-trip and drop cursor, scroll
// and undo history. Holding the key for exactly one navigation lets the page
// component absorb the path change in place.
let hold = false;

/** Call immediately before a goto() whose page must survive the navigation. */
export function holdRouteKey(): void {
	hold = true;
}

/** One-shot: true if the current pathname change should not remount. */
export function consumeRouteKeyHold(): boolean {
	const held = hold;
	hold = false;
	return held;
}
