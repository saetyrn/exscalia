// Markdown formatting keys for the editor. Cmd/Ctrl+B and +I did nothing in
// a markdown notebook — the muscle memory every editor since 1990 trained.
// Link insertion sits on Mod-Shift-K because plain Mod-K belongs to the
// command palette.
import {
	EditorSelection,
	Prec,
	type ChangeSet,
	type ChangeSpec,
	type EditorState,
	type Extension,
	type Line,
	type SelectionRange,
	type StateCommand
} from '@codemirror/state';
import { EditorView, type KeyBinding } from '@codemirror/view';

/** What a line marks up before its text: indentation, quote markers, list
 *  bullets and their checkboxes, heading hashes. Wrapping any of that in
 *  `~~` would turn the line back into plain text. */
const LINE_PREFIX = /^[ \t]*(?:>[ \t]?|(?:[-+*]|\d+[.)])[ \t]+(?:\[[ xX/]\][ \t]+)?|#{1,6}[ \t]+)*/;
/** Lines that have no prose to wrap: task metadata and other comments, code
 *  fences, rules, table rows. */
const NO_PROSE = /^\s*(?:<!--.*-->\s*$|```|~~~|\||([-*_])(?:\s*\1){2,}\s*$)/;

interface Segment {
	from: number;
	to: number;
}

/** The part of each line in `from`..`to` that inline markers can go around.
 *  Emphasis cannot cross a list item, a blank line or a heading, so a
 *  selection over several lines is marked up line by line. */
function proseSegments(state: EditorState, from: number, to: number): Segment[] {
	const out: Segment[] = [];
	for (let pos = from; ; ) {
		const line = state.doc.lineAt(pos);
		if (!NO_PROSE.test(line.text)) {
			const textStart = line.from + (LINE_PREFIX.exec(line.text)?.[0].length ?? 0);
			let start = Math.max(from, textStart);
			let end = Math.min(to, line.to);
			const text = state.sliceDoc(start, end);
			start += text.length - text.trimStart().length;
			end -= text.length - text.trimEnd().length;
			if (start < end) out.push({ from: start, to: end });
		}
		if (line.to >= to) break;
		pos = line.to + 1;
	}
	return out;
}

/** The open and close markers already on a segment, just inside it or just
 *  outside, or null when it is not wrapped. */
function existingMarkers(
	state: EditorState,
	{ from, to }: Segment,
	open: string,
	close: string
): ChangeSpec[] | null {
	const inner = state.sliceDoc(from, to);
	const openInside = inner.startsWith(open);
	const closeInside = inner.endsWith(close);
	if (openInside && closeInside && inner.length < open.length + close.length) return null;
	const openAt = openInside
		? from
		: state.sliceDoc(from - open.length, from) === open
			? from - open.length
			: null;
	const closeAt = closeInside
		? to - close.length
		: state.sliceDoc(to, to + close.length) === close
			? to
			: null;
	if (openAt === null || closeAt === null) return null;
	return [
		{ from: openAt, to: openAt + open.length },
		{ from: closeAt, to: closeAt + close.length }
	];
}

/** The selection after a wrap or unwrap: its start rides in past an opening
 *  marker placed on it, its end stays in front of a closing one. */
function mappedRange(range: SelectionRange, changes: ChangeSet): SelectionRange {
	const from = changes.mapPos(range.from, 1);
	const to = changes.mapPos(range.to, -1);
	return range.anchor <= range.head ? EditorSelection.range(from, to) : EditorSelection.range(to, from);
}

/** Toggle an `open`/`close` pair around each selection (one string toggles a
 * symmetric marker like `**`). An empty selection gets the pair with the
 * cursor parked in the middle. A selection over several lines is wrapped line
 * by line, around the text and not the bullets; it is unwrapped only when
 * every line already carries the pair. */
function toggleWrap(open: string, close: string = open): StateCommand {
	return ({ state, dispatch }) => {
		const changes = state.changeByRange((range) => {
			const segments = range.empty ? [] : proseSegments(state, range.from, range.to);
			if (!segments.length) {
				return {
					changes: [
						{ from: range.from, insert: open },
						{ from: range.to, insert: close }
					],
					range: EditorSelection.range(range.from + open.length, range.to + open.length)
				};
			}
			const found = segments.map((segment) => existingMarkers(state, segment, open, close));
			const spec: ChangeSpec[] = found.every((markers) => markers)
				? found.flatMap((markers) => markers ?? [])
				: segments.flatMap((segment, i) =>
						found[i]
							? []
							: [
									{ from: segment.from, insert: open },
									{ from: segment.to, insert: close }
								]
					);
			const set = state.changes(spec);
			return { changes: set, range: mappedRange(range, set) };
		});
		dispatch(state.update(changes, { scrollIntoView: true, userEvent: 'input' }));
		return true;
	};
}

// Shared with the contextual selection toolbar.
export const toggleBold = toggleWrap('**');
export const toggleItalic = toggleWrap('*');
export const toggleStrikethrough = toggleWrap('~~');
// Markdown has no underline; inline HTML is the notebook's own convention.
export const toggleUnderline = toggleWrap('<u>', '</u>');

/** Wrap the selection as [text](url) and select the url placeholder. */
const insertLink: StateCommand = ({ state, dispatch }) => {
	const changes = state.changeByRange((range) => {
		const text = state.sliceDoc(range.from, range.to) || 'link text';
		const inserted = `[${text}](url)`;
		const urlStart = range.from + text.length + 3; // past "[text]("
		return {
			changes: { from: range.from, to: range.to, insert: inserted },
			range: EditorSelection.range(urlStart, urlStart + 3)
		};
	});
	dispatch(state.update(changes, { scrollIntoView: true, userEvent: 'input' }));
	return true;
};

// ---------------------------------------------------------------------------
// Wrap the selection when a marker character is typed
// ---------------------------------------------------------------------------

/** Characters that mean "wrap this" when something is selected. Brackets are
 *  deliberately absent: `closeBrackets` already wraps a selection in `(` / `[`
 *  / `` ` ``, and it owns the caret-position behavior for those too. */
const WRAP_CHARS = new Set(['*', '_', '~', '"', "'"]);
const EMPHASIS_CHARS = new Set(['*', '_', '~']);

/** Put `char` on both sides of every non-empty range and keep the selection on
 *  the INNER text, so a second press of the same key wraps again (`*` twice on
 *  a word gives `**word**`). Empty ranges just take the character.
 *
 *  Returns false when nothing is selected, which is what lets ordinary typing
 *  fall through to CodeMirror's own input handling. */
export function wrapSelectionWith(char: string): StateCommand {
	return ({ state, dispatch }) => {
		if (state.readOnly) return false;
		if (!state.selection.ranges.some((range) => !range.empty)) return false;
		const changes = state.changeByRange((range) => {
			if (range.empty)
				return {
					changes: { from: range.from, insert: char },
					range: EditorSelection.cursor(range.from + char.length)
				};
			// Emphasis cannot cross lines, so `~` twice over a list strikes each
			// item; quotes still go around the whole passage.
			if (EMPHASIS_CHARS.has(char) && state.doc.lineAt(range.from).to < range.to) {
				const segments = proseSegments(state, range.from, range.to);
				if (segments.length) {
					const set = state.changes(
						segments.flatMap((segment) => [
							{ from: segment.from, insert: char },
							{ from: segment.to, insert: char }
						])
					);
					return { changes: set, range: mappedRange(range, set) };
				}
			}
			return {
				changes: [
					{ from: range.from, insert: char },
					{ from: range.to, insert: char }
				],
				// Both ends shift by exactly one inserted character, so the range
				// still covers the original text and nothing else.
				range: EditorSelection.range(range.anchor + char.length, range.head + char.length)
			};
		});
		dispatch(state.update(changes, { scrollIntoView: true, userEvent: 'input.type' }));
		return true;
	};
}

// Same test `closeBrackets` uses: on Android the composition flags lie until
// the composition ends, so the cheaper `composing` is the only usable guard.
const android = typeof navigator === 'object' && /Android\b/.test(navigator.userAgent);

/** Typing a marker character over a selection wraps it instead of replacing it.
 *
 *  `Prec.high` so it is offered the input before `closeBrackets` — the two
 *  never claim the same character today, but the ordering is what keeps that
 *  true if either token set grows. */
export const wrapSelectionOnType: Extension = Prec.high(
	EditorView.inputHandler.of((view, from, to, insert) => {
		if ((android ? view.composing : view.compositionStarted) || view.state.readOnly) return false;
		if (insert.length !== 1 || !WRAP_CHARS.has(insert)) return false;
		// Only handle input that replaces exactly what is selected; anything
		// else is an IME or a paste-shaped change that must take its own path.
		const main = view.state.selection.main;
		if (from !== main.from || to !== main.to) return false;
		return wrapSelectionWith(insert)(view);
	})
);

// ---------------------------------------------------------------------------
// Line commands: headings and task markers
// ---------------------------------------------------------------------------

/** An ATX heading prefix — the hashes plus the space that separates them from
 *  the text. Up to three leading spaces are still a heading in CommonMark. */
const HEADING = /^ {0,3}(#{1,6})[ \t]+/;
/** Any list item, ordered or not, task or not. */
const LIST_ITEM = /^[ \t]*(?:[-+*]|\d+[.)])[ \t]/;
/** A bullet item, splitting off its checkbox if it has one. */
const TASK_ITEM = /^([ \t]*)([-+*])([ \t]+)(\[([ xX/])\][ \t]+)?/;

/** Every line the selection touches, in document order, without duplicates. */
function selectedLines(state: EditorState): Line[] {
	const seen = new Set<number>();
	const lines: Line[] = [];
	for (const range of state.selection.ranges) {
		let pos = range.from;
		for (;;) {
			const line = state.doc.lineAt(pos);
			if (!seen.has(line.number)) {
				seen.add(line.number);
				lines.push(line);
			}
			if (line.to >= range.to) break;
			pos = line.to + 1;
		}
	}
	lines.sort((a, b) => a.from - b.from);
	return lines;
}

interface LineChange {
	from: number;
	to: number;
	insert: string;
}

function applyLineChanges(
	{ state, dispatch }: Parameters<StateCommand>[0],
	changes: LineChange[]
): boolean {
	if (!changes.length) return false;
	const changeSet = state.changes(changes);
	// A caret associates forward, so it rides out in front of a prefix inserted
	// at the line start rather than being buried behind it. A range's start
	// associates backward instead, so a selection that covered whole lines still
	// covers them — markers included — once the prefix is there.
	const selection = EditorSelection.create(
		state.selection.ranges.map((range) => {
			if (range.empty) return EditorSelection.cursor(changeSet.mapPos(range.head, 1));
			const from = changeSet.mapPos(range.from, -1);
			const to = changeSet.mapPos(range.to, 1);
			return EditorSelection.range(range.anchor <= range.head ? from : to, range.anchor <= range.head ? to : from);
		}),
		state.selection.mainIndex
	);
	dispatch(state.update({ changes: changeSet, selection, userEvent: 'input', scrollIntoView: true }));
	return true;
}

/** Make every selected line a level-`level` heading, or strip the hashes when
 *  they are all already at that level.
 *
 *  List lines are left alone: `## - milk` is not a heading, it is a mistake,
 *  and the most useful answer for a caret parked on a bullet is nothing at all. */
export function setHeading(level: number): StateCommand {
	const prefix = '#'.repeat(level) + ' ';
	return (target) => {
		const { state } = target;
		if (state.readOnly) return false;
		const lines = selectedLines(state).filter((line) => !LIST_ITEM.test(line.text));
		if (!lines.length) return false;
		// One decision for the whole selection: a mixed run of levels becomes
		// uniform first, and only a run that already says `level` toggles off.
		const alreadySet = lines.every((line) => HEADING.exec(line.text)?.[1].length === level);
		const changes = lines.map((line) => {
			const cut = HEADING.exec(line.text)?.[0].length ?? 0;
			return { from: line.from, to: line.from + cut, insert: alreadySet ? '' : prefix };
		});
		if (changes.every((change) => change.from === change.to && !change.insert)) return false;
		return applyLineChanges(target, changes);
	};
}

/** Cycle the checkbox on the selected bullet lines: plain item → unchecked →
 *  in progress → checked → plain item again, the status widget's order. Each
 *  line advances from its own state. Indentation and the `-`/`*`/`+` marker
 *  survive.
 *
 *  Returns false on anything that is not a bullet item, so the key falls
 *  through (Mod-Enter otherwise belongs to `insertBlankLine`). */
export const toggleTaskMarker: StateCommand = (target) => {
	const { state } = target;
	if (state.readOnly) return false;
	const changes: LineChange[] = [];
	for (const line of selectedLines(state)) {
		const match = TASK_ITEM.exec(line.text);
		if (!match) continue;
		const boxFrom = line.from + match[1].length + match[2].length + match[3].length;
		const contentFrom = line.from + match[0].length;
		if (!match[4]) changes.push({ from: contentFrom, to: contentFrom, insert: '[ ] ' });
		else if (match[5] === ' ') changes.push({ from: boxFrom + 1, to: boxFrom + 2, insert: '/' });
		else if (match[5] === '/') changes.push({ from: boxFrom + 1, to: boxFrom + 2, insert: 'x' });
		else changes.push({ from: boxFrom, to: contentFrom, insert: '' });
	}
	return applyLineChanges(target, changes);
};

/** The bullet marker at the head of a line, with its trailing space. */
const BULLET_PREFIX = /^([ \t]*)([-+*])[ \t]+(\[[ xX/]\][ \t]+)?/;
/** An ordered marker, which `toggleBullet` converts rather than nests under. */
const ORDERED_PREFIX = /^([ \t]*)\d+[.)][ \t]+/;

/** Make every selected line a bullet, or strip the bullets when they are all
 *  already there. An ordered item becomes a bullet; a checkbox goes with the
 *  marker it belongs to.
 *
 *  This is the toolbar's list button — a keyboard has Tab and `-`, a phone
 *  keyboard has neither within reach. */
export const toggleBullet: StateCommand = (target) => {
	const { state } = target;
	if (state.readOnly) return false;
	const lines = selectedLines(state);
	if (!lines.length) return false;
	const allBullets = lines.every((line) => BULLET_PREFIX.test(line.text));
	const changes: LineChange[] = [];
	for (const line of lines) {
		const bullet = BULLET_PREFIX.exec(line.text);
		if (allBullets) {
			if (bullet) changes.push({ from: line.from, to: line.from + bullet[0].length, insert: '' });
			continue;
		}
		// Mixed selection, adding: a line that is already a bullet keeps whatever
		// marker and checkbox it has. Rewriting it would silently drop the box.
		if (bullet) continue;
		const ordered = ORDERED_PREFIX.exec(line.text);
		const indent = ordered ? ordered[1] : (/^[ \t]*/.exec(line.text)?.[0] ?? '');
		const cut = ordered ? ordered[0].length : indent.length;
		changes.push({ from: line.from + indent.length, to: line.from + cut, insert: '- ' });
	}
	return applyLineChanges(target, changes);
};

/** Open a `[[wikilink]]`: brackets around the selection, or an empty pair with
 *  the caret inside it. The `input.type` user event is what makes the
 *  completion popup open by itself, exactly as if `[[` had been typed. */
export const insertWikilink: StateCommand = ({ state, dispatch }) => {
	if (state.readOnly) return false;
	const changes = state.changeByRange((range) => {
		const text = state.sliceDoc(range.from, range.to);
		return {
			changes: { from: range.from, to: range.to, insert: `[[${text}]]` },
			// Empty: caret between the brackets, where the completion reads its
			// fragment. Non-empty: the text stays selected, so the next keystroke
			// still replaces it and the popup filters on what is there.
			range: range.empty
				? EditorSelection.cursor(range.from + 2)
				: EditorSelection.range(range.from + 2, range.to + 2)
		};
	});
	dispatch(state.update(changes, { scrollIntoView: true, userEvent: 'input.type' }));
	return true;
};

export const markdownKeys: readonly KeyBinding[] = [
	{ key: 'Mod-b', run: toggleBold, preventDefault: true },
	{ key: 'Mod-i', run: toggleItalic, preventDefault: true },
	{ key: 'Mod-u', run: toggleUnderline, preventDefault: true },
	{ key: 'Mod-Shift-x', run: toggleStrikethrough, preventDefault: true },
	{ key: 'Mod-Shift-k', run: insertLink, preventDefault: true },
	// Mod-Enter must be listed before defaultKeymap's `insertBlankLine`, which
	// this file's consumers guarantee by spreading markdownKeys first.
	{ key: 'Mod-Enter', run: toggleTaskMarker, preventDefault: true },
	// Mod-Alt, not plain Mod: the desktop shell owns Mod-1..9 for tab switching.
	...[1, 2, 3, 4, 5, 6].map((level) => ({
		key: `Mod-Alt-${level}`,
		run: setHeading(level),
		preventDefault: true
	}))
];
