import { syntaxTree } from '@codemirror/language';
import { findMath, renderTex } from './math';
import {
	EditorState,
	RangeSetBuilder,
	StateField,
	type ChangeDesc,
	type ChangeSpec,
	type EditorSelection,
	type Range,
	type Text
} from '@codemirror/state';
import {
	Decoration,
	EditorView,
	WidgetType,
	ViewPlugin,
	ViewUpdate,
	type DecorationSet
} from '@codemirror/view';

/** A stretch of the document, as `view.visibleRanges` reports them. The
 *  decoration builders take these rather than an EditorView so they can run
 *  headlessly against a bare EditorState. */
export interface Span {
	from: number;
	to: number;
}

/** Every line number touched by the selection — those keep their raw markers. */
function revealedLines(state: EditorState): Set<number> {
	const revealed = new Set<number>();
	const doc = state.doc;
	for (const range of state.selection.ranges) {
		const last = doc.lineAt(range.to).number;
		for (let line = doc.lineAt(range.from).number; line <= last; line += 1) revealed.add(line);
	}
	return revealed;
}

/**
 * Format mode (live preview): markdown syntax markers stay in the document
 * but are hidden — headings render without `##`, bold without `**`, etc.
 * Any line the cursor/selection touches shows its raw markers so editing
 * works normally (the Obsidian live-preview behavior).
 */

// Syntax-tree node names for pure formatting markers, safe to hide.
const HIDE_NODES = new Set([
	'HeaderMark', // ## before headings
	'EmphasisMark', // * / ** / _ around em/strong
	'CodeMark', // ` around inline code
	'StrikethroughMark', // ~~
	'QuoteMark' // > before blockquotes
]);

/** The `-` of a list item rendered as a real bullet, its shape set by
 * nesting depth: filled dot at the top level, square one level in,
 * alternating deeper. Filled shapes only — the hollow ring this cycle once
 * carried rendered as nothing on the user's setup no matter how thick its
 * stroke, so it is gone for good. */
const BULLET_SHAPES = ['dot', 'square'];

class BulletWidget extends WidgetType {
	constructor(readonly shape: string) {
		super();
	}
	toDOM() {
		const span = document.createElement('span');
		span.className = `cm-list-bullet cm-list-bullet--${this.shape}`;
		return span;
	}
	eq(other: BulletWidget) {
		return other.shape === this.shape;
	}
	ignoreEvent() {
		return false;
	}
}

const bulletByDepth = BULLET_SHAPES.map((shape) =>
	Decoration.replace({ widget: new BulletWidget(shape) })
);

// Ordered marks keep their number — it carries meaning — but pick up the
// same muted formatted-list styling as bullets.
const numberMark = Decoration.mark({ class: 'cm-list-number' });

// Extra visual indent per nesting level past the first, on top of the two
// space characters already in the document. Two UI-font spaces are ~0.56em,
// so 0.14em/level ≈ a quarter more depth (the requested 1.25x).
const INDENT_EXTRA_EM = 0.14;
const indentLineCache = new Map<number, Decoration>();
function listIndentLine(depth: number): Decoration {
	let deco = indentLineCache.get(depth);
	if (!deco) {
		// margin, not padding: .cm-line carries CodeMirror's own base padding,
		// and an inline padding-left would replace it instead of adding.
		deco = Decoration.line({
			attributes: { style: `margin-left: ${((depth - 1) * INDENT_EXTRA_EM).toFixed(2)}em` }
		});
		indentLineCache.set(depth, deco);
	}
	return deco;
}

/** The formatted-view decorations, plus the subset of them the caret must
 *  treat as single units. Only the replacements are atomic: the ordered-list
 *  number keeps a plain mark, and making that atomic would trap the caret
 *  beside the digits. */
export interface FormatDecorations {
	decorations: DecorationSet;
	atomic: DecorationSet;
}

/** A decoration and the range it covers, collected before the set is built.
 *  `Decoration.set(items, true)` sorts them — side ordering included, which a
 *  hand-fed RangeSetBuilder gets wrong — and spills anything that overlaps into
 *  a second layer, which is precisely what the view did back when the markers
 *  and the inline media lived in two separate plugins. */
type Decorated = Range<Decoration>;

