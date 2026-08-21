// Proofreading suggestions, anchored in the document.
//
// The backend resolves each suggestion to an offset when it is staged, but the
// note keeps being edited afterwards — including by accepting other
// suggestions. A StateField is the right home for these: CodeMirror hands it
// every change, so anchors are mapped forward for free instead of being
// recomputed from stale offsets and drifting onto the wrong words.
import { StateEffect, StateField, type EditorState, type Extension } from '@codemirror/state';
import { Decoration, EditorView, type DecorationSet } from '@codemirror/view';

export type SuggestionCategory = 'spelling' | 'grammar' | 'clarity';

export interface Suggestion {
	id: string;
	category: SuggestionCategory;
	before: string;
	after: string;
	reason: string;
	offset: number;
	end: number;
}

interface Anchored {
	id: string;
	category: SuggestionCategory;
	before: string;
	after: string;
	from: number;
	to: number;
}

interface ProofreadState {
	items: Anchored[];
	activeId: string | null;
}

export const setSuggestions = StateEffect.define<Suggestion[]>();
export const dropSuggestion = StateEffect.define<string>();
export const setActiveSuggestion = StateEffect.define<string | null>();

const EMPTY: ProofreadState = { items: [], activeId: null };

function anchor(list: Suggestion[]): Anchored[] {
	return list
		.map((s) => ({
			id: s.id,
			category: s.category,
			before: s.before,
			after: s.after,
			from: s.offset,
			to: s.end
		}))
		.sort((a, b) => a.from - b.from);
}

export const proofreadField = StateField.define<ProofreadState>({
	create: () => EMPTY,
	update(value, tr) {
		let next = value;
		for (const effect of tr.effects) {
			if (effect.is(setSuggestions)) next = { items: anchor(effect.value), activeId: null };
			else if (effect.is(dropSuggestion))
				next = {
					items: next.items.filter((item) => item.id !== effect.value),
					activeId: next.activeId === effect.value ? null : next.activeId
				};
			else if (effect.is(setActiveSuggestion)) next = { ...next, activeId: effect.value };
		}
		if (!tr.docChanged || next.items.length === 0) return next;
		// `from` associates right and `to` left, so text typed immediately
		// beside a suggestion is not swallowed into it.
		return {
			...next,
			items: next.items.map((item) => ({
				...item,
				from: tr.changes.mapPos(item.from, 1),
				to: tr.changes.mapPos(item.to, -1)
			}))
		};
	},
	provide: (field) =>
		EditorView.decorations.from(field, (value) => decorationsFor(value))
});

function decorationsFor(value: ProofreadState): DecorationSet {
	const marks = [];
	for (const item of value.items) {
		if (item.to <= item.from) continue; // deleted out from under us
		marks.push(
			Decoration.mark({
				class:
					`cm-suggest cm-suggest-${item.category}` +
					(item.id === value.activeId ? ' cm-suggest-active' : ''),
				attributes: { 'data-suggestion': item.id }
			}).range(item.from, item.to)
		);
	}
	return Decoration.set(marks, true);
}

/** Live position of a suggestion, or null once it has been edited away.
 * Takes an EditorState rather than a view so the anchoring can be exercised
 * headlessly — see scripts/proofread-check.ts. */
export function liveRangeIn(state: EditorState, id: string): Anchored | null {
	const item = state.field(proofreadField).items.find((s) => s.id === id);
	if (!item || item.to <= item.from) return null;
	return item;
}

export function isStaleIn(state: EditorState, id: string): boolean {
	const item = liveRangeIn(state, id);
	if (!item) return true;
	return state.doc.sliceString(item.from, item.to) !== item.before;
}

function liveRange(view: EditorView, id: string): Anchored | null {
	return liveRangeIn(view.state, id);
}

/**
 * True when the text under a suggestion no longer matches what it was written
 * against. Applying it then would rewrite something the user did not agree to,
 * so the panel greys it out rather than guessing.
 */
export function isStale(view: EditorView | null, id: string): boolean {
	return view ? isStaleIn(view.state, id) : false;
}

export function staleIds(view: EditorView | null): Set<string> {
	if (!view) return new Set();
	const out = new Set<string>();
	for (const item of view.state.field(proofreadField).items) {
		if (isStale(view, item.id)) out.add(item.id);
	}
	return out;
}

/** Apply one suggestion as an ordinary edit. Returns false if it went stale. */
export function applySuggestion(view: EditorView | null, id: string): boolean {
	if (!view) return false;
	const item = liveRange(view, id);
	if (!item || view.state.doc.sliceString(item.from, item.to) !== item.before) return false;
	view.dispatch({
		changes: { from: item.from, to: item.to, insert: item.after },
		effects: dropSuggestion.of(id),
		// Put the caret where the change landed, so undo and continued typing
		// both behave the way they would if the user had made the edit.
		selection: { anchor: item.from + item.after.length },
		scrollIntoView: true
	});
	return true;
}

export function dismissSuggestion(view: EditorView | null, id: string): void {
	view?.dispatch({ effects: dropSuggestion.of(id) });
}

export function revealSuggestion(view: EditorView | null, id: string): void {
	if (!view) return;
	const item = liveRange(view, id);
	if (!item) return;
	view.dispatch({
		effects: [setActiveSuggestion.of(id), EditorView.scrollIntoView(item.from, { y: 'center' })]
	});
}

export function loadSuggestions(view: EditorView | null, list: Suggestion[]): void {
	view?.dispatch({ effects: setSuggestions.of(list) });
}

/** Clicking an underlined span selects its card in the panel. */
function clickToSelect(onSelect: (id: string) => void) {
	return EditorView.domEventHandlers({
		mousedown(event, view) {
			const target = event.target as HTMLElement | null;
			const marked = target?.closest?.('[data-suggestion]') as HTMLElement | null;
			const id = marked?.dataset?.suggestion;
			if (!id) return false;
			view.dispatch({ effects: setActiveSuggestion.of(id) });
			onSelect(id);
			return false; // let the click also place the caret
		}
	});
}

const theme = EditorView.baseTheme({
	'.cm-suggest': {
		// A dotted underline rather than a squiggle: it reads as "look here"
		// without turning a page of prose into a warning screen.
		textDecoration: 'underline',
		textDecorationStyle: 'dotted',
		textDecorationThickness: '2px',
		textUnderlineOffset: '3px',
		cursor: 'pointer'
	},
	'.cm-suggest-spelling, .cm-suggest-grammar': {
		textDecorationColor: 'var(--danger)'
	},
	'.cm-suggest-clarity': {
		textDecorationColor: 'var(--accent)'
	},
	'.cm-suggest-active': {
		background: 'var(--accent-soft)',
		borderRadius: '3px'
	}
});

export function proofreadExtension(onSelect: (id: string) => void): Extension {
	return [proofreadField, clickToSelect(onSelect), theme];
}
