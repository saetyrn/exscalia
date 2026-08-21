// Autocorrect applies machine edits to a document the user may have kept
// typing in — the drift rules decide which corrections are still safe, and the
// flash decorations must follow the text they highlighted.
import { EditorState, Text } from '@codemirror/state';
import type { DecorationSet } from '@codemirror/view';
import { describe, expect, it } from 'vitest';
import type { AutocorrectSuggestion } from './api';
import {
	addFlash,
	addSwap,
	changedRegion,
	clearFlash,
	flashField,
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
