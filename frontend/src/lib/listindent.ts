// Tab inside a list nests the item; Shift-Tab lifts it back out.
//
// `indentMore`/`indentLess` only ever push a generic indent unit around, which
// is wrong for markdown: a child of `1. ` has to sit at column 3, not 2, and the
// numbers on both the level the item left and the level it joined go stale.
// lang-markdown knows all of this — for Enter — but keeps `getContext`,
// `renumberList` and `itemNumber` private, so the marker geometry is
// reimplemented here.

import { indentUnit, syntaxTree } from '@codemirror/language';
import { EditorState, type Extension, type StateCommand } from '@codemirror/state';
import type { SyntaxNode } from '@lezer/common';

/** Editor configuration this module assumes.
 *
 *  Two spaces, never four: four spaces of indentation is an indented code block
 *  in CommonMark, so a 4-wide indent unit would turn every nested bullet into a
 *  code fence. The tab *size* stays 4 so a literal tab still reads normally. */
export const listIndentConfig: Extension[] = [indentUnit.of('  '), EditorState.tabSize.of(4)];

// Marker geometry. Deliberately NOT lang-markdown's checkbox-aware bullet
// regex (`/^( *)([-+*])( {1,4}\[[ xX]\])?( +)/`): the checkbox belongs to the
// marker when Enter *continues* an item, but `- [ ] x` still has its CommonMark
// content column at 2, and that column is what a child item must line up with.
const BULLET = /^( *)([-+*])( *)/;
const ORDERED = /^( *)(\d+)([.)])( *)/;
/** lang-markdown's `itemNumber`, ported: the digits of an ordered marker. */
const ITEM_NUMBER = /^(\s*)(\d+)(?=[.)])/;

/** A change spec narrowed to the shape this module emits, so the list can be
 *  sorted into document order before it becomes a ChangeSet. */
interface Change {
	from: number;
	to?: number;
	insert?: string;
}

interface Marker {
	/** Column of the first character of the marker. */
	indent: number;
	/** Column where the item's content starts — where a child item nests. */
	content: number;
	ordered: boolean;
}

function markerOf(state: EditorState, item: SyntaxNode): Marker | null {
	const ordered = item.parent?.name === 'OrderedList';
	const line = state.doc.lineAt(item.from);
	const start = item.from - line.from;
	const match = (ordered ? ORDERED : BULLET).exec(line.text.slice(start));
	if (!match) return null;
	const indent = start + match[1].length;
	// Anything but whitespace before the marker means the list is wrapped in
	// something this command must not rewrite (a blockquote, most likely).
	if (/\S/.test(line.text.slice(0, indent))) return null;
	const markerLen = ordered ? match[2].length + 1 : 1;
	const gap = (ordered ? match[4] : match[3]).length;
	// CommonMark: an empty item, or a gap wide enough to start an indented code
	// block, both put the content exactly one space after the marker.
	const content = indent + markerLen + (gap === 0 || gap >= 4 ? 1 : gap);
	return { indent, content, ordered };
}

/** The innermost ListItem covering `pos`, or null.
 *
 *  `resolveInner(pos, -1)` is what lang-markdown uses, but at the very start of
 *  a line it resolves to the enclosing list rather than the item beginning
 *  there — and "caret at column 0, press Tab" is the most ordinary way to hit
 *  this command. So the forward side is tried as a fallback. */
function itemAt(state: EditorState, pos: number): SyntaxNode | null {
	const tree = syntaxTree(state);
	for (const side of [-1, 1] as const) {
		for (let node: SyntaxNode | null = tree.resolveInner(pos, side); node; node = node.parent) {
			// Inside a code block the text is literal; indenting is not a list op.
			if (node.name === 'FencedCode' || node.name === 'CodeBlock') return null;
			if (node.name === 'ListItem' && node.from <= pos && pos <= node.to) return node;
		}
	}
	return null;
}

function previousItem(item: SyntaxNode): SyntaxNode | null {
	for (let node = item.prevSibling; node; node = node.prevSibling)
		if (node.name === 'ListItem') return node;
	return null;
}

function nextItem(item: SyntaxNode): SyntaxNode | null {
	for (let node = item.nextSibling; node; node = node.nextSibling)
		if (node.name === 'ListItem') return node;
	return null;
}

function numberOf(state: EditorState, item: SyntaxNode): number | null {
	const match = ITEM_NUMBER.exec(state.doc.sliceString(item.from, item.from + 12));
	return match ? +match[2] : null;
}

/** Rewrite an ordered item's digits, skipping the change when they already say
 *  what they should. */
function setNumber(state: EditorState, item: SyntaxNode, value: number, changes: Change[]) {
	const match = ITEM_NUMBER.exec(state.doc.sliceString(item.from, item.from + 12));
	if (!match || +match[2] === value) return;
	changes.push({
		from: item.from + match[1].length,
		to: item.from + match[0].length,
		insert: String(value)
	});
}

/** Walk the ordered siblings from `first`, numbering them `start`, `start+1`, …
 *
 *  Mirrors lang-markdown's `renumberList` bail-out: the moment the *existing*
 *  numbers stop being consecutive the list was numbered by hand, and the rest
 *  is left exactly as the author wrote it. */
function renumberSequence(
	state: EditorState,
	first: SyntaxNode | null,
	start: number,
	changes: Change[]
) {
	let expected = start;
	let previous = -1;
	for (let node = first; node; node = nextItem(node)) {
		const number = numberOf(state, node);
		if (number === null) return;
		if (previous >= 0 && number !== previous + 1) return;
		previous = number;
		setNumber(state, node, expected++, changes);
	}
}

