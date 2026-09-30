// Silent autocorrect: a background scan posts a snapshot of the note, and the
// corrections that come back are applied as one ordinary, undo-able edit. Each
// corrected span flashes briefly so the change is noticeable without a panel.
//
// The scan is asynchronous, so the document may have moved on by the time the
// response lands. Every span is re-verified against the live text before it is
// applied; anything that drifted is silently dropped — the next scan sees the
// current text and re-suggests whatever still applies.
//
// A correction the user undoes is a rejection: its `before` is remembered per
// note on this device and sent with every later scan, so the server never
// re-applies it.
import {
	Annotation,
	StateEffect,
	StateField,
	type Text,
	type Transaction
} from '@codemirror/state';
import { Decoration, EditorView, ViewPlugin, type DecorationSet, type ViewUpdate } from '@codemirror/view';
import { api, type AutocorrectSuggestion } from './api';

const FLASH_MS = 2000;
const SWAP_MS = 240;
const SWAP_STAGGER_MS = 40;

export const addFlash = StateEffect.define<readonly { from: number; to: number }[]>();
export const addSwap =
	StateEffect.define<readonly { from: number; to: number; delay: number }[]>();
export const clearFlash = StateEffect.define<null>();

const flashMark = Decoration.mark({ class: 'cm-autocorrect-flash' });

export const flashField = StateField.define<DecorationSet>({
	create: () => Decoration.none,
	update(deco, tr) {
		if (tr.docChanged) deco = deco.map(tr.changes);
		for (const effect of tr.effects) {
			if (effect.is(addFlash)) {
				const marks = effect.value
					.filter((range) => range.to > range.from)
					.map((range) => flashMark.range(range.from, range.to));
				if (marks.length > 0) deco = deco.update({ add: marks, sort: true });
			} else if (effect.is(addSwap)) {
				const marks = effect.value
					.filter((range) => range.to > range.from)
					.map((range) =>
						Decoration.mark({
							class: 'cm-autocorrect-swap',
							attributes: { style: `animation-delay: ${range.delay}ms` }
						}).range(range.from, range.to)
					);
				if (marks.length > 0) deco = deco.update({ add: marks, sort: true });
			} else if (effect.is(clearFlash)) {
				deco = Decoration.none;
			}
		}
		return deco;
	},
	provide: (field) => EditorView.decorations.from(field)
});

// One timer covers every visible flash: scans are at least 5s apart and a
// flash lasts 2s, so flashes never overlap in practice, and a reset-on-add keeps the rare
// stragglers visible rather than cutting them short.
const flashTimers = ViewPlugin.fromClass(
	class {
		private timer: ReturnType<typeof setTimeout> | null = null;
		constructor(private view: EditorView) {}
		update(update: ViewUpdate) {
			const flashed = update.transactions.some((tr) =>
				tr.effects.some((effect) => effect.is(addFlash))
			);
			if (!flashed) return;
			if (this.timer) clearTimeout(this.timer);
			this.timer = setTimeout(() => {
				this.timer = null;
				this.view.dispatch({ effects: clearFlash.of(null) });
			}, FLASH_MS);
		}
		destroy() {
			if (this.timer) clearTimeout(this.timer);
		}
	}
);

// The swapped-in letters roll upright first (staggered per letter), then the
// whole corrected span blinks twice. The blink's delay covers the longest
// letter stagger, so the pulse reads as "done".
const theme = EditorView.baseTheme({
	'@keyframes cm-ac-swap-in': {
		from: { opacity: '0', transform: 'rotateX(90deg)' },
		to: { opacity: '1', transform: 'none' }
	},
	'@keyframes cm-ac-blink': {
		'0%, 100%': { backgroundColor: 'transparent' },
		'50%': { backgroundColor: 'color-mix(in srgb, var(--accent) 22%, transparent)' }
	},
	'.cm-autocorrect-swap': {
		display: 'inline-block',
		animation: `cm-ac-swap-in ${SWAP_MS}ms cubic-bezier(0.2, 0.7, 0.3, 1) backwards`
	},
	'.cm-autocorrect-flash': {
		animation: 'cm-ac-blink 420ms ease-in-out 400ms 2'
	},
	'@media (prefers-reduced-motion: reduce)': {
		'.cm-autocorrect-swap': { animation: 'none' },
		'.cm-autocorrect-flash': {
			animation: 'none',
			background: 'color-mix(in srgb, var(--accent) 18%, transparent)'
		}
	}
});

