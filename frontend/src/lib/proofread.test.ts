// Proofreading anchors: the server stores each suggestion's offsets once, when
// the review is staged, while the note keeps changing underneath. These run
// the StateField headlessly (state + transactions, no DOM, no editor view).
import { EditorState } from '@codemirror/state';
import { EditorView, type DecorationSet } from '@codemirror/view';
import { describe, expect, it } from 'vitest';
import {
	applySuggestion,
	isStaleIn,
	liveRangeIn,
	proofreadField,
	setSuggestions,
	type Suggestion
} from './proofread';

const DOC = 'The quick brown fox jumps over the lazy dog.';

function stage(doc: string, list: Suggestion[]): EditorState {
	const state = EditorState.create({ doc, extensions: [proofreadField] });
	return state.update({ effects: setSuggestions.of(list) }).state;
}

/** A suggestion anchored the way the backend does it, on the first hit. */
function suggestion(before: string, after: string, doc = DOC, id = 's1'): Suggestion {
	const offset = doc.indexOf(before);
	return raw(id, before, after, offset, offset + before.length);
}

/** A suggestion with offsets exactly as the server stored them. */
function raw(id: string, before: string, after: string, offset: number, end: number): Suggestion {
	return { id, category: 'spelling', before, after, reason: 'test', offset, end };
}

function at(state: EditorState, id: string): string | null {
	const range = liveRangeIn(state, id);
	return range ? state.doc.sliceString(range.from, range.to) : null;
}

/** Just enough of an EditorView for applySuggestion: it reads `state` and
 *  dispatches one transaction spec. */
function viewOf(initial: EditorState): EditorView {
	const stub = {
		state: initial,
		dispatch(spec: Parameters<EditorState['update']>[0]) {
			stub.state = stub.state.update(spec).state;
		}
	};
	return stub as unknown as EditorView;
}

/** Every decoration range the field hands the view. Building them is what the
 *  view does on each update, so this doubles as a no-throw check. */
function decorationRanges(state: EditorState): [number, number][] {
	const out: [number, number][] = [];
	for (const source of state.facet(EditorView.decorations)) {
		if (typeof source === 'function') continue;
		(source as DecorationSet).between(0, Number.MAX_SAFE_INTEGER, (from, to) => {
			out.push([from, to]);
		});
	}
	return out;
}

function expectInBounds(state: EditorState, id: string) {
	const range = liveRangeIn(state, id);
	if (range) {
		expect(range.from).toBeGreaterThanOrEqual(0);
		expect(range.to).toBeLessThanOrEqual(state.doc.length);
		expect(() => state.doc.lineAt(range.from)).not.toThrow();
		expect(() => state.doc.lineAt(range.to)).not.toThrow();
	}
	for (const [from, to] of decorationRanges(state)) {
		expect(from).toBeGreaterThanOrEqual(0);
		expect(to).toBeLessThanOrEqual(state.doc.length);
	}
}

describe('offsets past the end of the document', () => {
	// The repro's numbers: a review staged on a ~320-char note, reopened after
	// the note was saved at 132 chars.
	const SHORT =
		'Notes from the call.\n\n' + 'x'.repeat(74) + '\n\nFollow up on the budget by Friday.';
	const gone = raw('far', 'recieved', 'received', 303, 311);

	it('has the repro length', () => {
		expect(SHORT.length).toBe(132);
	});

	it('loads without throwing', () => {
		expect(() => stage(SHORT, [gone])).not.toThrow();
		expectInBounds(stage(SHORT, [gone]), 'far');
	});

	it('survives typing, a newline and a whole-document replace', () => {
		const s = stage(SHORT, [gone]);
		const edits = [
			{ from: 5, insert: 'a' },
			{ from: SHORT.length, insert: '\n' },
			{ from: 0, to: SHORT.length, insert: 'Rewritten elsewhere.' }
		];
		for (const changes of edits) {
			let next: EditorState | undefined;
			expect(() => (next = s.update({ changes }).state)).not.toThrow();
			expectInBounds(next!, 'far');
		}
	});

	it('re-anchors onto its text when that text is still in the note', () => {
		const tail = SHORT.replace('budget', 'budgte');
		const s = stage(tail, [{ ...gone, before: 'budgte', after: 'budget' }]);
		expect(at(s, 'far')).toBe('budgte');
		expect(isStaleIn(s, 'far')).toBe(false);
		expect(() => s.update({ changes: { from: 0, insert: 'a' } })).not.toThrow();
	});
});

