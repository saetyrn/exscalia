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
	const bothPureInserts = a.fromA === a.toA && b.fromA === b.toA;
	if (bothPureInserts) {
		// Insertions at the same point don't overwrite each other, but their
		// order is arbitrary — so only identical text is safe to merge.
		return a.fromA === b.fromA && a.text !== b.text;
	}
	return !(a.toA <= b.fromA || b.toA <= a.fromA);
}

function edits(base: string, other: string): Edit[] {
	return diff(base, other).map((change) => ({
		fromA: change.fromA,
		toA: change.toA,
		text: other.slice(change.fromB, change.toB)
	}));
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
		// Both sides making the identical insertion at the same point is a
		// merge, not a duplication.
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
