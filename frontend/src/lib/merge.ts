// Three-way merge, for a note edited on two devices at once.
//
// Both sides descend from a common ancestor: the content this device last had
// in sync with the server. Diffing base→mine and base→theirs gives two sets of
// edits in the *same* coordinate space, so they can be replayed onto the base
// together whenever they don't touch overlapping ground.
//
// The diff comes from @codemirror/merge, which the editor already bundles —
// no extra dependency, and it works at character level rather than by line, so
// two people editing different words of the same sentence still merges.
import { diff } from '@codemirror/merge';

export type MergeResult =
	| { ok: true; text: string }
	| { ok: false; conflicts: number };

interface Edit {
	fromA: number;
	toA: number;
	text: string;
}

/** Do two edits, expressed in base coordinates, contend for the same ground? */
function collides(a: Edit, b: Edit): boolean {
	// The same edit made on both sides (a save that landed, replayed from a
	// draft or a retry) agrees with itself; the replay below keeps one copy.
	if (a.fromA === b.fromA && a.toA === b.toA && a.text === b.text) return false;
	const bothPureInserts = a.fromA === a.toA && b.fromA === b.toA;
	if (bothPureInserts) {
		// Insertions at the same point don't overwrite each other, but their
		// order is arbitrary — so only identical text is safe to merge.
		return a.fromA === b.fromA && a.text !== b.text;
	}
	// Edits that only meet at a point are independent: deleting a list item
	// beside an added one, or inserting a word before one the other side
	// capitalised. Rewrites of the same word overlap once edits() has joined
	// their pieces.
	return !(a.toA <= b.fromA || b.toA <= a.fromA);
}

// Base text between two pieces of one side's diff that still counts as the
// same word: no whitespace, and short.
const SAME_WORD = /^\S{0,15}$/;

/** One side's edits, the pieces of a rewritten word joined into one edit.
 * The diff anchors on shared letters, so "line" → "rewritten" comes back as
 * l→rewr, n→tt, +n. Against the other side's "rewritten twice", whose pieces
 * anchor on the same letters, those only touch, and replaying both spliced
 * the two words together. Joined, the two rewrites overlap and conflict. */
function edits(base: string, other: string): Edit[] {
	const out: Edit[] = [];
	for (const change of diff(base, other)) {
		const text = other.slice(change.fromB, change.toB);
		const last = out[out.length - 1];
		const gap = last ? base.slice(last.toA, change.fromA) : '';
		if (last && SAME_WORD.test(gap)) {
			last.text += gap + text;
			last.toA = change.toA;
		} else {
			out.push({ fromA: change.fromA, toA: change.toA, text });
		}
	}
	return out;
}

/**
 * Combine two independent sets of changes to `base`.
 *
 * Returns the merged text when the two sides touched different regions, or a
 * conflict count when they didn't — in which case the caller must ask, because
 * picking a winner silently would lose someone's writing.
 */
export function mergeThreeWay(base: string, mine: string, theirs: string): MergeResult {
	if (mine === theirs) return { ok: true, text: mine };
	if (base === mine) return { ok: true, text: theirs };
	if (base === theirs) return { ok: true, text: mine };

	const ours = edits(base, mine);
	const yours = edits(base, theirs);

	let conflicts = 0;
	for (const a of ours) {
		for (const b of yours) if (collides(a, b)) conflicts += 1;
	}
	if (conflicts > 0) return { ok: false, conflicts };

	const all = [...ours, ...yours].sort((a, b) => a.fromA - b.fromA || a.toA - b.toA);
	let out = '';
	let pos = 0;
	let previous: Edit | null = null;
	for (const edit of all) {
		// Both sides making the identical edit at the same point is a merge,
		// not a duplication.
		if (
			previous &&
			previous.fromA === edit.fromA &&
			previous.toA === edit.toA &&
			previous.text === edit.text
		) {
			continue;
		}
		out += base.slice(pos, edit.fromA) + edit.text;
		pos = edit.toA;
		previous = edit;
	}
	return { ok: true, text: out + base.slice(pos) };
}