/** The markdown markers: hidden syntax, bullets, ordered-list numbers. */
function collectFormat(
	state: EditorState,
	ranges: readonly Span[],
	revealed: Set<number>,
	into: Decorated[],
	atomic: Decorated[]
): void {
	const doc = state.doc;

	const hide = (from: number, to: number, deco: Decoration) => {
		into.push(deco.range(from, to));
		atomic.push(deco.range(from, to));
	};

	for (const { from, to } of ranges) {
		syntaxTree(state).iterate({
			from,
			to,
			enter(node) {
				if (node.name === 'ListMark') {
					const mark = state.sliceDoc(node.from, node.to);
					// Nesting depth = enclosing list count: 1 at the top level.
					let depth = 0;
					for (let p = node.node.parent; p; p = p.parent) {
						if (p.name === 'BulletList' || p.name === 'OrderedList') depth++;
					}
					// The document's own nesting indent (two spaces a level) reads
					// shallow, so nested lines get visual padding on top — ~25%
					// over the character indent per level. Padding, not wider
					// characters: wrapped rows shift with the line box, and the
					// text stays byte-identical.
					if (depth > 1) {
						const line = doc.lineAt(node.from);
						into.push(listIndentLine(depth).range(line.from));
					}
					// Ordered marks ("1.") keep their number, restyled — pure
					// styling, so it applies even on the active line.
					if (/^\d+[.)]$/.test(mark)) {
						into.push(numberMark.range(node.from, node.to));
						return;
					}
					// Unordered marks are ALWAYS bullets — no raw-dash reveal,
					// even under the cursor. The marker char stays in the
					// document (Raw mode shows it; backspace deletes it), but
					// in the formatted view a list reads as bullets, full stop.
					// Dashes inside sentence text are not ListMarks and never
					// convert.
					if (mark !== '-' && mark !== '*' && mark !== '+') return;
					if (/^\s*\[[ xX/]\]/.test(state.sliceDoc(node.to, node.to + 4))) return;
					hide(
						node.from,
						node.to,
						bulletByDepth[Math.max(0, depth - 1) % BULLET_SHAPES.length]
					);
					return;
				}
				if (!HIDE_NODES.has(node.name)) return;
				if (revealed.has(doc.lineAt(node.from).number)) return;
				let hideTo = node.to;
				// Swallow the single space after `##` / `>` so headings and
				// quotes align flush left.
				if (
					(node.name === 'HeaderMark' || node.name === 'QuoteMark') &&
					state.sliceDoc(hideTo, hideTo + 1) === ' '
				) {
					hideTo += 1;
				}
				hide(node.from, hideTo, Decoration.replace({}));
			}
		});
	}
}

/** Just the markers, for tests and for reading in isolation. */
export function buildFormatDecorations(
	state: EditorState,
	ranges: readonly Span[],
	revealed: Set<number> = revealedLines(state)
): FormatDecorations {
	const decorations: Decorated[] = [];
	const atomic: Decorated[] = [];
	collectFormat(state, ranges, revealed, decorations, atomic);
	return { decorations: Decoration.set(decorations, true), atomic: Decoration.set(atomic, true) };
}

/** Everything the formatted view draws inline — markers, bullets, images and
 *  math — as ONE decoration set.
 *
 *  They were two plugins, and each one walked `visibleRanges` and built its own
 *  revealed-line set for every keystroke. They are never enabled apart (the
 *  note page swaps them in together), so the second walk bought nothing. */
export function buildLiveDecorations(
	state: EditorState,
	ranges: readonly Span[],
	revealed: Set<number> = revealedLines(state)
): FormatDecorations {
	const decorations: Decorated[] = [];
	const atomic: Decorated[] = [];
	collectFormat(state, ranges, revealed, decorations, atomic);
	collectRich(state, ranges, revealed, decorations);
	return { decorations: Decoration.set(decorations, true), atomic: Decoration.set(atomic, true) };
}

/** Whether two revealed-line sets say the same thing. Moving the caret along a
 *  line leaves them identical — the common case, and it has to cost nothing. */
function sameLines(a: Set<number>, b: Set<number>): boolean {
	if (a.size !== b.size) return false;
	for (const line of a) if (!b.has(line)) return false;
	return true;
}

