// Anchoring checks for proofreading suggestions.
//
// The backend resolves offsets when a suggestion is staged, but the note keeps
// being edited afterwards. These exercise the StateField mapping headlessly:
// no DOM, no editor view, just state + transactions.
import { EditorState } from '@codemirror/state';
import {
	isStaleIn,
	liveRangeIn,
	proofreadField,
	setSuggestions,
	type Suggestion
} from '../src/lib/proofread';

let pass = 0;
let fail = 0;
const check = (name: string, got: unknown, want: unknown) => {
	const ok = JSON.stringify(got) === JSON.stringify(want);
	ok ? pass++ : fail++;
	console.log(`${ok ? 'ok  ' : 'FAIL'} ${name}`);
	if (!ok) console.log(`      got  ${JSON.stringify(got)}\n      want ${JSON.stringify(want)}`);
};

const DOC = 'The quick brown fox jumps over the lazy dog.';

function stage(doc: string, list: Suggestion[]): EditorState {
	const state = EditorState.create({ doc, extensions: [proofreadField] });
	return state.update({ effects: setSuggestions.of(list) }).state;
}

function suggestion(before: string, after: string, doc = DOC, id = 's1'): Suggestion {
	const offset = doc.indexOf(before);
	return {
		id,
		category: 'spelling',
		before,
		after,
		reason: 'test',
		offset,
		end: offset + before.length
	};
}

const brown = suggestion('brown', 'red');
const dog = suggestion('dog', 'cat', DOC, 's2');

// --- anchors survive edits elsewhere --------------------------------------

let s = stage(DOC, [brown, dog]);
check('anchors start where the backend put them', [
	liveRangeIn(s, 's1')!.from,
	liveRangeIn(s, 's2')!.from
], [DOC.indexOf('brown'), DOC.indexOf('dog')]);

// Insert before both.
let after = s.update({ changes: { from: 0, insert: '>> ' } }).state;
check('insertion before shifts both anchors', [
	after.doc.sliceString(liveRangeIn(after, 's1')!.from, liveRangeIn(after, 's1')!.to),
	after.doc.sliceString(liveRangeIn(after, 's2')!.from, liveRangeIn(after, 's2')!.to)
], ['brown', 'dog']);

// Insert between them: only the later anchor moves.
after = s.update({ changes: { from: DOC.indexOf('jumps'), insert: 'quickly ' } }).state;
check('insertion between moves only the later anchor', [
	after.doc.sliceString(liveRangeIn(after, 's1')!.from, liveRangeIn(after, 's1')!.to),
	after.doc.sliceString(liveRangeIn(after, 's2')!.from, liveRangeIn(after, 's2')!.to)
], ['brown', 'dog']);

// Insert after both: nothing moves.
after = s.update({ changes: { from: DOC.length, insert: ' Really.' } }).state;
check('insertion after leaves anchors alone', [
	liveRangeIn(after, 's1')!.from,
	liveRangeIn(after, 's2')!.from
], [DOC.indexOf('brown'), DOC.indexOf('dog')]);

// --- typing adjacent to a suggestion must not be absorbed ------------------

after = s.update({ changes: { from: DOC.indexOf('brown'), insert: 'X' } }).state;
check('typing immediately before is not swallowed',
	after.doc.sliceString(liveRangeIn(after, 's1')!.from, liveRangeIn(after, 's1')!.to), 'brown');

after = s.update({ changes: { from: DOC.indexOf('brown') + 5, insert: 'X' } }).state;
check('typing immediately after is not swallowed',
	after.doc.sliceString(liveRangeIn(after, 's1')!.from, liveRangeIn(after, 's1')!.to), 'brown');

// --- staleness -------------------------------------------------------------

check('a fresh suggestion is not stale', isStaleIn(s, 's1'), false);

after = s.update({
	changes: { from: DOC.indexOf('brown'), to: DOC.indexOf('brown') + 5, insert: 'green' }
}).state;
check('editing the text underneath makes it stale', isStaleIn(after, 's1'), true);
check('and leaves its neighbour alone', isStaleIn(after, 's2'), false);

after = s.update({
	changes: { from: DOC.indexOf('brown'), to: DOC.indexOf('brown') + 5, insert: '' }
}).state;
check('deleting the span makes it stale', isStaleIn(after, 's1'), true);
check('a deleted span reports no live range', liveRangeIn(after, 's1'), null);

// Deleting a whole region containing the suggestion.
after = s.update({ changes: { from: 0, to: DOC.length, insert: 'gone' } }).state;
check('replacing the document invalidates everything',
	[isStaleIn(after, 's1'), isStaleIn(after, 's2')], [true, true]);

// --- accepting one suggestion keeps the others correct ---------------------
// This is the case that matters most: Grammarly-style review means applying
// fixes one at a time, and every accepted fix shifts what follows it.

const range = liveRangeIn(s, 's1')!;
after = s
	.update({ changes: { from: range.from, to: range.to, insert: 'red' } })
	.state;
check('accepting the first leaves the second anchored correctly',
	after.doc.sliceString(liveRangeIn(after, 's2')!.from, liveRangeIn(after, 's2')!.to), 'dog');
check('and the document reads as expected',
	after.doc.toString(), 'The quick red fox jumps over the lazy dog.');
check('the untouched suggestion is still not stale', isStaleIn(after, 's2'), false);

// Accepting a *longer* replacement, so following anchors shift right.
const longer = stage(DOC, [suggestion('quick', 'extraordinarily quick'), dog]);
const lr = liveRangeIn(longer, 's1')!;
after = longer.update({ changes: { from: lr.from, to: lr.to, insert: 'extraordinarily quick' } }).state;
check('a longer replacement still leaves the next anchor right',
	after.doc.sliceString(liveRangeIn(after, 's2')!.from, liveRangeIn(after, 's2')!.to), 'dog');

// --- deletions (empty `after`) --------------------------------------------

const wordy = 'The glasses will actually have geotagging.';
const filler = suggestion(' actually', '', wordy);
let w = stage(wordy, [filler]);
const fr = liveRangeIn(w, 's1')!;
check('a deletion suggestion anchors on the right span',
	wordy.slice(fr.from, fr.to), ' actually');
w = w.update({ changes: { from: fr.from, to: fr.to, insert: '' } }).state;
check('applying a deletion removes exactly that text',
	w.doc.toString(), 'The glasses will have geotagging.');

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