describe('reopening a note that changed since the review', () => {
	const INTRO = 'An opening paragraph that the user later decides to cut entirely.\n\n';
	const LONG = INTRO + 'Middle text stays.\n\nThe ending has a mispeled word in it.';
	const SHORTER = LONG.slice(INTRO.length);
	const staged = suggestion('mispeled', 'misspelled', LONG);

	it('the raw offsets are past the end of the shorter note', () => {
		expect(staged.end).toBeGreaterThan(SHORTER.length);
	});

	it('re-anchors onto `before` and can be accepted', () => {
		const s = stage(SHORTER, [staged]);
		expect(at(s, 's1')).toBe('mispeled');
		expect(isStaleIn(s, 's1')).toBe(false);
		const view = viewOf(s);
		expect(applySuggestion(view, 's1')).toBe(true);
		expect(view.state.doc.toString()).toContain('a misspelled word');
	});

	it('keeps a later suggestion accurate after an earlier one was accepted', () => {
		// Accepting a shorter replacement shifts everything after it; a reload
		// then hands back the original offsets for the rest.
		const doc = 'We definately agree that the recieved text is fine.';
		const first = suggestion('definately', 'do', doc, 'a');
		const second = suggestion('recieved', 'received', doc, 'b');
		const view = viewOf(stage(doc, [first, second]));
		expect(applySuggestion(view, 'a')).toBe(true);
		const reopened = stage(view.state.doc.toString(), [second]);
		expect(at(reopened, 'b')).toBe('recieved');
		expect(isStaleIn(reopened, 'b')).toBe(false);
	});

	it('reloading raw offsets over live anchors keeps them on their text', () => {
		// A title rename reloads the server's offsets into an editor whose
		// anchors had been mapping correctly through the user's edits.
		let s = stage(LONG, [staged]);
		s = s.update({ changes: { from: 0, to: INTRO.length } }).state;
		expect(at(s, 's1')).toBe('mispeled');
		s = s.update({ effects: setSuggestions.of([staged]) }).state;
		expect(at(s, 's1')).toBe('mispeled');
		expect(() => s.update({ changes: { from: s.doc.length, insert: '!' } })).not.toThrow();
	});

	it('picks the occurrence nearest the old offset', () => {
		const doc = 'teh start. Some words. teh middle. More words here. teh end.';
		const last = doc.lastIndexOf('teh');
		// The note lost three characters above the last `teh`.
		const s = stage(doc, [raw('n', 'teh', 'the', last + 3, last + 6)]);
		expect(liveRangeIn(s, 'n')!.from).toBe(last);
	});

	it('anchors against the new document when loaded with a change', () => {
		const s = EditorState.create({ doc: 'abc', extensions: [proofreadField] });
		let next: EditorState | undefined;
		expect(
			() =>
				(next = s.update({
					changes: { from: 0, to: 3, insert: LONG },
					effects: setSuggestions.of([staged])
				}).state)
		).not.toThrow();
		expect(at(next!, 's1')).toBe('mispeled');
	});
});

describe('astral characters before a suggestion', () => {
	it('re-anchors offsets counted in code points', () => {
		const doc = 'Mood \u{1F600} today: teh end';
		// Python counts the emoji as one position; CodeMirror as two units.
		const codePoints = [...'Mood \u{1F600} today: '].length;
		expect(doc.indexOf('teh')).toBe(codePoints + 1);
		const s = stage(doc, [raw('e', 'teh', 'the', codePoints, codePoints + 3)]);
		expect(at(s, 'e')).toBe('teh');
		expect(isStaleIn(s, 'e')).toBe(false);
	});
});

describe('a suggestion whose text is gone', () => {
	const doc = 'Short note now.';
	const lost = raw('lost', 'irregardless', 'regardless', 40, 52);

	it('is kept, clamped into the document, and stale', () => {
		const s = stage(doc, [lost]);
		const item = s.field(proofreadField).items.find((i) => i.id === 'lost');
		expect(item).toBeDefined();
		expect(item!.from).toBeLessThanOrEqual(doc.length);
		expect(item!.to).toBeLessThanOrEqual(doc.length);
		expect(isStaleIn(s, 'lost')).toBe(true);
		expect(applySuggestion(viewOf(s), 'lost')).toBe(false);
		expectInBounds(s, 'lost');
	});

	it('is stale when its in-range offsets now cover other text', () => {
		const s = stage(doc, [{ ...lost, offset: 2, end: 8 }]);
		expect(isStaleIn(s, 'lost')).toBe(true);
		expectInBounds(s, 'lost');
	});

	it('does not break typing', () => {
		const s = stage(doc, [lost]);
		expect(() => s.update({ changes: { from: doc.length, insert: 'x' } })).not.toThrow();
		expect(() => s.update({ changes: { from: 0, insert: '\n' } })).not.toThrow();
	});
});