/** Tags the transaction that applied a batch of corrections. */
export const autocorrectApplied = Annotation.define<readonly AutocorrectSuggestion[]>();

export interface AppliedSpan {
	from: number;
	to: number;
	before: string;
	after: string;
}

const MAX_TRACKED = 50;

/**
 * Applied corrections that still read as applied, in live coordinates — the
 * spans an undo can reject. Read-only bookkeeping: it never alters a
 * transaction (a filter would also see server diffs and OMNIA edits).
 */
export const appliedField = StateField.define<readonly AppliedSpan[]>({
	create: () => [],
	update(spans, tr) {
		if (tr.docChanged && spans.length > 0) {
			spans = spans.flatMap((span) => {
				const from = tr.changes.mapPos(span.from, 1);
				const to = Math.max(from, tr.changes.mapPos(span.to, -1));
				return tr.newDoc.sliceString(from, to) === span.after ? [{ ...span, from, to }] : [];
			});
		}
		const applied = tr.annotation(autocorrectApplied);
		if (applied) {
			const added = applied.map((c) => {
				const from = tr.changes.mapPos(c.offset, -1);
				return { from, to: from + c.after.length, before: c.before, after: c.after };
			});
			spans = [...spans, ...added].slice(-MAX_TRACKED);
		}
		return spans;
	}
});

/**
 * The `before` of every tracked correction that `tr` undid. `tracked` is the
 * field's value before `tr`. Pure, exported for vitest.
 */
export function rejectedByUndo(tr: Transaction, tracked: readonly AppliedSpan[]): string[] {
	if (!tr.isUserEvent('undo')) return [];
	const out = new Set<string>();
	for (const span of tracked) {
		if (!tr.changes.touchesRange(span.from, span.to)) continue;
		if (tr.startState.doc.sliceString(span.from, span.to) !== span.after) continue;
		const at = tr.changes.mapPos(span.from, -1);
		if (tr.newDoc.sliceString(at, at + span.before.length) === span.before) out.add(span.before);
	}
	return [...out];
}

function rejectionListener(onReject: (before: string) => void) {
	return EditorView.updateListener.of((update) => {
		for (const tr of update.transactions) {
			const tracked = tr.startState.field(appliedField, false) ?? [];
			for (const before of rejectedByUndo(tr, tracked)) onReject(before);
		}
	});
}

export function autocorrectExtension(onReject?: (before: string) => void) {
	return [
		flashField,
		flashTimers,
		theme,
		appliedField,
		...(onReject ? [rejectionListener(onReject)] : [])
	];
}

// Per note and per device, newest first. Losing the list (private mode,
// cleared storage) only means one more undo.
const IGNORED_KEY = 'exscalia.autocorrect.ignored:';
const IGNORED_LIMIT = 50;

/** The corrections the user undid in `path`, as their `before` text. */
export function ignoredFor(path: string): string[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const raw = JSON.parse(localStorage.getItem(IGNORED_KEY + path) ?? '[]');
		return Array.isArray(raw)
			? raw.filter((s): s is string => typeof s === 'string').slice(0, IGNORED_LIMIT)
			: [];
	} catch {
		return [];
	}
}

export function addIgnored(path: string, before: string): void {
	if (!path || !before || typeof localStorage === 'undefined') return;
	const next = [before, ...ignoredFor(path).filter((s) => s !== before)].slice(0, IGNORED_LIMIT);
	try {
		localStorage.setItem(IGNORED_KEY + path, JSON.stringify(next));
	} catch {
		/* private mode — the correction may come back once more */
	}
}

/**
 * The corrections that are still safe to apply. Pure so the drift rules can be
 * unit-tested headlessly.
 *
 * - A span must always read exactly as the scan saw it — even when the
 *   document is unchanged, a span the server mis-positioned must not apply.
 * - If the document drifted from the snapshot, a span must also not touch the
 *   caret's word or the selection — mid-word rewrites under the cursor are how
 *   autocorrect fights the user.
 */
