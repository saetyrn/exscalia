// Hold a programmatic document replacement while an IME composition is running.
//
// The editor's `replaceDoc` is how the server's normalized text comes back into
// the note (timestamps, hidden task metadata, remote edits, OMNIA's approvals).
// Dispatching that mid-composition is destructive: the browser owns the
// composing text as an uncommitted DOM range, and a doc change underneath it
// makes CodeMirror tear the composition down — the half-typed kana, pinyin or
// Hangul syllable is dropped or duplicated. So the replacement waits.
//
// It cannot wait forever. A composition that never ends (a stuck IME, an app
// switch mid-word, a browser that forgets to fire `compositionend`) would
// freeze server normalization for as long as the note stays open, so the hold
// is capped and the last requested value goes in regardless.
//
// A held replacement is a fixed target string, computed against the document
// as it was when it was requested — but the composition keeps committing text
// while it waits. Applying the target verbatim would delete everything typed in
// the meantime, so `reconcileHeld` folds the two together first.
//
// The page moves its merge base to the replacement's revision as soon as it
// asks for it, so while the value waits the page must see the document as it
// will be once it lands (`settleHeld`): the composed text on its own, saved on
// that base, would revert the replacement on the server.
//
// This file is deliberately free of the editor view and of the DOM: the state
// machine is the part worth testing, and it is testable with two callbacks.

import { mergeThreeWay } from './merge';

/** How long a replacement may wait for the composition to finish. Long enough
 *  for a full multi-key composition, short enough that a stuck IME costs one
 *  visible hiccup instead of an unsaved note. */
export const COMPOSITION_HOLD_MS = 2000;

type TimerId = ReturnType<typeof setTimeout>;

export interface CompositionQueueOptions<T> {
	/** Whether an IME composition is in flight right now. Read fresh on every
	 *  request and release — never cached. */
	composing: () => boolean;
	/** Perform the replacement. Called with the most recent requested value, and
	 *  expected to reconcile it against the document as it is *then* (see
	 *  {@link reconcileHeld}). */
	apply: (value: T) => void;
	/** Overridable for tests; defaults to the ambient timer functions. */
	setTimer?: (fn: () => void, ms: number) => TimerId;
	clearTimer?: (id: TimerId) => void;
	/** Overridable for tests; defaults to {@link COMPOSITION_HOLD_MS}. */
	holdMs?: number;
}

/** A one-slot queue: at most one replacement is ever pending, and a newer
 *  request supersedes an older one (they describe the same document, so only
 *  the latest is worth applying). */
export class CompositionQueue<T> {
	#options: Required<CompositionQueueOptions<T>>;
	#pending: { value: T } | null = null;
	#timer: TimerId | null = null;

	constructor(options: CompositionQueueOptions<T>) {
		this.#options = {
			setTimer: (fn, ms) => setTimeout(fn, ms),
			clearTimer: (id) => clearTimeout(id),
			holdMs: COMPOSITION_HOLD_MS,
			...options
		};
	}

	/** Is a replacement waiting for the composition to end? */
	get pending(): boolean {
		return this.#pending !== null;
	}

	/** The value currently waiting, for tests and diagnostics. */
	get pendingValue(): T | undefined {
		return this.#pending?.value;
	}

	/** Ask for `value` to be applied. Applies immediately and returns true when
	 *  nothing is composing; otherwise stores it and returns false. */
	request(value: T): boolean {
		if (!this.#options.composing()) {
			this.#clearTimer();
			this.#pending = null;
			this.#options.apply(value);
			return true;
		}
		this.#pending = { value };
		// The cap runs from the FIRST held request, not the latest: a stream of
		// updates arriving during one long composition must not keep pushing the
		// deadline out.
		if (this.#timer === null)
			this.#timer = this.#options.setTimer(() => {
				this.#timer = null;
				this.#flush();
			}, this.#options.holdMs);
		return false;
	}

	/** A composition ended (or the editor reported that it is no longer
	 *  composing). Applies whatever was held, if anything.
	 *
	 *  Safe to call at any time, from any number of sources — Android's
	 *  `compositionend` ordering is unreliable enough that the editor listens
	 *  for both the DOM event and the flag going false, and both land here.
	 *  Returns true when something was applied. */
	release(): boolean {
		if (!this.#pending) return false;
		// A `compositionend` that is immediately followed by the next composition
		// (fast typing on Android fires end/start per character) must not apply
		// into the middle of the new one. The cap still guarantees progress.
		if (this.#options.composing()) return false;
		return this.#flush();
	}

	/** Drop anything held without applying it — the editor it belonged to is
	 *  going away, so the value describes a document that no longer exists. */
	cancel(): void {
		this.#clearTimer();
		this.#pending = null;
	}

	#flush(): boolean {
		const held = this.#pending;
		this.#clearTimer();
		this.#pending = null;
		if (!held) return false;
		this.#options.apply(held.value);
		return true;
	}

	#clearTimer(): void {
		if (this.#timer === null) return;
		this.#options.clearTimer(this.#timer);
		this.#timer = null;
	}
}

/** The text a held replacement should apply once the IME lets go.
 *
 *  `base` is the document when the replacement was requested, `current` the
 *  document now, `target` the replacement itself. Whatever was composed since
 *  (base -> current) and whatever the replacement changes (base -> target) are
 *  merged the way two devices' edits are. When they overlap the composed text
 *  is kept, `current` comes back unchanged and `ok` is false: the replacement
 *  is lost unless someone asks (see {@link settleHeld}). */
export function reconcileHeld(
	base: string,
	current: string,
	target: string
): { text: string; ok: boolean } {
	if (current === base) return { text: target, ok: true };
	const merged = mergeThreeWay(base, current, target);
	return merged.ok ? { text: merged.text, ok: true } : { text: current, ok: false };
}

/** A replacement waiting for a composition to end. */
export interface HeldReplacement {
	/** The document it was computed against. */
	base: string;
	/** The replacement itself. */
	doc: string;
	/** The server's text coming back — not an edit made on the user's behalf. */
	programmatic: boolean;
	/** It carries another device's or OMNIA's edit, which the page has already
	 *  taken as its base. The server's normalization of our own save does not:
	 *  the next save is normalized again, so dropping it loses nothing. */
	remote?: boolean;
	/** Set once the composition was found to overlap it. */
	conflicted?: boolean;
}

/** The document as the page should see it while `held` waits: what it will
 *  be once the replacement goes in. With nothing held that is `current`.
 *
 *  When the composition overlaps the replacement, the composed text is what
 *  the page sees. For a remote one `onConflict` runs, once: the page already
 *  treats its revision as the base, so saving the composition on top of it
 *  would silently overwrite that version. */
export function settleHeld(
	held: HeldReplacement | undefined,
	current: string,
	onConflict: () => void
): string {
	if (!held) return current;
	const { text, ok } = reconcileHeld(held.base, current, held.doc);
	if (!ok && held.remote && !held.conflicted) {
		held.conflicted = true;
		onConflict();
	}
	return text;
}
