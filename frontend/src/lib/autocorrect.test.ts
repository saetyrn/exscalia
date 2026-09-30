// Autocorrect applies machine edits to a document the user may have kept
// typing in — the drift rules decide which corrections are still safe, and the
// flash decorations must follow the text they highlighted.
import { history, redo, undo } from '@codemirror/commands';
import {
	EditorState,
	Text,
	type StateCommand,
	type Transaction,
	type TransactionSpec
} from '@codemirror/state';
import type { DecorationSet, EditorView } from '@codemirror/view';
import { afterEach, describe, expect, it } from 'vitest';
import type { AutocorrectSuggestion } from './api';
import {
	addFlash,
	addIgnored,
	addSwap,
	appliedField,
	applyCorrections,
	autocorrectApplied,
	changedRegion,
	clearFlash,
	flashField,
	ignoredFor,
	rejectedByUndo,
	selectApplicable
} from './autocorrect';

const SNAPSHOT = 'teh quick brown fox jumsp over the dog';

function fix(
	offset: number,
	end: number,
	before: string,
	after: string
): AutocorrectSuggestion {
	return { id: `${offset}-${end}`, category: 'spelling', before, after, offset, end };
}

const TEH = fix(0, 3, 'teh', 'the');
const JUMSP = fix(20, 25, 'jumsp', 'jumps');

function docOf(text: string): Text {
	return Text.of(text.split('\n'));
}

describe('selectApplicable', () => {
	it('applies everything when the document still matches the snapshot', () => {
		const picked = selectApplicable(docOf(SNAPSHOT), SNAPSHOT, [TEH, JUMSP], { from: 1, to: 2 });
		expect(picked.map((c) => c.id)).toEqual([TEH.id, JUMSP.id]);
	});

	it('keeps only spans that survived a drift', () => {
		// 'quick' became 'quack' after the snapshot was taken; both typo spans
		// still read as scanned, but the correction on the edited word must go.
		const drifted = SNAPSHOT.replace('quick', 'quack');
		const quick = fix(4, 9, 'quick', 'swift');
		const picked = selectApplicable(docOf(drifted), SNAPSHOT, [TEH, quick, JUMSP], null);
		expect(picked.map((c) => c.id)).toEqual([TEH.id, JUMSP.id]);
	});

	it('drops spans that run past the end of a shortened document', () => {
		const truncated = SNAPSHOT.slice(0, 10);
		expect(selectApplicable(docOf(truncated), SNAPSHOT, [JUMSP], null)).toEqual([]);
	});

	it('never touches the caret word or selection on a drifted document', () => {
		const drifted = `${SNAPSHOT}!`;
		const picked = selectApplicable(docOf(drifted), SNAPSHOT, [TEH, JUMSP], { from: 1, to: 2 });
		expect(picked.map((c) => c.id)).toEqual([JUMSP.id]);
	});

	it('treats a span that merely borders the exclusion as touching it', () => {
		const drifted = `${SNAPSHOT}!`;
		expect(selectApplicable(docOf(drifted), SNAPSHOT, [TEH], { from: 3, to: 8 })).toEqual([]);
	});

	it('drops a span that does not read as scanned, even on an unchanged document', () => {
		// The emoji is two UTF-16 units: offsets counted in code points land one
		// short and would splice 'receive' over ' reciev'.
		const text = '\u{1F3AF} I recieve mail';
		const codePoints = fix(4, 11, 'recieve', 'receive');
		expect(selectApplicable(docOf(text), text, [codePoints], null)).toEqual([]);
		const units = fix(5, 12, 'recieve', 'receive');
		expect(selectApplicable(docOf(text), text, [units], null)).toEqual([units]);
	});

	it('drops malformed and overlapping spans', () => {
		const empty = fix(5, 5, '', 'x');
		const overlap = fix(2, 6, 'h qu', 'zzzz');
		const picked = selectApplicable(docOf(SNAPSHOT), SNAPSHOT, [overlap, TEH, empty], null);
		expect(picked.map((c) => c.id)).toEqual([TEH.id]);
	});
});

function ranges(deco: DecorationSet): { from: number; to: number }[] {
	const out: { from: number; to: number }[] = [];
	const iter = deco.iter();
	while (iter.value) {
		out.push({ from: iter.from, to: iter.to });
		iter.next();
	}
	return out;
}