const liveFormatPlugin = ViewPlugin.fromClass(
	class {
		decorations: DecorationSet;
		atomic: DecorationSet;
		revealed: Set<number>;
		constructor(view: EditorView) {
			this.revealed = revealedLines(view.state);
			({ decorations: this.decorations, atomic: this.atomic } = buildLiveDecorations(
				view.state,
				view.visibleRanges,
				this.revealed
			));
		}
		update(u: ViewUpdate) {
			// The background parser can finish AFTER the keystroke's rebuild —
			// without this, a bullet on a freshly parsed line stays undecorated
			// until the next edit.
			const treeAdvanced = syntaxTree(u.state) !== syntaxTree(u.startState);
			if (!u.docChanged && !u.viewportChanged && !treeAdvanced) {
				if (!u.selectionSet) return;
				const revealed = revealedLines(u.state);
				// The caret moved but the same lines are revealed, so every
				// decoration would come out identical. Don't build them again.
				if (sameLines(revealed, this.revealed)) return;
				this.revealed = revealed;
			} else {
				this.revealed = revealedLines(u.state);
			}
			({ decorations: this.decorations, atomic: this.atomic } = buildLiveDecorations(
				u.state,
				u.view.visibleRanges,
				this.revealed
			));
		}
	},
	{
		decorations: (v) => v.decorations,
		// A hidden marker is one thing, not a run of invisible characters: the
		// arrows, Home and Backspace step over the whole `**` or bullet instead
		// of parking the caret inside a zero-width replacement.
		provide: (plugin) =>
			EditorView.atomicRanges.of((view) => view.plugin(plugin)?.atomic ?? Decoration.none)
	}
);

const formatModeTheme = EditorView.baseTheme({
	// CSS shapes, sized in em so they track the editor font. 0.5em ≈ the old
	// 2x-scaled glyph minus 25%.
	'.cm-list-bullet': {
		display: 'inline-block',
		width: '0.375em',
		height: '0.375em',
		boxSizing: 'border-box',
		verticalAlign: 'middle',
		borderRadius: '50%',
		background: 'var(--text-muted, currentColor)'
	},
	'.cm-list-bullet--square': {
		width: '0.315em',
		height: '0.315em',
		borderRadius: '0.045em'
	},
	'.cm-list-number': { color: 'var(--text-muted, inherit)' }
});

/** Line numbers currently on screen, ascending and without repeats.
 *
 * A plugin here used to walk every line of the document on each update, with
 * two `doc.line()` tree walks and a JSON.parse per line. On a long note that
 * is thousands of wasted lookups per keystroke to decorate the ~50 lines
 * actually visible.
 */
function* visibleLines(
	state: EditorState,
	ranges: readonly Span[],
	startAt = 1
): Generator<number> {
	const doc = state.doc;
	let previous = 0;
	for (const { from, to } of ranges) {
		const first = Math.max(startAt, doc.lineAt(from).number, previous + 1);
		const last = doc.lineAt(to).number;
		for (let lineNumber = first; lineNumber <= last; lineNumber += 1) yield lineNumber;
		previous = Math.max(previous, last);
	}
}

/** What one walk of the document found: the lines to hide, plus the geometry
 *  that decides whether a later edit could have changed the answer. */
export interface MetadataScan {
	/** The 1-based line numbers to hide, ascending. */
	lines: number[];
	/** Last line of the frontmatter block; 0 when the note has none. */
	frontmatterEnd: number;
	/** The `## Log` section: `Infinity` for "no log" and for "runs to the end". */
	logStart: number;
	logEnd: number;
	isMediaNote: boolean;
}

/** Hide bookkeeping EXSCALIA maintains while live preview is active: the
 * frontmatter fences and app-managed keys, a media item's structured record
 * and its `## Log` ledger, and the hidden task-id comments. Every line stays
 * in the document and reappears unchanged in raw mode.
 *
 * Pure over the document — the selection decides which of these lines are
 * revealed, later. This is a walk of the WHOLE document, so its result is
 * cached; `scanSurvives` below says when the cache still holds. */