export function selectApplicable(
	doc: Text,
	snapshot: string,
	corrections: readonly AutocorrectSuggestion[],
	cursorExclusion: { from: number; to: number } | null
): AutocorrectSuggestion[] {
	const clean = doc.toString() === snapshot;
	const sorted = [...corrections].sort((a, b) => a.offset - b.offset);
	const out: AutocorrectSuggestion[] = [];
	let lastEnd = -1;
	for (const c of sorted) {
		if (c.end <= c.offset || c.offset < lastEnd) continue; // malformed or overlapping
		if (c.end > doc.length) continue;
		if (doc.sliceString(c.offset, c.end) !== c.before) continue;
		if (!clean) {
			if (cursorExclusion && c.offset <= cursorExclusion.to && c.end >= cursorExclusion.from)
				continue;
		}
		out.push(c);
		lastEnd = c.end;
	}
	return out;
}

/**
 * The characters of `after` that actually differ from `before`, as an
 * [start, end) range in `after`'s coordinates — the common prefix and suffix
 * are trimmed away so only the swapped letters animate. Empty for pure
 * deletions ("the the" -> "the"). Pure, exported for vitest.
 */
export function changedRegion(before: string, after: string): { start: number; end: number } {
	const max = Math.min(before.length, after.length);
	let pre = 0;
	while (pre < max && before[pre] === after[pre]) pre++;
	let suf = 0;
	while (suf < max - pre && before[before.length - 1 - suf] === after[after.length - 1 - suf])
		suf++;
	return { start: pre, end: after.length - suf };
}

/** Apply the surviving corrections as one edit. Returns how many landed. */
export function applyCorrections(
	view: EditorView,
	corrections: readonly AutocorrectSuggestion[],
	snapshot: string
): number {
	const sel = view.state.selection.main;
	const word = sel.empty ? view.state.wordAt(sel.head) : null;
	const exclusion = {
		from: word ? Math.min(sel.from, word.from) : sel.from,
		to: word ? Math.max(sel.to, word.to) : sel.to
	};
	const applicable = selectApplicable(view.state.doc, snapshot, corrections, exclusion);
	if (applicable.length === 0) return 0;
	const changeSet = view.state.changes(
		applicable.map((c) => ({ from: c.offset, to: c.end, insert: c.after }))
	);
	const flashes: { from: number; to: number }[] = [];
	const swaps: { from: number; to: number; delay: number }[] = [];
	for (const c of applicable) {
		if (c.after.length === 0) continue;
		const from = changeSet.mapPos(c.offset, -1);
		flashes.push({ from, to: from + c.after.length });
		const region = changedRegion(c.before, c.after);
		// Per-letter marks so each glyph rolls in on its own beat; the stagger
		// is capped so long insertions still finish inside the blink delay.
		for (let i = region.start; i < region.end; i++) {
			swaps.push({
				from: from + i,
				to: from + i + 1,
				delay: Math.min(i - region.start, 8) * SWAP_STAGGER_MS
			});
		}
	}
	view.dispatch({
		changes: changeSet,
		// Before-insertion association, like the autosave normalization path:
		// the caret must not be dragged around by a correction landing at it.
		selection: view.state.selection.map(changeSet, -1),
		annotations: autocorrectApplied.of(applicable),
		effects: [
			...(flashes.length > 0 ? [addFlash.of(flashes)] : []),
			...(swaps.length > 0 ? [addSwap.of(swaps)] : [])
		]
	});
	return applicable.length;
}

// The enabled flag is read once a minute at most; the scan response itself
// carries `enabled` too, so switching the feature off stops the very next tick
// via markAutocorrectDisabled rather than waiting out the TTL.
const ENABLED_TTL = 60_000;
let enabledCache: { value: boolean; at: number } | null = null;
let enabledFetch: Promise<boolean> | null = null;

export async function isAutocorrectEnabled(): Promise<boolean> {
	if (enabledCache && Date.now() - enabledCache.at < ENABLED_TTL) return enabledCache.value;
	if (enabledFetch) return enabledFetch;
	enabledFetch = api
		.getOmniaSettings()
		.then((settings) => {
			enabledCache = { value: settings.autocorrect, at: Date.now() };
			return settings.autocorrect;
		})
		.catch(() => {
			// Unreachable server: treat as off, and back off for one TTL.
			enabledCache = { value: false, at: Date.now() };
			return false;
		})
		.finally(() => {
			enabledFetch = null;
		});
	return enabledFetch;
}

export function markAutocorrectDisabled(): void {
	enabledCache = { value: false, at: Date.now() };
}