describe('flashField', () => {
	it('maps flash ranges through later edits', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions: [flashField] });
		state = state.update({ effects: addFlash.of([{ from: 0, to: 3 }, { from: 20, to: 25 }]) })
			.state;
		expect(ranges(state.field(flashField))).toEqual([
			{ from: 0, to: 3 },
			{ from: 20, to: 25 }
		]);
		// Typing before the highlights must carry them along with their text.
		state = state.update({ changes: { from: 0, to: 0, insert: 'Ah, ' } }).state;
		expect(ranges(state.field(flashField))).toEqual([
			{ from: 4, to: 7 },
			{ from: 24, to: 29 }
		]);
	});

	it('drops a flash whose text was deleted', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions: [flashField] });
		state = state.update({ effects: addFlash.of([{ from: 20, to: 25 }]) }).state;
		state = state.update({ changes: { from: 19, to: 26, insert: ' ' } }).state;
		expect(ranges(state.field(flashField))).toEqual([]);
	});

	it('clears everything on clearFlash', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions: [flashField] });
		state = state.update({ effects: addFlash.of([{ from: 0, to: 3 }]) }).state;
		state = state.update({ effects: clearFlash.of(null) }).state;
		expect(ranges(state.field(flashField))).toEqual([]);
	});

	it('carries per-letter swap marks and clears them with the flash', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions: [flashField] });
		state = state.update({
			effects: addSwap.of([
				{ from: 1, to: 2, delay: 0 },
				{ from: 2, to: 3, delay: 40 }
			])
		}).state;
		expect(ranges(state.field(flashField))).toEqual([
			{ from: 1, to: 2 },
			{ from: 2, to: 3 }
		]);
		state = state.update({ effects: clearFlash.of(null) }).state;
		expect(ranges(state.field(flashField))).toEqual([]);
	});
});

describe('changedRegion', () => {
	it('trims the common prefix and suffix down to the swapped letters', () => {
		// recieve -> receive: only "ei" differs.
		expect(changedRegion('recieve', 'receive')).toEqual({ start: 3, end: 5 });
	});

	it('is empty for a pure deletion', () => {
		expect(changedRegion('the the', 'the')).toEqual({ start: 3, end: 3 });
	});

	it('covers appended letters', () => {
		expect(changedRegion('a', 'an')).toEqual({ start: 1, end: 2 });
	});

	it('covers an inserted apostrophe', () => {
		expect(changedRegion('Its', "It's")).toEqual({ start: 2, end: 3 });
	});

	it('covers a full-word swap', () => {
		expect(changedRegion('their', 'there')).toEqual({ start: 3, end: 5 });
	});
});

/** Apply corrections the way applyCorrections does: one annotated edit. */
function applyFixes(state: EditorState, fixes: AutocorrectSuggestion[]): EditorState {
	return state.update({
		changes: fixes.map((c) => ({ from: c.offset, to: c.end, insert: c.after })),
		annotations: autocorrectApplied.of(fixes)
	}).state;
}

/** Run a history command headlessly, as lib/testing/cm.ts runs a StateCommand. */
function run(state: EditorState, command: StateCommand): Transaction {
	let dispatched: Transaction | null = null;
	command({ state, dispatch: (tr) => (dispatched = tr) });
	if (!dispatched) throw new Error('command dispatched nothing');
	return dispatched;
}

function rejected(tr: Transaction): string[] {
	return rejectedByUndo(tr, tr.startState.field(appliedField));
}

describe('appliedField', () => {
	it('records applied spans and maps them through later edits', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions: [appliedField] });
		state = applyFixes(state, [TEH, JUMSP]);
		expect(state.field(appliedField)).toEqual([
			{ from: 0, to: 3, before: 'teh', after: 'the' },
			{ from: 20, to: 25, before: 'jumsp', after: 'jumps' }
		]);
		state = state.update({ changes: { from: 0, to: 0, insert: 'Ah, ' } }).state;
		expect(state.field(appliedField).map((s) => [s.from, s.to])).toEqual([
			[4, 7],
			[24, 29]
		]);
	});

	it('is fed by applyCorrections', () => {
		// applyCorrections only touches view.state and view.dispatch.
		const view = {
			state: EditorState.create({ doc: SNAPSHOT, extensions: [appliedField] }),
			dispatch(spec: TransactionSpec) {
				this.state = this.state.update(spec).state;
			}
		};
		expect(applyCorrections(view as unknown as EditorView, [TEH], SNAPSHOT)).toBe(1);
		expect(view.state.field(appliedField)).toEqual([
			{ from: 0, to: 3, before: 'teh', after: 'the' }
		]);
	});

	it('forgets a span once its text changes', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions: [appliedField] });
		state = applyFixes(state, [TEH]);
		state = state.update({ changes: { from: 1, to: 2, insert: 'x' } }).state;
		expect(state.field(appliedField)).toEqual([]);
	});
});