/** `after` is leaving this level: everything below it moves up one. */
function renumberAfterRemoval(state: EditorState, after: SyntaxNode, changes: Change[]) {
	const start = numberOf(state, after);
	if (start === null) return;
	renumberSequence(state, nextItem(after), start, changes);
}

/** An item is joining the level right after `after`: everything below it moves
 *  down one. */
function renumberAfterInsertion(state: EditorState, after: SyntaxNode, changes: Change[]) {
	const start = numberOf(state, after);
	if (start === null) return;
	renumberSequence(state, nextItem(after), start + 2, changes);
}

/** The number an item takes when it nests under `parent`: it joins whatever
 *  ordered list already hangs there, or opens a new one at 1. */
function nestedNumber(state: EditorState, parent: SyntaxNode): number {
	const last = parent.lastChild;
	if (!last || last.name !== 'OrderedList') return 1;
	let tail: SyntaxNode | null = null;
	for (let node = last.firstChild; node; node = node.nextSibling)
		if (node.name === 'ListItem') tail = node;
	if (!tail) return 1;
	const number = numberOf(state, tail);
	return number === null ? 1 : number + 1;
}

/** Where the item lands when nesting, or null when nesting is illegal.
 *  CommonMark has no way to express a nested first item, so an item with no
 *  previous sibling simply cannot be indented. */
function nestTarget(
	state: EditorState,
	item: SyntaxNode,
	marker: Marker,
	changes: Change[]
): number | null {
	const previous = previousItem(item);
	if (!previous) return null;
	const parentMarker = markerOf(state, previous);
	if (!parentMarker || parentMarker.content <= marker.indent) return null;
	if (marker.ordered) {
		renumberAfterRemoval(state, item, changes);
		setNumber(state, item, nestedNumber(state, previous), changes);
	}
	return parentMarker.content;
}

/** Where the item lands when lifted out, or null when it is already at the
 *  outermost level. */
function liftTarget(
	state: EditorState,
	item: SyntaxNode,
	marker: Marker,
	changes: Change[]
): number | null {
	const enclosing = item.parent?.parent;
	const parentItem = enclosing && enclosing.name === 'ListItem' ? enclosing : null;
	// The level being joined starts where the enclosing item's marker starts,
	// which is that item's own context's content column.
	const target = parentItem ? (markerOf(state, parentItem)?.indent ?? null) : 0;
	if (target === null || target >= marker.indent) return null;
	if (marker.ordered && parentItem) {
		// Whatever followed the item is now indented past it rather than beside
		// it, so that remainder becomes a fresh level starting at 1.
		renumberSequence(state, nextItem(item), 1, changes);
		if (parentItem.parent?.name === 'OrderedList') {
			const parentNumber = numberOf(state, parentItem);
			if (parentNumber !== null) {
				setNumber(state, item, parentNumber + 1, changes);
				renumberAfterInsertion(state, parentItem, changes);
			}
		} else {
			// Landing among bullets: the item opens its own ordered list.
			setNumber(state, item, 1, changes);
		}
	}
	return target;
}

/** Move every line of the item — continuation lines and nested children with
 *  it — from one indent column to another. */
function shiftItem(
	state: EditorState,
	item: SyntaxNode,
	from: number,
	to: number,
	changes: Change[]
) {
	const first = state.doc.lineAt(item.from);
	const last = state.doc.lineAt(item.to);
	const delta = to - from;
	// Rewrite exactly the whitespace in front of the marker, so a renumbering
	// edit on the marker itself stays a separate, adjacent range.
	changes.push({ from: first.from, to: first.from + from, insert: ' '.repeat(to) });
	for (let number = first.number + 1; number <= last.number; number++) {
		const line = state.doc.line(number);
		if (!line.text.trim()) continue; // never pad a blank line into whitespace
		if (delta > 0) {
			changes.push({ from: line.from, insert: ' '.repeat(delta) });
		} else {
			const lead = /^ */.exec(line.text)![0].length;
			const cut = Math.min(-delta, lead);
			if (cut) changes.push({ from: line.from, to: line.from + cut });
		}
	}
}

/**
 * Nest (`dir: 1`) or lift (`dir: -1`) the list item under the caret.
 *
 * Returns false — so the Tab chain falls through to plain indentation — when
 * the caret is not in a list, when the selection covers more than one item, or
 * when the move would be illegal markdown.
 */
export function indentListItem(dir: 1 | -1): StateCommand {
	return ({ state, dispatch }) => {
		if (state.readOnly) return false;
		// Multiple cursors are a whole-document operation; leave those to
		// indentMore/indentLess.
		if (state.selection.ranges.length !== 1) return false;
		const range = state.selection.main;
		const item = itemAt(state, range.from);
		if (!item || range.to > item.to) return false;
		const marker = markerOf(state, item);
		if (!marker) return false;

		// Every offset below is read against the pre-change document. That is
		// safe because indentation edits only ever touch the whitespace in front
		// of a marker, so no marker text ever moves out from under a renumbering.
		const changes: Change[] = [];
		const target =
			dir === 1
				? nestTarget(state, item, marker, changes)
				: liftTarget(state, item, marker, changes);
		if (target === null) return false;
		shiftItem(state, item, marker.indent, target, changes);

		// ChangeSet wants document order, and an insertion has to precede a
		// replacement that starts at the same offset.
		changes.sort(
			(a, b) => a.from - b.from || ((a.to ?? a.from) - a.from) - ((b.to ?? b.from) - b.from)
		);
		const changeSet = state.changes(changes);
		dispatch(
			state.update({
				changes: changeSet,
				// Associate forward so the caret rides with the marker it sits on
				// rather than being left behind by the inserted indentation.
				selection: state.selection.map(changeSet, 1),
				userEvent: dir === 1 ? 'input.indent' : 'delete.dedent',
				scrollIntoView: true
			})
		);
		return true;
	};
}