// Ported from scripts/proofread-check.ts, which nothing ran.
describe('mapping through edits', () => {
	const brown = suggestion('brown', 'red');
	const dog = suggestion('dog', 'cat', DOC, 's2');
	const s = stage(DOC, [brown, dog]);

	it('anchors start where the backend put them', () => {
		expect([liveRangeIn(s, 's1')!.from, liveRangeIn(s, 's2')!.from]).toEqual([
			DOC.indexOf('brown'),
			DOC.indexOf('dog')
		]);
	});

	it('insertion before shifts both anchors', () => {
		const after = s.update({ changes: { from: 0, insert: '>> ' } }).state;
		expect([at(after, 's1'), at(after, 's2')]).toEqual(['brown', 'dog']);
	});

	it('insertion between moves only the later anchor', () => {
		const after = s.update({ changes: { from: DOC.indexOf('jumps'), insert: 'quickly ' } }).state;
		expect(liveRangeIn(after, 's1')!.from).toBe(DOC.indexOf('brown'));
		expect([at(after, 's1'), at(after, 's2')]).toEqual(['brown', 'dog']);
	});

	it('insertion after leaves anchors alone', () => {
		const after = s.update({ changes: { from: DOC.length, insert: ' Really.' } }).state;
		expect([liveRangeIn(after, 's1')!.from, liveRangeIn(after, 's2')!.from]).toEqual([
			DOC.indexOf('brown'),
			DOC.indexOf('dog')
		]);
	});

	it('typing immediately before or after is not swallowed', () => {
		const before = s.update({ changes: { from: DOC.indexOf('brown'), insert: 'X' } }).state;
		expect(at(before, 's1')).toBe('brown');
		const after = s.update({ changes: { from: DOC.indexOf('brown') + 5, insert: 'X' } }).state;
		expect(at(after, 's1')).toBe('brown');
	});

	it('a fresh suggestion is not stale', () => {
		expect(isStaleIn(s, 's1')).toBe(false);
	});

	it('editing the text underneath makes it stale and leaves its neighbour alone', () => {
		const from = DOC.indexOf('brown');
		const after = s.update({ changes: { from, to: from + 5, insert: 'green' } }).state;
		expect(isStaleIn(after, 's1')).toBe(true);
		expect(isStaleIn(after, 's2')).toBe(false);
	});

	it('deleting the span makes it stale with no live range', () => {
		const from = DOC.indexOf('brown');
		const after = s.update({ changes: { from, to: from + 5, insert: '' } }).state;
		expect(isStaleIn(after, 's1')).toBe(true);
		expect(liveRangeIn(after, 's1')).toBeNull();
	});

	it('replacing the document invalidates everything', () => {
		const after = s.update({ changes: { from: 0, to: DOC.length, insert: 'gone' } }).state;
		expect([isStaleIn(after, 's1'), isStaleIn(after, 's2')]).toEqual([true, true]);
	});

	it('accepting one suggestion keeps the others correct', () => {
		const range = liveRangeIn(s, 's1')!;
		const after = s.update({ changes: { from: range.from, to: range.to, insert: 'red' } }).state;
		expect(at(after, 's2')).toBe('dog');
		expect(after.doc.toString()).toBe('The quick red fox jumps over the lazy dog.');
		expect(isStaleIn(after, 's2')).toBe(false);
	});

	it('a longer replacement still leaves the next anchor right', () => {
		const longer = stage(DOC, [suggestion('quick', 'extraordinarily quick'), dog]);
		const range = liveRangeIn(longer, 's1')!;
		const after = longer.update({
			changes: { from: range.from, to: range.to, insert: 'extraordinarily quick' }
		}).state;
		expect(at(after, 's2')).toBe('dog');
	});

	it('a deletion suggestion anchors on its span and removes exactly that text', () => {
		const wordy = 'The glasses will actually have geotagging.';
		const w = stage(wordy, [suggestion(' actually', '', wordy)]);
		expect(at(w, 's1')).toBe(' actually');
		const view = viewOf(w);
		expect(applySuggestion(view, 's1')).toBe(true);
		expect(view.state.doc.toString()).toBe('The glasses will have geotagging.');
	});
});