export function scanMetadata(state: EditorState): MetadataScan {
	const doc = state.doc;
	const hidden: number[] = [];
	if (doc.lines < 1) {
		return { lines: hidden, frontmatterEnd: 0, logStart: Infinity, logEnd: Infinity, isMediaNote: false };
	}

	const hasFrontmatter = doc.lines >= 2 && doc.line(1).text.trim() === '---';

	// A media item's whole structured record is managed by OMNIA and the media
	// page — bookkeeping, so it hides in format mode. Only when the note
	// declares `media:`; an ordinary note carrying its own `year:` keeps it.
	let isMediaNote = false;
	let frontmatterEnd = 0;
	if (hasFrontmatter) {
		for (let lineNumber = 2; lineNumber <= doc.lines; lineNumber += 1) {
			const text = doc.line(lineNumber).text;
			if (text.trim() === '---') {
				frontmatterEnd = lineNumber;
				break;
			}
			if (/^\s*media\s*:/i.test(text)) isMediaNote = true;
		}
	}

	if (frontmatterEnd) {
		// The frontmatter fences are structural file syntax, not note content.
		hidden.push(1);

		const hiddenKeys = isMediaNote
			? /^\s*(created|modified|title|pinned|media|media_status|creator|year|cover|backdrop|theme|rating|started|finished|total|source)\s*:/i
			: /^\s*(created|modified|title|pinned)\s*:/i;

		let inTagsList = false;
		let inThemeBlock = false;
		for (let lineNumber = 2; lineNumber < frontmatterEnd; lineNumber += 1) {
			const text = doc.line(lineNumber).text;
			// App-managed frontmatter. These are bookkeeping the app writes and
			// maintains, so they are noise while reading or writing; Raw mode
			// still shows the file exactly as it is on disk. Tags belong to the
			// panel now, so the entry and its dash items hide with the rest.
			const isTagsKey: boolean = /^\s*tags\s*:/i.test(text);
			const isTagsItem: boolean = inTagsList && /^\s*-\s*/.test(text);
			inTagsList = isTagsKey || isTagsItem;
			// theme: is a nested map — its indented children hide with it.
			const isThemeKey: boolean = isMediaNote && /^\s*theme\s*:/i.test(text);
			const isThemeChild: boolean = inThemeBlock && /^\s+\S/.test(text);
			inThemeBlock = isThemeKey || isThemeChild;
			if (isTagsKey || isTagsItem || isThemeChild || hiddenKeys.test(text)) {
				hidden.push(lineNumber);
			}
		}
		hidden.push(frontmatterEnd);
	}

	// On media items the ## Log section is the app's ledger — the media page
	// and panel render it, OMNIA writes it — so in format mode the note reads
	// as just the review. Raw mode shows the lines as ever.
	let logStart = Infinity;
	let logEnd = Infinity;
	let inFence = false;
	let lineNumber = frontmatterEnd;
	// One sequential pass for the body: the log section and the task comments.
	for (const text of doc.iterLines(frontmatterEnd + 1, doc.lines + 1)) {
		lineNumber += 1;
		const trimmed = text.trim();
		if (isMediaNote) {
			if (/^(?:```|~~~)/.test(trimmed)) {
				inFence = !inFence;
			} else if (!inFence) {
				if (logStart === Infinity) {
					if (/^##\s+log\s*$/i.test(trimmed)) logStart = lineNumber;
				} else if (logEnd === Infinity && /^##(?!#)/.test(trimmed)) {
					logEnd = lineNumber;
				}
			}
		}
		if (lineNumber >= logStart && lineNumber < logEnd) {
			hidden.push(lineNumber);
			continue;
		}
		// Cheap prefix test before the real one: nearly no line starts with `<`.
		if (trimmed.startsWith('<!--') && /^<!--\s*exscalia-task:\{.*\}\s*-->$/.test(trimmed)) {
			hidden.push(lineNumber);
		}
	}
	return { lines: hidden, frontmatterEnd, logStart, logEnd, isMediaNote };
}

/** The hidden line numbers alone. */
export function hiddenMetadataLines(state: EditorState): number[] {
	return scanMetadata(state).lines;
}

/** A line whose text could change what a scan finds: a heading (`## Log`, or
 *  the heading that ends it), a code fence (which decides whether a `## Log`
 *  counts at all), a frontmatter delimiter, or a task-id comment. */
const SCAN_TRIGGER = /^\s*(?:#|-{3}|~{3}|```|<!--)/;

/** Whether a cached scan still describes the document after `changes`.
 *
 * The scan walks the whole document, and it used to run on every keystroke and
 * every caret move. It only keys off four things, so an edit is inert unless it
 * can reach one of them:
 *
 *  1. the line numbering — the scan's answer IS a list of line numbers, so
 *     anything that adds or removes a line invalidates it;
 *  2. the frontmatter block, or any line the scan already hides;
 *  3. the `## Log` section of a media note, up to and including the heading
 *     that closes it;
 *  4. a line that reads — before OR after the edit — like a heading, a fence, a
 *     frontmatter delimiter or a task-id comment.
 *
 * What is left is ordinary typing in the body of a note, which is the case that
 * has to be free. */
export function scanSurvives(
	scan: MetadataScan,
	before: Text,
	after: Text,
	changes: ChangeDesc
): boolean {
	if (before.lines !== after.lines) return false;
	const hidden = new Set(scan.lines);
	let survives = true;
	changes.iterChangedRanges((fromA, toA, fromB, toB) => {
		if (!survives) return;
		const first = before.lineAt(fromA).number;
		const last = before.lineAt(toA).number;
		// Equal line totals are not enough on their own: a deletion here and an
		// insertion there would cancel out while every number in between moved.
		if (first !== after.lineAt(fromB).number || last !== after.lineAt(toB).number) {
			survives = false;
			return;
		}
		for (let line = first; line <= last; line += 1) {
			if (line <= scan.frontmatterEnd || hidden.has(line)) {
				survives = false;
				return;
			}
			if (line >= scan.logStart && line <= scan.logEnd) {
				survives = false;
				return;
			}
			if (SCAN_TRIGGER.test(before.line(line).text) || SCAN_TRIGGER.test(after.line(line).text)) {
				survives = false;
				return;
			}
		}
	});
	return survives;
}

/** Group ascending line numbers into `[first, last]` runs of adjacent lines. */
export function contiguousRuns(lines: readonly number[]): [number, number][] {
	const runs: [number, number][] = [];
	for (const line of lines) {
		const last = runs[runs.length - 1];
		if (last && line === last[1] + 1) last[1] = line;
		else if (!last || line > last[1]) runs.push([line, line]);
	}
	return runs;
}

/** One block replacement per hidden run, skipping any run the caret is in.
 * Block replacement — rather than a `display: none` line — is what keeps the
 * caret out: hidden lines hold no visible position at all, so vertical motion
 * cannot strand the cursor on an invisible line and the scroller has nothing
 * to jump to. A run the caret does reach simply comes back into view, the same
 * contract as marker hiding.
 *
 * `caretIsLive` false means there is no caret worth respecting yet — see the
 * field below — and every run stays hidden. */
function metadataDecorations(
	state: EditorState,
	runs: readonly [number, number][],
	caretIsLive: boolean
): DecorationSet {
	const builder = new RangeSetBuilder<Decoration>();
	const doc = state.doc;
	for (const [first, last] of runs) {
		const from = doc.line(first).from;
		const to = doc.line(last).to;
		// An empty run has no range to replace (CodeMirror rejects a zero-length
		// block replacement). Only a lone blank line can produce one; leaving it
		// visible is harmless.
		if (from === to) continue;
		if (caretIsLive && touchesSelection(state.selection, from, to)) continue;
		builder.add(from, to, hiddenRun);
	}
	return builder.finish();
}

/** The same thing from a bare state, scanning as it goes — for tests and for
 *  any caller that has no cached scan to hand. */
export function buildMetadataDecorations(state: EditorState, caretIsLive = true): DecorationSet {
	return metadataDecorations(state, contiguousRuns(scanMetadata(state).lines), caretIsLive);
}

/** Which runs the caret currently opens, as a comparable key. Two updates with
 *  the same key produce identical decorations, so the second can be skipped. */
function revealedRuns(
	state: EditorState,
	runs: readonly [number, number][],
	caretIsLive: boolean
): string {
	if (!caretIsLive) return '';
	const doc = state.doc;
	let key = '';
	for (let i = 0; i < runs.length; i += 1) {
		const from = doc.line(runs[i][0]).from;
		if (touchesSelection(state.selection, from, doc.line(runs[i][1]).to)) key += `${i},`;
	}
	return key;
}

const hiddenRun = Decoration.replace({ block: true });

function touchesSelection(selection: EditorSelection, from: number, to: number): boolean {
	return selection.ranges.some((range) => range.from <= to && range.to >= from);
}

/** A cursor sitting at the very start of the document, which is where every
 * freshly opened note starts out — the user never put it there. */
function isUntouched(selection: EditorSelection): boolean {
	return selection.ranges.length === 1 && selection.main.empty && selection.main.head === 0;
}

/** Whether there is a caret worth revealing metadata for. Without this, every
 * note would open showing its raw frontmatter: a new editor starts with a
 * cursor at position 0, which touches the first hidden run. Only real user
 * interaction counts — a typed edit, a user selection (a click, Mod-Home),
 * or a selection anywhere but the very start. Focus alone must NOT count:
 * notes auto-focus on open, and that was re-revealing every frontmatter. */
const caretIsLive = StateField.define<boolean>({
	create: (state) => !isUntouched(state.selection),
	update(value, tr) {
		return (
			value ||
			tr.docChanged ||
			tr.isUserEvent('select') ||
			!isUntouched(tr.state.selection)
		);
	}
});

/** Block decorations may not come from a view plugin — the view needs them for
 * height measurement before plugins run — so this is a state field. It holds
 * nothing but a projection of the document and the selection, which is why it
 * is safe to swap in and out of the configuration with the rest of format
 * mode. */
interface MetadataDecorations {
	decorations: DecorationSet;
	live: boolean;
	/** The document walk these decorations came from, kept so ordinary typing
	 *  and caret movement don't repeat it. */
	scan: MetadataScan;
	runs: [number, number][];
	revealed: string;
}

const hiddenMetadata = StateField.define<MetadataDecorations>({
	create(state) {
		const live = state.field(caretIsLive);
		const scan = scanMetadata(state);
		const runs = contiguousRuns(scan.lines);
		return {
			decorations: metadataDecorations(state, runs, live),
			live,
			scan,
			runs,
			revealed: revealedRuns(state, runs, live)
		};
	},
	update(value, tr) {
		const live = tr.state.field(caretIsLive);
		if (!tr.docChanged && !tr.selection && live === value.live) return value;
		const kept = !tr.docChanged || scanSurvives(value.scan, tr.startState.doc, tr.state.doc, tr.changes);
		const scan = kept ? value.scan : scanMetadata(tr.state);
		const runs = kept ? value.runs : contiguousRuns(scan.lines);
		const revealed = revealedRuns(tr.state, runs, live);
		// Nothing moved and nothing opened or closed: the same set, byte for byte.
		if (kept && !tr.docChanged && live === value.live && revealed === value.revealed) return value;
		return { decorations: metadataDecorations(tr.state, runs, live), live, scan, runs, revealed };
	},
	provide: (field) => EditorView.decorations.from(field, (value) => value.decorations)
});

// Order matters: `hiddenMetadata` reads `caretIsLive` during its own update,
// which only works for a field configured before it.
export const formattedMetadata = [caretIsLive, hiddenMetadata];

type Status = 'todo' | 'in_progress' | 'done';

class TaskStatusWidget extends WidgetType {
	constructor(
		readonly id: string,
		readonly status: Status,
		readonly change: (id: string, status: Status) => void
	) {
		super();
	}
	eq(other: TaskStatusWidget) {
		return other.id === this.id && other.status === this.status;
	}
	toDOM() {
		const button = document.createElement('button');
		button.type = 'button';
		button.className = `cm-task-status cm-task-${this.status}`;
		button.textContent =
			this.status === 'done' ? '✓' : this.status === 'in_progress' ? '◐' : '○';
		button.title = `Task status: ${this.status.replace('_', ' ')}. Click to advance.`;
		button.setAttribute('aria-label', button.title);
		button.onmousedown = (event) => event.preventDefault();
		button.onclick = (event) => {
			event.preventDefault();
			event.stopPropagation();
			const next: Status =
				this.status === 'todo'
					? 'in_progress'
					: this.status === 'in_progress'
						? 'done'
						: 'todo';
			this.change(this.id, next);
		};
		return button;
	}
	ignoreEvent() {
		return true;
	}
}

/** A checkbox on a task the app knows nothing about — one the user simply
 * typed. Clicking it edits the document, nothing more: no task record, no
 * server round trip. Same look as the registered kind, deliberately. */
class LocalTaskWidget extends WidgetType {
	constructor(readonly done: boolean) {
		super();
	}
	eq(other: LocalTaskWidget) {
		return other.done === this.done;
	}
	toDOM(view: EditorView) {
		const button = document.createElement('button');
		button.type = 'button';
		button.className = `cm-task-status cm-task-${this.done ? 'done' : 'todo'}`;
		button.textContent = this.done ? '✓' : '○';
		button.title = this.done ? 'Done. Click to reopen.' : 'Click to complete.';
		button.setAttribute('aria-label', button.title);
		button.onmousedown = (event) => event.preventDefault();
		button.onclick = (event) => {
			event.preventDefault();
			event.stopPropagation();
			// Ask the DOM where this widget ended up rather than trusting the
			// position it was built at — the document may have moved under it.
			const at = view.posAtDOM(button);
			const change = toggleTaskAt(view.state, at);
			if (change) view.dispatch({ changes: change, userEvent: 'input' });
		};
		return button;
	}
	ignoreEvent() {
		return true;
	}
}

/** The document change that flips the checkbox on the line holding `pos`, or
 * null if that line has no plain `- [ ]` / `- [x]` marker. */
export function toggleTaskAt(state: EditorState, pos: number): ChangeSpec | null {
	const line = state.doc.lineAt(Math.max(0, Math.min(pos, state.doc.length)));
	const match = /^(\s*[-*]\s+)\[([ xX])\]/.exec(line.text);
	if (!match) return null;
	const from = line.from + match[1].length + 1;
	return { from, to: from + 1, insert: match[2] === ' ' ? 'x' : ' ' };
}

export function taskStatusControls(change: (id: string, status: Status) => void) {
	const build = (state: EditorState, ranges: readonly Span[], revealed: Set<number>) => {
		const builder = new RangeSetBuilder<Decoration>();
		const doc = state.doc;
		for (const lineNumber of visibleLines(state, ranges)) {
			if (revealed.has(lineNumber)) continue;
			const line = doc.line(lineNumber);
			const match = /^(\s*[-*]\s+)\[( |\/|x|X)\]/.exec(line.text);
			if (!match) continue;
			const from = line.from + match[1].length;
			const metadata =
				lineNumber > 1
					? /^\s*<!--\s*exscalia-task:(\{.*\})\s*-->\s*$/.exec(doc.line(lineNumber - 1).text)
					: null;
			if (!metadata) {
				// A hand-typed task. `[/]` has no plain-markdown meaning, so it
				// keeps its raw text; the two standard states get a checkbox.
				if (match[2] === '/') continue;
				builder.add(
					from,
					from + 3,
					Decoration.replace({
						widget: new LocalTaskWidget(match[2].toLowerCase() === 'x')
					})
				);
				continue;
			}
			try {
				const value = JSON.parse(metadata[1]) as { id?: string };
				if (!value.id) continue;
				const status: Status =
					match[2].toLowerCase() === 'x'
						? 'done'
						: match[2] === '/'
							? 'in_progress'
							: 'todo';
				builder.add(
					from,
					from + 3,
					Decoration.replace({
						widget: new TaskStatusWidget(value.id, status, change)
					})
				);
			} catch {
				// Malformed metadata stays visible and editable.
			}
		}
		return builder.finish();
	};
	return ViewPlugin.fromClass(
		class {
			decorations: DecorationSet;
			revealed: Set<number>;
			constructor(view: EditorView) {
				this.revealed = revealedLines(view.state);
				this.decorations = build(view.state, view.visibleRanges, this.revealed);
			}
			update(update: ViewUpdate) {
				// Same skip as the marker plugin: a caret moving along one line
				// changes no checkbox, so it must not rebuild them. A background
				// parse finishing still rebuilds (fresh checkboxes need it).
				const treeAdvanced = syntaxTree(update.state) !== syntaxTree(update.startState);
				if (!update.docChanged && !update.viewportChanged && !treeAdvanced) {
					if (!update.selectionSet) return;
					const revealed = revealedLines(update.state);
					if (sameLines(revealed, this.revealed)) return;
					this.revealed = revealed;
				} else {
					this.revealed = revealedLines(update.state);
				}
				this.decorations = build(update.state, update.view.visibleRanges, this.revealed);
			}
		},
		{ decorations: (plugin) => plugin.decorations }
	);
}

export const taskStatusTheme = EditorView.baseTheme({
	'.cm-task-status': {
		width: '19px',
		height: '19px',
		margin: '0 4px 0 0',
		padding: '0',
		border: '1px solid var(--border)',
		borderRadius: '50%',
		background: 'var(--bg-panel)',
		color: 'var(--text-muted)',
		fontSize: '11px',
		lineHeight: '17px',
		verticalAlign: 'middle'
	},
	'.cm-task-status:hover': {
		borderColor: 'var(--accent)',
		color: 'var(--accent)',
		background: 'var(--accent-soft)'
	},
	'.cm-task-done': {
		borderColor: 'var(--accent)',
		background: 'var(--accent)',
		color: '#fff'
	}
});

// --- inline media and math -------------------------------------------------
// Both render as replace-widgets and both step aside on the cursor's line, so
// the raw markdown is always one click away — same contract as marker hiding.

class ImageWidget extends WidgetType {
	constructor(
		readonly url: string,
		readonly alt: string
	) {
		super();
	}
	eq(other: ImageWidget) {
		return other.url === this.url && other.alt === this.alt;
	}
	toDOM() {
		const wrap = document.createElement('span');
		wrap.className = 'cm-inline-image';
		if (this.url === 'uploading…') {
			wrap.classList.add('cm-image-pending');
			wrap.textContent = `Uploading ${this.alt}…`;
			return wrap;
		}
		const img = document.createElement('img');
		img.src = this.url;
		img.alt = this.alt;
		img.loading = 'lazy';
		img.onerror = () => {
			wrap.classList.add('cm-image-broken');
			wrap.textContent = `Missing image: ${this.alt || this.url}`;
		};
		wrap.appendChild(img);
		return wrap;
	}
	// Let CodeMirror handle clicks on the picture: it puts the caret at the
	// nearest position, which reveals the raw markdown behind it. Swallowing
	// them meant an image could not be clicked into at all.
	ignoreEvent() {
		return false;
	}
}

class MathWidget extends WidgetType {
	constructor(
		readonly tex: string,
		readonly display: boolean
	) {
		super();
	}
	eq(other: MathWidget) {
		return other.tex === this.tex && other.display === this.display;
	}
	toDOM() {
		const host = document.createElement('span');
		host.className = this.display ? 'cm-math cm-math-display' : 'cm-math';
		host.innerHTML = renderTex(this.tex, this.display);
		return host;
	}
	// As with images: a click is how you get at the TeX source.
	ignoreEvent() {
		return false;
	}
}

const IMAGE_RE = /!\[([^\]]*)\]\(([^)\s]+)\)/g;

/** Inline images and rendered math, appended to `into`. */
function collectRich(
	state: EditorState,
	ranges: readonly Span[],
	revealed: Set<number>,
	into: Decorated[]
): void {
	const widgets: { from: number; to: number; deco: Decoration }[] = [];
	const doc = state.doc;

	for (const { from, to } of ranges) {
		const text = state.sliceDoc(from, to);

		IMAGE_RE.lastIndex = 0;
		let image: RegExpExecArray | null;
		while ((image = IMAGE_RE.exec(text)) !== null) {
			const start = from + image.index;
			if (revealed.has(doc.lineAt(start).number)) continue;
			widgets.push({
				from: start,
				to: start + image[0].length,
				deco: Decoration.replace({ widget: new ImageWidget(image[2], image[1]) })
			});
		}

		for (const span of findMath(text)) {
			const start = from + span.from;
			if (revealed.has(doc.lineAt(start).number)) continue;
			widgets.push({
				from: start,
				to: from + span.to,
				deco: Decoration.replace({ widget: new MathWidget(span.tex, span.display) })
			});
		}
	}

	// Images and math are found in separate passes, so sort before the overlap
	// check below can mean anything.
	widgets.sort((a, b) => a.from - b.from || a.to - b.to);
	let lastTo = -1;
	for (const w of widgets) {
		if (w.from < lastTo) continue; // never emit overlapping replacements
		into.push(w.deco.range(w.from, w.to));
		lastTo = w.to;
	}
}

/** Just the inline media, for tests and for reading in isolation. */
export function buildRichDecorations(
	state: EditorState,
	ranges: readonly Span[],
	revealed: Set<number> = revealedLines(state)
): DecorationSet {
	const into: Decorated[] = [];
	collectRich(state, ranges, revealed, into);
	return Decoration.set(into, true);
}

const richContentTheme = EditorView.baseTheme({
	// The caret goes where you click, so the pointer says so.
	'.cm-inline-image, .cm-math': { cursor: 'text' },
	'.cm-inline-image img': {
		maxWidth: '100%',
		maxHeight: '420px',
		borderRadius: '8px',
		border: '1px solid var(--border)',
		display: 'block',
		margin: '6px 0'
	},
	'.cm-image-pending, .cm-image-broken': {
		display: 'inline-block',
		padding: '2px 8px',
		borderRadius: '6px',
		background: 'var(--bg-panel)',
		border: '1px dashed var(--border)',
		color: 'var(--text-muted)',
		fontSize: '12px'
	},
	'.cm-math-display': { display: 'block', margin: '8px 0', textAlign: 'center' }
});

/** The formatted view (live preview): hidden markdown markers, real bullets,
 *  inline images and rendered LaTeX. One plugin, one decoration set — the
 *  markers and the inline media used to be two, walking the viewport twice per
 *  keystroke for the same answer. */
export const formatMode = [liveFormatPlugin, formatModeTheme, richContentTheme];
