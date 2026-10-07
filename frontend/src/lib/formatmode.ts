import { syntaxTree } from '@codemirror/language';
import { ICONS } from './icons';
import { inCode } from './inputrules';
import { findMath, renderTex } from './math';
import {
	EditorState,
	Prec,
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
	keymap,
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

// Read-mode look, brought into live preview. Blockquotes gain a left border,
// fenced code a panel background (its fence lines muted), and inline code a
// subtle chip — one line/mark decoration each, height-neutral so a view plugin
// may provide them.
const quoteLineDeco = Decoration.line({ class: 'cm-blockquote' });
const codeBlockLineDeco = Decoration.line({ class: 'cm-code-block' });
const codeFenceLineDeco = Decoration.line({ class: 'cm-code-block cm-code-fence' });
const inlineCodeMark = Decoration.mark({ class: 'cm-inline-code' });

/** A `---`/`***` thematic break, drawn as a rule when the caret is elsewhere.
 *  Replacing the source with a widget (rather than a `display:none` line) keeps
 *  the caret out of it — the same contract the bullets and hidden markers use. */
class RuleWidget extends WidgetType {
	eq() {
		return true;
	}
	toDOM() {
		const span = document.createElement('span');
		span.className = 'cm-hr';
		return span;
	}
	// A click lands the caret next to the rule, which reveals the raw `---`.
	ignoreEvent() {
		return false;
	}
}
const ruleDeco = Decoration.replace({ widget: new RuleWidget() });

/** Last line of a leading `---` frontmatter block, or 0 for none. The body's
 *  own `---` rules render, but the frontmatter fences are hidden as a block by
 *  the metadata field — drawing a rule over them too would collide. */
function frontmatterEndLine(doc: Text): number {
	if (doc.lines < 2 || doc.line(1).text.trim() !== '---') return 0;
	for (let n = 2; n <= doc.lines; n += 1) {
		if (doc.line(n).text.trim() === '---') return n;
	}
	return 0;
}

const FENCE_RE = /^\s*(?:```|~~~)/;

// A hanging indent for wrapped list items. Without it, a bullet whose text
// spills onto a second visual row sends that row to the left margin — left of
// the bullet itself — instead of hanging under the item's text. `padding-left`
// holds every wrapped row at the content column; the matching negative
// `text-indent` pulls the first row (leading spaces + marker) back to the
// normal gutter, so the marker still sits where it did. 6px is CodeMirror's
// default `.cm-line` left padding, kept so list lines start where prose does;
// the per-level bump keeps nesting a touch deeper (the earlier 1.25x request).
const LINE_PAD_LEFT_PX = 6; // CodeMirror default `.cm-line` padding-left
const INDENT_EXTRA_EM = 0.14;
const indentLineCache = new Map<string, Decoration>();
function listHangIndent(cols: number, depth: number): Decoration {
	const key = `${cols}:${depth}`;
	let deco = indentLineCache.get(key);
	if (!deco) {
		const bump = ((depth - 1) * INDENT_EXTRA_EM).toFixed(2);
		deco = Decoration.line({
			attributes: {
				style:
					`padding-left: calc(${LINE_PAD_LEFT_PX}px + ${cols}ch + ${bump}em);` +
					` text-indent: -${cols}ch;`
			}
		});
		indentLineCache.set(key, deco);
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
	const frontmatterEnd = frontmatterEndLine(doc);

	const hide = (from: number, to: number, deco: Decoration) => {
		into.push(deco.range(from, to));
		atomic.push(deco.range(from, to));
	};

	for (const { from, to } of ranges) {
		// Line decorations for the visible portion of a multi-line block.
		const decorateLines = (nodeFrom: number, nodeTo: number, fenceAware: boolean) => {
			const firstLine = doc.lineAt(Math.max(from, nodeFrom)).number;
			const lastLine = doc.lineAt(Math.min(to, Math.max(nodeFrom, nodeTo))).number;
			for (let ln = firstLine; ln <= lastLine; ln += 1) {
				const line = doc.line(ln);
				if (fenceAware) {
					into.push(
						(FENCE_RE.test(line.text) ? codeFenceLineDeco : codeBlockLineDeco).range(line.from)
					);
				} else {
					into.push(quoteLineDeco.range(line.from));
				}
			}
		};

		syntaxTree(state).iterate({
			from,
			to,
			enter(node) {
				if (node.name === 'Blockquote') {
					// The `>` marks are hidden by the QuoteMark pass below; this adds
					// the left-border block that reads like read mode.
					decorateLines(node.from, node.to, false);
					return;
				}
				if (node.name === 'FencedCode') {
					decorateLines(node.from, node.to, true);
					// Nothing inside needs marker hiding — the fence lines are muted
					// as whole lines and the body is highlighted separately.
					return false;
				}
				if (node.name === 'HorizontalRule') {
					const line = doc.lineAt(node.from).number;
					// A `---` inside the frontmatter fence is the block the metadata
					// field already hides; leave it to that one.
					if (frontmatterEnd && line <= frontmatterEnd) return;
					if (!revealed.has(line)) hide(node.from, node.to, ruleDeco);
					return;
				}
				if (node.name === 'InlineCode') {
					// A mark (not a replace): the caret sits in it normally, the
					// backtick CodeMarks inside are hidden by the pass below.
					into.push(inlineCodeMark.range(node.from, node.to));
					return;
				}
				if (node.name === 'Link') {
					const line = doc.lineAt(node.from).number;
					if (revealed.has(line)) return;
					// Only an inline `[text](url)` link is condensed to its text.
					// A shortcut/reference link (no URL) — which is how `[[wiki]]`
					// parses — keeps its brackets, so its own rendering is untouched.
					const marks: { from: number; to: number }[] = [];
					let hasUrl = false;
					for (let child = node.node.firstChild; child; child = child.nextSibling) {
						if (child.name === 'LinkMark') marks.push({ from: child.from, to: child.to });
						else if (child.name === 'URL') hasUrl = true;
					}
					if (!hasUrl || marks.length < 2) return;
					// Hide the opening `[` and the trailing `](url)`, leaving `text`.
					hide(marks[0].from, marks[0].to, Decoration.replace({}));
					hide(marks[1].from, node.to, Decoration.replace({}));
					return;
				}
				if (node.name === 'ListMark') {
					const mark = state.sliceDoc(node.from, node.to);
					// Nesting depth = enclosing list count: 1 at the top level.
					let depth = 0;
					for (let p = node.node.parent; p; p = p.parent) {
						if (p.name === 'BulletList' || p.name === 'OrderedList') depth++;
					}
					// Hang wrapped rows under the item text (and give nested items
					// the ~25%-deeper bump). `cols` = characters before the item
					// content: leading indent + marker + the space(s) after it.
					const line = doc.lineAt(node.from);
					let contentStart = node.to;
					while (
						contentStart < line.to &&
						state.sliceDoc(contentStart, contentStart + 1) === ' '
					) {
						contentStart += 1;
					}
					into.push(listHangIndent(contentStart - line.from, depth).range(line.from));
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
	collectRich(state, ranges, revealed, decorations, atomic);
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
	'.cm-list-number': { color: 'var(--text-muted, inherit)' },
	// Blockquote: the read-mode left rule. QuoteMark hiding handles the `>`.
	'.cm-line.cm-blockquote': {
		borderLeft: '3px solid var(--border)',
		paddingLeft: '14px',
		color: 'var(--text-muted)'
	},
	// Fenced code: a panel-backed block, its ``` fence lines muted.
	'.cm-line.cm-code-block': {
		background: 'var(--bg-panel)',
		fontFamily: 'var(--font-mono)',
		fontSize: '0.9em'
	},
	'.cm-line.cm-code-fence': { color: 'var(--text-muted)' },
	// Inline code chip.
	'.cm-inline-code': {
		background: 'var(--bg-panel)',
		borderRadius: '4px',
		padding: '1px 5px',
		fontFamily: 'var(--font-mono)',
		fontSize: '0.9em'
	},
	// Thematic break, drawn where the `---` source used to sit.
	'.cm-hr': {
		display: 'inline-block',
		width: '100%',
		borderTop: '1px solid var(--border)',
		verticalAlign: 'middle'
	}
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
 * and its `## Log` ledger, a recipe's record, and the hidden task-id comments. Every line stays
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
	// Likewise a recipe's record, which the recipe page and form write.
	let isRecipeNote = false;
	let frontmatterEnd = 0;
	if (hasFrontmatter) {
		for (let lineNumber = 2; lineNumber <= doc.lines; lineNumber += 1) {
			const text = doc.line(lineNumber).text;
			if (text.trim() === '---') {
				frontmatterEnd = lineNumber;
				break;
			}
			if (/^\s*media\s*:/i.test(text)) isMediaNote = true;
			if (RECIPE_MARK.test(text)) isRecipeNote = true;
		}
	}

	if (frontmatterEnd) {
		// The frontmatter fences are structural file syntax, not note content.
		hidden.push(1);

		const hiddenKeys = isMediaNote
			? /^\s*(created|modified|title|pinned|media|media_status|creator|year|cover|backdrop|theme|rating|started|finished|total|source)\s*:/i
			: /^\s*(created|modified|title|pinned)\s*:/i;

		// A recipe's record is whole YAML blocks, not one-line keys; it has a
		// walk of its own and skips the loop below.
		if (isRecipeNote) hidden.push(...recipeRecordLines(doc, frontmatterEnd));
		let inTagsList = false;
		let inThemeBlock = false;
		for (let lineNumber = 2; lineNumber < frontmatterEnd && !isRecipeNote; lineNumber += 1) {
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

/** `recipe: 1` at the margin: the strict mark of a recipe note (`recipe: true`
 *  is an ordinary note's own key). */
const RECIPE_MARK = /^recipe\s*:\s*1\s*(?:#.*)?$/;

/** The top-level keys of a recipe's record, plus the ones every note hides. */
const RECIPE_RECORD =
	/^(created|modified|title|pinned|tags|recipe|servings|prep_minutes|cook_minutes|recipe_tags|source_type|source_url|thumbnail|ingredients|steps|art|art_colors|art_palette)\s*:/;

/** The frontmatter lines of a recipe note that belong to its record.
 *
 * Several keys are blocks: `ingredients:` and `steps:` are lists of mappings,
 * `art:` a list of strings, `art_palette:` a mapping, and a long string wraps
 * onto indented lines. A key's block is every line after it that is indented
 * or a `-` item at the margin, so a managed key hides with its whole block and
 * a key the user added, children and all, stays. A blank line (a quoted
 * string's line break, say) goes with the line after it: hidden when that line
 * is, the closing fence included. */
function recipeRecordLines(doc: Text, frontmatterEnd: number): number[] {
	const hidden: number[] = [];
	let hiding = false;
	let blanks: number[] = [];
	for (let lineNumber = 2; lineNumber < frontmatterEnd; lineNumber += 1) {
		const text = doc.line(lineNumber).text;
		if (!text.trim()) {
			blanks.push(lineNumber);
			continue;
		}
		if (!/^(?:\s|-(?:\s|$))/.test(text)) hiding = RECIPE_RECORD.test(text);
		if (hiding) hidden.push(...blanks, lineNumber);
		blanks = [];
	}
	hidden.push(...blanks);
	return hidden;
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
 * null if that line has no plain `- [ ]` / `- [x]` marker. A marker inside
 * code is text, not a task — the same rule `buildTaskControls` follows, so a
 * checkbox is never drawn where a click would decline, or the reverse. */
export function toggleTaskAt(state: EditorState, pos: number): ChangeSpec | null {
	const line = state.doc.lineAt(Math.max(0, Math.min(pos, state.doc.length)));
	const match = /^(\s*[-*]\s+)\[([ xX])\]/.exec(line.text);
	if (!match) return null;
	const bracket = line.from + match[1].length;
	if (inCode(state, bracket, 1)) return null;
	const from = bracket + 1;
	return { from, to: from + 1, insert: match[2] === ' ' ? 'x' : ' ' };
}

/** The checkbox controls on the visible task lines — registered and hand-typed
 *  alike. A `- [ ]` inside code is left as the text it is. */
export function buildTaskControls(
	state: EditorState,
	ranges: readonly Span[],
	change: (id: string, status: Status) => void,
	revealed: Set<number> = revealedLines(state)
): DecorationSet {
	const builder = new RangeSetBuilder<Decoration>();
	const doc = state.doc;
	for (const lineNumber of visibleLines(state, ranges)) {
		if (revealed.has(lineNumber)) continue;
		const line = doc.line(lineNumber);
		const match = /^(\s*[-*]\s+)\[( |\/|x|X)\]/.exec(line.text);
		if (!match) continue;
		const from = line.from + match[1].length;
		if (inCode(state, from, 1)) continue;
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
}

export function taskStatusControls(change: (id: string, status: Status) => void) {
	const build = (state: EditorState, ranges: readonly Span[], revealed: Set<number>) =>
		buildTaskControls(state, ranges, change, revealed);
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
		position: 'relative',
		width: '19px',
		height: '19px',
		// The mobile button floor would stretch the circle into a 19x44 oval
		// and every task line with it; the ::after below is the target instead.
		minHeight: '0',
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
	// Line-tall (17px padding box + 10) and reaching no further right than
	// the margin: taller and stacked tasks would take each other's taps,
	// wider and it would swallow the first letter.
	'.cm-task-status::after': {
		content: '""',
		position: 'absolute',
		inset: '-5px'
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
// Both render as replace-widgets. Math steps aside on the cursor's line, so its
// TeX is one click away — same contract as marker hiding. An image never does:
// it is one object, like a picture in a document. A click selects it, the
// arrows and Backspace treat it as one character, and its link stays hidden.

/** The text a placeholder carries while its file uploads. */
export const UPLOADING = 'uploading…';

const IMAGE_SOURCE = /^!\[([^\]]*)\]\(([^)\s]+)\)$/;

/** An image in the document: where its markdown sits and what it says. */
export interface NoteImage {
	from: number;
	to: number;
	url: string;
	alt: string;
}

/** The image the main selection covers exactly, or null. */
export function selectedImage(state: EditorState): NoteImage | null {
	const { from, to, empty } = state.selection.main;
	// No image source is anywhere near this long; skip slicing a big selection.
	if (empty || to - from > 4096) return null;
	const match = IMAGE_SOURCE.exec(state.sliceDoc(from, to));
	return match ? { from, to, alt: match[1], url: match[2] } : null;
}

/** Delete an image. One that has its line to itself takes the line with it,
 *  so no blank line is left where the picture was. */
export function removeImage(view: EditorView, image: Pick<NoteImage, 'from' | 'to'>): void {
	const doc = view.state.doc;
	const line = doc.lineAt(image.from);
	let { from, to } = image;
	if (line.from === from && line.to === to) {
		if (line.number < doc.lines) to += 1;
		else if (line.number > 1) from -= 1;
	}
	view.dispatch({ changes: { from, to }, selection: { anchor: from }, userEvent: 'delete' });
}

/** Backspace or Delete on a selected picture: the same tidy removal as the
 *  toolbar's, rather than a hole left on an empty line. */
const deleteSelectedImage = (view: EditorView): boolean => {
	const image = selectedImage(view.state);
	if (!image) return false;
	removeImage(view, image);
	return true;
};

/** The picture being dragged, while a drag that began on one is in flight. */
let dragging: { view: EditorView; image: NoteImage } | null = null;

/** A picture dropped in the note moves there as a block of its own: onto a
 *  new line after the line it was dropped on, or before it when dropped at
 *  that line's very start. CodeMirror's own move would splice it in mid-text. */
function dropImage(view: EditorView, event: DragEvent): boolean {
	const held = dragging;
	dragging = null;
	if (!held || held.view !== view) return false;
	const { image } = held;
	const source = view.state.sliceDoc(image.from, image.to);
	if (!IMAGE_SOURCE.test(source) || event.dataTransfer?.getData('text/plain') !== source) return false;
	const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
	if (pos === null) return false;
	event.preventDefault();
	const doc = view.state.doc;
	const target = doc.lineAt(pos);
	const home = doc.lineAt(image.from);
	if (target.number === home.number) return true; // dropped where it was
	// The source goes as `removeImage` takes it: with its line when it has one.
	let delFrom = image.from;
	let delTo = image.to;
	if (home.from === image.from && home.to === image.to) {
		if (home.number < doc.lines) delTo += 1;
		else delFrom -= 1;
	}
	const before = pos === target.from && target.length > 0;
	const insert = before
		? { from: target.from, insert: source + '\n' }
		: { from: target.to, insert: (target.length ? '\n' : '') + source };
	const changes = view.state.changes([{ from: delFrom, to: delTo }, insert]);
	// The picture comes out selected where it landed.
	const start = before
		? changes.mapPos(insert.from, -1)
		: changes.mapPos(insert.from, 1) - source.length;
	view.dispatch({
		changes,
		selection: { anchor: start, head: start + source.length },
		userEvent: 'move.drop'
	});
	view.focus();
	return true;
}

const imageKeysAndDrops = [
	Prec.high(
		keymap.of([
			{ key: 'Backspace', run: deleteSelectedImage },
			{ key: 'Delete', run: deleteSelectedImage }
		])
	),
	EditorView.domEventHandlers({
		drop: (event, view) => dropImage(view, event),
		dragend: () => {
			dragging = null;
			return false;
		}
	})
];

/** What an image's toolbar asks the editor around it to do. Delete is done
 *  here; the rest need the clipboard, the network or a dialog, which this
 *  module stays clear of. The event bubbles from the widget. */
export interface ImageActionDetail {
	action: 'copy' | 'view' | 'download';
	image: NoteImage;
}
export const IMAGE_ACTION_EVENT = 'note-image-action';

const IMAGE_TOOLS: { action: ImageActionDetail['action'] | 'delete'; label: string; path: string }[] = [
	{ action: 'copy', label: 'Copy image', path: ICONS.copy },
	{ action: 'view', label: 'View full size', path: ICONS.expand },
	{ action: 'download', label: 'Download', path: ICONS.download },
	{ action: 'delete', label: 'Delete image', path: ICONS.trash }
];

function toolButton(label: string, path: string): HTMLButtonElement {
	const button = document.createElement('button');
	button.type = 'button';
	button.title = label;
	button.setAttribute('aria-label', label);
	const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
	svg.setAttribute('viewBox', '0 0 24 24');
	svg.setAttribute('aria-hidden', 'true');
	const stroke = document.createElementNS('http://www.w3.org/2000/svg', 'path');
	stroke.setAttribute('d', path);
	svg.appendChild(stroke);
	button.appendChild(svg);
	return button;
}

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
	/** The length of the markdown this widget stands for. */
	get sourceLength(): number {
		return this.alt.length + this.url.length + 5; // ![](…)
	}
	/** Where this widget's markdown is now, read back from the document so a
	 *  stale widget can never act on text that moved under it. */
	locate(view: EditorView, dom: HTMLElement): NoteImage | null {
		let from: number;
		try {
			from = view.posAtDOM(dom);
		} catch {
			return null;
		}
		const to = from + this.sourceLength;
		const match = IMAGE_SOURCE.exec(view.state.sliceDoc(from, to));
		return match ? { from, to, alt: match[1], url: match[2] } : null;
	}
	toDOM(view: EditorView) {
		const wrap = document.createElement('span');
		wrap.className = 'cm-inline-image';
		wrap.dataset.len = String(this.sourceLength);
		if (this.url === UPLOADING) {
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
			img.remove();
			tools.remove();
			wrap.prepend(`Missing image: ${this.alt || this.url}`);
		};
		wrap.appendChild(img);
		// CodeMirror carries the drag itself; this only remembers which picture
		// it is, so the drop can place it on a line of its own.
		img.addEventListener('dragstart', () => {
			const image = this.locate(view, wrap);
			dragging = image ? { view, image } : null;
		});

		const tools = document.createElement('span');
		tools.className = 'cm-image-tools';
		for (const tool of IMAGE_TOOLS) {
			const button = toolButton(tool.label, tool.path);
			// The editor keeps focus and its selection while a button is pressed.
			button.addEventListener('mousedown', (event) => event.preventDefault());
			button.addEventListener('click', (event) => {
				event.preventDefault();
				event.stopPropagation();
				const image = this.locate(view, wrap);
				if (!image) return;
				if (tool.action === 'delete') {
					removeImage(view, image);
					view.focus();
					return;
				}
				wrap.dispatchEvent(
					new CustomEvent<ImageActionDetail>(IMAGE_ACTION_EVENT, {
						bubbles: true,
						detail: { action: tool.action, image }
					})
				);
			});
			tools.appendChild(button);
		}
		wrap.appendChild(tools);

		// A click selects the whole picture; a double click opens it. Selecting
		// on click rather than mousedown leaves the press free to start a drag,
		// which CodeMirror turns into a move of the image's markdown.
		wrap.addEventListener('click', (event) => {
			if ((event.target as Element).closest('.cm-image-tools')) return;
			const image = this.locate(view, wrap);
			if (!image) return;
			view.dispatch({
				selection: { anchor: image.from, head: image.to },
				userEvent: 'select.pointer'
			});
			// A finger selecting a picture should not raise the keyboard.
			if (!window.matchMedia?.('(pointer: coarse)')?.matches) view.focus();
		});
		wrap.addEventListener('dblclick', (event) => {
			if ((event.target as Element).closest('.cm-image-tools')) return;
			const image = this.locate(view, wrap);
			if (!image) return;
			wrap.dispatchEvent(
				new CustomEvent<ImageActionDetail>(IMAGE_ACTION_EVENT, {
					bubbles: true,
					detail: { action: 'view', image }
				})
			);
		});
		return wrap;
	}
	// The widget handles its own pointer events. Drags and drops still go to
	// CodeMirror: it moves a dragged widget's source, and a file dropped on a
	// picture has to reach the editor's upload handler, not the browser.
	ignoreEvent(event: Event) {
		return !(event.type.startsWith('drag') || event.type === 'drop');
	}
}

/** Mark the images the selection covers, which shows their toolbar.
 *
 *  A class on the widget's own element rather than a decoration: a decoration
 *  change would redraw the picture, and on the desktop app every redraw is a
 *  fresh blob fetch. Runs after every update, once the DOM is in place. */
const imageSelection = EditorView.updateListener.of((u) => {
	const nodes = u.view.contentDOM.querySelectorAll<HTMLElement>('.cm-inline-image');
	if (!nodes.length) return;
	const ranges = u.state.selection.ranges.filter((r) => !r.empty);
	for (const node of nodes) {
		let selected = false;
		if (ranges.length) {
			try {
				const from = u.view.posAtDOM(node);
				const to = from + Number(node.dataset.len ?? 0);
				selected = ranges.some((r) => r.from <= from && r.to >= to);
			} catch {
				selected = false;
			}
		}
		node.classList.toggle('cm-image-selected', selected);
	}
});

class MathWidget extends WidgetType {
	constructor(
		readonly tex: string,
		readonly display: boolean,
		/** Drawn by `displayMathField` in place of whole lines: a real block. */
		readonly block = false
	) {
		super();
	}
	eq(other: MathWidget) {
		return other.tex === this.tex && other.display === this.display && other.block === this.block;
	}
	toDOM() {
		const host = document.createElement(this.block ? 'div' : 'span');
		host.className = this.display ? 'cm-math cm-math-display' : 'cm-math';
		if (this.block) host.classList.add('cm-math-block');
		host.innerHTML = renderTex(this.tex, this.display);
		return host;
	}
	// A click puts the caret in, which reveals the TeX source.
	ignoreEvent() {
		return false;
	}
}

/** A `$$` display block that spans lines, as whole lines: from the start of
 *  its opening line to the end of its closing one. */
export interface DisplayMath extends Span {
	tex: string;
}

const FENCE_MARKER = /^(```|~~~)/;

/** The TeX of a whole-line block — indentation and `$$` delimiters off. */
function blockTex(source: string): string {
	return source.trim().slice(2, -2).trim();
}

/** Every multi-line `$$` block in the document.
 *
 *  Line based, over the whole document, so no viewport edge can split a pair.
 *  A line that starts with `$$` and holds no second one opens a block (`$$x$$`
 *  is one line, which the plugin renders); the next line holding a `$$`
 *  closes it, provided that `$$` ends the line. One sitting mid-line leaves the
 *  pair as raw text rather than swallow what follows it. Inside fenced code a
 *  `$$` is code. */
export function scanDisplayMath(doc: Text): DisplayMath[] {
	const blocks: DisplayMath[] = [];
	let fence = '';
	let openAt = -1;
	let pos = 0;
	for (const text of doc.iterLines()) {
		const lineFrom = pos;
		pos += text.length + 1;
		const trimmed = text.trim();
		if (openAt >= 0) {
			const close = trimmed.indexOf('$$');
			if (close < 0) continue;
			if (close === trimmed.length - 2) {
				const to = lineFrom + text.length;
				blocks.push({ from: openAt, to, tex: blockTex(doc.sliceString(openAt, to)) });
			}
			openAt = -1;
			continue;
		}
		if (fence) {
			if (trimmed.startsWith(fence)) fence = '';
			continue;
		}
		const marker = FENCE_MARKER.exec(trimmed);
		if (marker) fence = marker[1];
		else if (trimmed.startsWith('$$') && !trimmed.includes('$$', 2)) openAt = lineFrom;
	}
	return blocks;
}

/** A line that can change what `scanDisplayMath` finds: one holding a `$`, or
 *  a code fence (which decides whether a `$$` counts at all). */
const DISPLAY_MATH_TRIGGER = /\$|^\s*(?:```|~~~)/m;

/** Whether the cached blocks still hold after `changes`, give or take a shift.
 *  Only `$` lines and fences decide the pairing, so an edit that reaches
 *  neither — ordinary typing, a paragraph between two blocks, a line inside
 *  one — cannot open, close or re-pair anything. */
export function displayMathSurvives(before: Text, after: Text, changes: ChangeDesc): boolean {
	let survives = true;
	changes.iterChangedRanges((fromA, toA, fromB, toB) => {
		if (!survives) return;
		const was = before.sliceString(before.lineAt(fromA).from, before.lineAt(toA).to);
		const is = after.sliceString(after.lineAt(fromB).from, after.lineAt(toB).to);
		if (DISPLAY_MATH_TRIGGER.test(was) || DISPLAY_MATH_TRIGGER.test(is)) survives = false;
	});
	return survives;
}

/** The cached blocks carried through `changes` that `displayMathSurvives`
 *  cleared. An edit inside a block leaves it a block but changes its TeX, so
 *  that block alone is read again. */
function mapDisplayMath(
	blocks: readonly DisplayMath[],
	doc: Text,
	changes: ChangeDesc
): DisplayMath[] {
	const edited: Span[] = [];
	changes.iterChangedRanges((_fromA, _toA, fromB, toB) => edited.push({ from: fromB, to: toB }));
	return blocks.map((block) => {
		const from = changes.mapPos(block.from);
		const to = changes.mapPos(block.to);
		const touched = edited.some((range) => range.from <= to && range.to >= from);
		return { from, to, tex: touched ? blockTex(doc.sliceString(from, to)) : block.tex };
	});
}

/** Which blocks the selection opens, as a comparable key. */
function revealedBlocks(selection: EditorSelection, blocks: readonly DisplayMath[]): string {
	let key = '';
	for (let i = 0; i < blocks.length; i += 1) {
		if (touchesSelection(selection, blocks[i].from, blocks[i].to)) key += `${i},`;
	}
	return key;
}

/** One rendered block per display block the selection is not in. Touching any
 *  of its lines brings the source back, the same contract as the markers. */
function displayMathDecorations(
	selection: EditorSelection,
	blocks: readonly DisplayMath[]
): DecorationSet {
	const builder = new RangeSetBuilder<Decoration>();
	for (const block of blocks) {
		// An empty block would render as nothing at all; its source says more.
		if (!block.tex || touchesSelection(selection, block.from, block.to)) continue;
		builder.add(
			block.from,
			block.to,
			Decoration.replace({ widget: new MathWidget(block.tex, true, true), block: true })
		);
	}
	return builder.finish();
}

interface DisplayMathDecorations {
	/** Every multi-line block, ascending — whether or not it is revealed. */
	blocks: DisplayMath[];
	decorations: DecorationSet;
	revealed: string;
}

/** Multi-line display math. Its replacement crosses line breaks, which only a
 *  state field may provide (HANDOFF §7) — from the plugin it threw out of the
 *  view update. The whole-document walk is cached like `hiddenMetadata`'s: an
 *  edit rescans only when it touches a `$` line or a fence, and a caret move
 *  that opens or closes no block returns the same value. */
export const displayMathField = StateField.define<DisplayMathDecorations>({
	create(state) {
		const blocks = scanDisplayMath(state.doc);
		return {
			blocks,
			decorations: displayMathDecorations(state.selection, blocks),
			revealed: revealedBlocks(state.selection, blocks)
		};
	},
	update(value, tr) {
		if (!tr.docChanged && !tr.selection) return value;
		let blocks = value.blocks;
		if (tr.docChanged) {
			blocks = displayMathSurvives(tr.startState.doc, tr.state.doc, tr.changes)
				? mapDisplayMath(blocks, tr.state.doc, tr.changes)
				: scanDisplayMath(tr.state.doc);
		}
		// A note without display math — nearly every note — stops here.
		if (!blocks.length && !value.blocks.length) return value;
		const revealed = revealedBlocks(tr.state.selection, blocks);
		if (!tr.docChanged && revealed === value.revealed) return value;
		return { blocks, decorations: displayMathDecorations(tr.state.selection, blocks), revealed };
	},
	provide: (field) => EditorView.decorations.from(field, (value) => value.decorations)
});

const IMAGE_RE = /!\[([^\]]*)\]\(([^)\s]+)\)/g;

/** Inline images and rendered math, appended to `into`; the images go into
 *  `atomic` too, so the caret steps over a picture in one move.
 *
 *  Matched a line at a time: a plugin may not replace a line break, and a
 *  match run over the viewport's raw text paired a `$$` with whichever one
 *  followed it — the next block's, when the viewport began inside a block.
 *  Multi-line display math belongs to `displayMathField`, whose lines are left
 *  alone here, and a match that starts inside code is code. Math on a revealed
 *  line shows its source; an image never does. */
function collectRich(
	state: EditorState,
	ranges: readonly Span[],
	revealed: Set<number>,
	into: Decorated[],
	atomic?: Decorated[]
): void {
	const widgets: { from: number; to: number; deco: Decoration; atomic: boolean }[] = [];
	const doc = state.doc;
	// Optional, so the builders still run headlessly against a bare state.
	const blocks = state.field(displayMathField, false)?.blocks ?? [];
	let block = 0;

	for (const lineNumber of visibleLines(state, ranges)) {
		const line = doc.line(lineNumber);
		// Lines come in ascending order, and so do the blocks.
		while (block < blocks.length && blocks[block].to < line.from) block += 1;
		if (block < blocks.length && blocks[block].from <= line.from) continue;
		const text = line.text;

		IMAGE_RE.lastIndex = 0;
		let image: RegExpExecArray | null;
		while ((image = IMAGE_RE.exec(text)) !== null) {
			const start = line.from + image.index;
			if (inCode(state, start, 1)) continue;
			widgets.push({
				from: start,
				to: start + image[0].length,
				deco: Decoration.replace({ widget: new ImageWidget(image[2], image[1]) }),
				atomic: true
			});
		}

		if (revealed.has(lineNumber)) continue;
		for (const span of findMath(text)) {
			const start = line.from + span.from;
			if (inCode(state, start, 1)) continue;
			widgets.push({
				from: start,
				to: line.from + span.to,
				deco: Decoration.replace({ widget: new MathWidget(span.tex, span.display) }),
				atomic: false
			});
		}
	}

	// Images and math are found in separate passes, so sort before the overlap
	// check below can mean anything.
	widgets.sort((a, b) => a.from - b.from || a.to - b.to);
	let lastTo = -1;
	for (const w of widgets) {
		if (w.from < lastTo) continue; // never emit overlapping replacements
		const range = w.deco.range(w.from, w.to);
		into.push(range);
		if (w.atomic) atomic?.push(range);
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
	// The caret goes where you click into math, so the pointer says so. A
	// picture is selected by a click instead.
	'.cm-math': { cursor: 'text' },
	'.cm-inline-image': {
		position: 'relative',
		display: 'inline-block',
		maxWidth: '100%',
		verticalAlign: 'top',
		margin: '6px 0',
		cursor: 'default'
	},
	'.cm-inline-image img': {
		maxWidth: '100%',
		maxHeight: '420px',
		borderRadius: '8px',
		border: '1px solid var(--border)',
		display: 'block'
	},
	// The native selection tint would wash over the picture; the ring says it.
	'.cm-inline-image img::selection': { backgroundColor: 'transparent' },
	'.cm-image-selected img': {
		outline: '2px solid var(--accent)',
		outlineOffset: '2px'
	},
	'.cm-image-pending, .cm-image-broken': {
		padding: '2px 8px',
		borderRadius: '6px',
		background: 'var(--bg-panel)',
		border: '1px dashed var(--border)',
		color: 'var(--text-muted)',
		fontSize: '12px'
	},
	'.cm-image-selected.cm-image-broken': { borderColor: 'var(--accent)', borderStyle: 'solid' },
	'.cm-image-tools': {
		position: 'absolute',
		top: '8px',
		right: '8px',
		display: 'none',
		gap: '2px',
		padding: '3px',
		border: '1px solid var(--border)',
		borderRadius: '8px',
		background: 'var(--bg-panel)',
		boxShadow: 'var(--lift-shadow)'
	},
	'.cm-image-selected .cm-image-tools': { display: 'flex' },
	'@media (hover: hover)': {
		'.cm-inline-image:hover .cm-image-tools': { display: 'flex' }
	},
	'.cm-image-tools button': {
		display: 'grid',
		placeItems: 'center',
		width: '28px',
		height: '26px',
		padding: '0',
		border: '0',
		borderRadius: '6px',
		background: 'none',
		color: 'var(--text)',
		cursor: 'pointer'
	},
	'.cm-image-tools button:hover': { background: 'var(--bg-hover)' },
	'.cm-image-tools svg': {
		width: '15px',
		height: '15px',
		fill: 'none',
		stroke: 'currentColor',
		strokeWidth: '2',
		strokeLinecap: 'round',
		strokeLinejoin: 'round'
	},
	'@media (max-width: 760px)': {
		'.cm-image-tools button': { width: '44px', height: '44px' },
		'.cm-image-tools svg': { width: 'var(--m-icon)', height: 'var(--m-icon)' }
	},
	'.cm-math-display': { display: 'block', margin: '8px 0', textAlign: 'center' },
	// A block widget's height is measured without its margins; pad instead.
	'.cm-math-block': { margin: '0', padding: '8px 0' }
});

/** The formatted view (live preview): hidden markdown markers, real bullets,
 *  inline images and rendered LaTeX. One plugin, one decoration set — the
 *  markers and the inline media used to be two, walking the viewport twice per
 *  keystroke for the same answer. Display math that spans lines is the one
 *  exception: it replaces line breaks, so it comes from its own state field. */
export const formatMode = [
	displayMathField,
	liveFormatPlugin,
	imageSelection,
	imageKeysAndDrops,
	formatModeTheme,
	richContentTheme
];