describe('rejectedByUndo', () => {
	const extensions = [history(), appliedField];

	it('reports a correction the user undid', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions });
		state = applyFixes(state, [TEH]);
		const undone = run(state, undo);
		expect(undone.state.doc.toString()).toBe(SNAPSHOT);
		expect(rejected(undone)).toEqual(['teh']);
		// The span reads 'teh' again, so the field lets go of it.
		expect(undone.state.field(appliedField)).toEqual([]);
		// Redoing is accepting the fix after all, not another rejection.
		const redone = run(undone.state, redo);
		expect(redone.state.doc.toString()).toBe(state.doc.toString());
		expect(rejected(redone)).toEqual([]);
	});

	it('ignores typing over a span and undoing an unrelated edit', () => {
		let state = EditorState.create({ doc: SNAPSHOT, extensions });
		state = applyFixes(state, [TEH]);
		const typed = state.update({
			changes: { from: 0, to: 3, insert: 'teh' },
			userEvent: 'input.type'
		});
		expect(rejected(typed)).toEqual([]);
		// A later edit far from the span is its own history event: undoing it
		// leaves the correction standing, and only the next undo rejects it.
		state = state.update({
			changes: { from: state.doc.length, to: state.doc.length, insert: ' ok' },
			userEvent: 'input.type'
		}).state;
		const first = run(state, undo);
		expect(rejected(first)).toEqual([]);
		expect(rejected(run(first.state, undo))).toEqual(['teh']);
	});

	it('pins a pure deletion to the undo that restores it', () => {
		// After deleting the doubled 'the ', the doc already reads 'the ' at the
		// span, so only an undo that actually edits there may count.
		const doc = 'the the cat';
		let state = EditorState.create({ doc, extensions });
		state = applyFixes(state, [fix(0, 4, 'the ', '')]);
		expect(state.field(appliedField)).toEqual([{ from: 0, to: 0, before: 'the ', after: '' }]);
		state = state.update({
			changes: { from: state.doc.length, to: state.doc.length, insert: ' ok' },
			userEvent: 'input.type'
		}).state;
		const first = run(state, undo);
		expect(rejected(first)).toEqual([]);
		const second = run(first.state, undo);
		expect(second.state.doc.toString()).toBe(doc);
		expect(rejected(second)).toEqual(['the ']);
	});
});

describe('ignore store', () => {
	const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');

	function stubStorage(): Map<string, string> {
		const items = new Map<string, string>();
		Object.defineProperty(globalThis, 'localStorage', {
			configurable: true,
			value: {
				getItem: (key: string) => items.get(key) ?? null,
				setItem: (key: string, value: string) => void items.set(key, String(value)),
				removeItem: (key: string) => void items.delete(key)
			}
		});
		return items;
	}

	afterEach(() => {
		if (original) Object.defineProperty(globalThis, 'localStorage', original);
		else delete (globalThis as { localStorage?: Storage }).localStorage;
	});

	it('round-trips per note, newest first, deduped and capped', () => {
		stubStorage();
		addIgnored('a.md', 'teh');
		addIgnored('a.md', 'recieve');
		addIgnored('a.md', 'teh');
		addIgnored('b.md', 'Smyth');
		expect(ignoredFor('a.md')).toEqual(['teh', 'recieve']);
		expect(ignoredFor('b.md')).toEqual(['Smyth']);
		for (let i = 0; i < 60; i++) addIgnored('a.md', `w${i}`);
		const capped = ignoredFor('a.md');
		expect(capped).toHaveLength(50);
		expect(capped[0]).toBe('w59');
		expect(capped).not.toContain('teh');
	});

	it('survives corrupt entries and a throwing store', () => {
		const items = stubStorage();
		items.set('exscalia.autocorrect.ignored:a.md', '{not json');
		expect(ignoredFor('a.md')).toEqual([]);
		Object.defineProperty(globalThis, 'localStorage', {
			configurable: true,
			value: {
				getItem: () => {
					throw new Error('denied');
				},
				setItem: () => {
					throw new Error('denied');
				}
			}
		});
		expect(() => addIgnored('a.md', 'teh')).not.toThrow();
		expect(ignoredFor('a.md')).toEqual([]);
	});

	it('is empty without localStorage', () => {
		delete (globalThis as { localStorage?: Storage }).localStorage;
		expect(typeof localStorage).toBe('undefined');
		expect(ignoredFor('a.md')).toEqual([]);
		expect(() => addIgnored('a.md', 'teh')).not.toThrow();
	});
});
