// The formatted view decides three things per line: what to hide, what the
// caret may enter, and what a checkbox click writes. All three are pure
// functions of an EditorState here, so they test without a DOM.
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { syntaxTree } from '@codemirror/language';
import {
	EditorSelection,
	type ChangeSpec,
	type EditorState,
	type Extension,
	type Text
} from '@codemirror/state';
import type { DecorationSet } from '@codemirror/view';
import { describe, expect, it } from 'vitest';
import {
	buildFormatDecorations,
	buildLiveDecorations,
	buildMetadataDecorations,
	buildRichDecorations,
	buildTaskControls,
	contiguousRuns,
	displayMathField,
	removeImage,
	selectedImage,
	displayMathSurvives,
	hiddenMetadataLines,
	scanDisplayMath,
	scanMetadata,
	scanSurvives,
	toggleTaskAt
} from './formatmode';
import { markDoc, stateFrom } from './testing/cm';
import type { EditorView } from '@codemirror/view';

const MD = [markdown({ base: markdownLanguage })];

/** Every decorated range, as `[from, to]` pairs. */
function spans(set: DecorationSet): [number, number][] {
	const out: [number, number][] = [];
	for (const iter = set.iter(); iter.value; iter.next()) out.push([iter.from, iter.to]);
	return out;
}

/** Decorations for a marked document — `|` places the caret, whose line keeps
 *  its raw markers. */
function formatMarked(marked: string) {
	const state = stateFrom(marked, MD);
	return buildFormatDecorations(state, [{ from: 0, to: state.doc.length }]);
}

/** The common case: nothing revealed. The caret parks on a blank line below,
 *  which leaves every offset in `doc` where it was. */
const format = (doc: string) => formatMarked(`${doc}\n\n|`);

describe('buildFormatDecorations — what the caret may enter', () => {
	it('makes a hidden bullet atomic', () => {
		const { decorations, atomic } = format('- one');
		// [0,0] is the line hang-indent (wrapped rows align under the text);
		// [0,1] is the bullet replacement.
		expect(spans(decorations)).toEqual([
			[0, 0],
			[0, 1]
		]);
		expect(spans(atomic)).toEqual([[0, 1]]);
	});

	it('makes hidden emphasis marks atomic, both sides', () => {
		// `**bold**` — the marks are hidden, so arrows step over them whole.
		expect(spans(format('a **b** c').atomic)).toEqual([
			[2, 4],
			[5, 7]
		]);
	});

	it('hides the heading mark and its space as one unit', () => {
		expect(spans(format('## Title').atomic)).toEqual([[0, 3]]);
	});

	it('leaves the ordered-list number out of the atomic set', () => {
		// It is a mark, not a replacement: the digits stay real text and the
		// caret must be able to sit among them.
		const { decorations, atomic } = format('1. one');
		expect(spans(decorations)).toEqual([
			[0, 0],
			[0, 2]
		]);
		expect(spans(atomic)).toEqual([]);
	});

	it('reveals markers on the line the selection touches', () => {
		expect(spans(formatMarked('## Ti|tle').decorations)).toEqual([]);
	});

	it('keeps the bullet on the selected line — bullets never reveal', () => {
		const { decorations, atomic } = formatMarked('- on|e');
		expect(spans(decorations)).toEqual([
			[0, 0],
			[0, 1]
		]);
		expect(spans(atomic)).toEqual([[0, 1]]);
	});
});

describe('read-mode fidelity — blockquote, code, rule, inline code, links', () => {
	it('gives a blockquote a line decoration and hides its `>`', () => {
		const { decorations, atomic } = format('> quote');
		// [0,0] is the .cm-blockquote line style; [0,2] hides `> ` (mark + space).
		expect(spans(decorations)).toEqual([
			[0, 0],
			[0, 2]
		]);
		expect(spans(atomic)).toEqual([[0, 2]]);
	});

	it('styles fenced-code lines and does not descend into the fence', () => {
		// Three line decorations (two fences + the body line); no markers hidden,
		// so nothing atomic — the ``` fences are muted whole-line, not replaced.
		const { decorations, atomic } = format('```\ncode\n```');
		expect(spans(decorations)).toEqual([
			[0, 0],
			[4, 4],
			[9, 9]
		]);
		expect(spans(atomic)).toEqual([]);
	});

	it('replaces a horizontal rule the caret is not on', () => {
		// `text` on line 1, a blank line, then `---` on line 3.
		const { decorations, atomic } = format('text\n\n---');
		expect(spans(decorations)).toEqual([[6, 9]]);
		expect(spans(atomic)).toEqual([[6, 9]]);
	});

	it('reveals the rule source when the caret is on it', () => {
		expect(spans(formatMarked('text\n\n--|-').decorations)).toEqual([]);
	});

	it('marks inline code and still hides its backticks', () => {
		const { decorations, atomic } = format('a `b` c');
		// The chip mark spans the whole `` `b` ``; the two backticks are hidden.
		expect(spans(decorations)).toEqual([
			[2, 3],
			[2, 5],
			[4, 5]
		]);
		expect(spans(atomic)).toEqual([
			[2, 3],
			[4, 5]
		]);
	});

	it('condenses an inline `[text](url)` link to its text', () => {
		const { decorations, atomic } = format('[a](b)');
		// Hide the opening `[` and the trailing `](b)`, leaving `a`.
		expect(spans(decorations)).toEqual([
			[0, 1],
			[2, 6]
		]);
		expect(spans(atomic)).toEqual([
			[0, 1],
			[2, 6]
		]);
	});

	it('leaves a `[[wikilink]]` untouched — it has no URL', () => {
		expect(spans(format('[[Foo]]').decorations)).toEqual([]);
	});
});

describe('buildLiveDecorations — one set for markers and inline media', () => {
	// `\n\n|` parks the caret clear of everything, as `format` does above.
	const merged = (doc: string) => {
		const state = stateFrom(`${doc}\n\n|`, MD);
		const ranges = [{ from: 0, to: state.doc.length }];
		return {
			live: buildLiveDecorations(state, ranges),
			markers: buildFormatDecorations(state, ranges),
			media: buildRichDecorations(state, ranges)
		};
	};

	it('is exactly the two old sets, merged in document order', () => {
		const { live, markers, media } = merged('## Title\n\n![a](pic.png)\n\n- **one**');
		expect(spans(media)).not.toEqual([]);
		expect(spans(live.decorations)).toEqual(
			[...spans(markers.decorations), ...spans(media)].sort((a, b) => a[0] - b[0] || a[1] - b[1])
		);
	});

	it('adds images to the atomic set, and leaves math out of it', () => {
		const { live, markers } = merged('![a](pic.png)\n\n## Title\n\n$x$');
		// The image is one step for the caret; math stays enterable, since a
		// caret inside it is how its TeX gets edited.
		expect(spans(live.atomic)).toEqual([[0, 13], ...spans(markers.atomic)]);
	});

	it('still reveals the caret’s line', () => {
		const state = stateFrom('## Ti|tle\n\n![a](pic.png)', MD);
		const live = buildLiveDecorations(state, [{ from: 0, to: state.doc.length }]);
		// The heading mark is revealed; the image, on another line, is not.
		expect(spans(live.decorations)).toEqual(spans(buildRichDecorations(state, [{ from: 0, to: state.doc.length }])));
	});
});

const PLAIN_NOTE = [
	'---',
	'title: Notes',
	'created: 2026-01-01',
	'project: apollo',
	'tags:',
	'  - work',
	'---',
	'',
	'Body text.'
].join('\n');

const MEDIA_NOTE = [
	'---',
	'title: Dune',
	'media: film',
	'year: 1984',
	'theme:',
	'  accent: "#883322"',
	'---',
	'',
	'A review.',
	'',
	'## Log',
	'- 2026-01-01 watched',
	'',
	'## Notes',
	'After the log.'
].join('\n');

describe('hiddenMetadataLines', () => {
	it('hides the fences and app-managed keys, keeping the reader’s own', () => {
		// 4 (`project:`) is the user's, 8 and 9 are the body.
		expect(hiddenMetadataLines(stateFrom(PLAIN_NOTE))).toEqual([1, 2, 3, 5, 6, 7]);
	});

	it('hides a media record, its nested theme block and the whole Log section', () => {
		expect(hiddenMetadataLines(stateFrom(MEDIA_NOTE))).toEqual([1, 2, 3, 4, 5, 6, 7, 11, 12, 13]);
	});

	it('hides a Log section that runs to the end of the note', () => {
		const doc = ['---', 'media: film', '---', '', '## Log', '- entry', '- entry'].join('\n');
		expect(hiddenMetadataLines(stateFrom(doc))).toEqual([1, 2, 3, 5, 6, 7]);
		// …and the block replacement reaches the last character.
		const state = stateFrom(doc);
		const last = spans(buildMetadataDecorations(state, false)).pop();
		expect(last?.[1]).toBe(state.doc.length);
	});

	it('leaves `year:` alone on an ordinary note', () => {
		const doc = ['---', 'year: 1984', '---', 'Body.'].join('\n');
		expect(hiddenMetadataLines(stateFrom(doc))).toEqual([1, 3]);
	});

	it('ignores a `## Log` heading on a note that is not media', () => {
		const doc = ['---', 'title: T', '---', '', '## Log', 'kept'].join('\n');
		expect(hiddenMetadataLines(stateFrom(doc))).toEqual([1, 2, 3]);
	});

	it('ignores a fenced `## Log` inside a code block', () => {
		const doc = [
			'---',
			'media: film',
			'---',
			'```',
			'## Log',
			'```',
			'still visible'
		].join('\n');
		expect(hiddenMetadataLines(stateFrom(doc))).toEqual([1, 2, 3]);
	});

	it('hides the task-id comments the app writes above registered tasks', () => {
		const doc = [
			'Tasks:',
			'<!-- exscalia-task:{"id":"t1"} -->',
			'- [ ] wash up',
			'- [ ] hand typed'
		].join('\n');
		expect(hiddenMetadataLines(stateFrom(doc))).toEqual([2]);
	});

	it('hides nothing in a note without frontmatter', () => {
		expect(hiddenMetadataLines(stateFrom('Just prose.\nMore prose.'))).toEqual([]);
	});
});

/** A recipe note as the server writes it (`recipes.build_recipe_content`),
 *  with a user's own keys put between the managed blocks. */
const RECIPE_NOTE = [
	'---', // 1
	'title: Spaghetti Carbonara',
	"created: '2026-10-04T14:22:56Z'",
	"modified: '2026-10-04T14:22:56Z'",
	'recipe: 1', // 5
	'servings: 2',
	'prep_minutes: 10',
	'cook_minutes: 15',
	'recipe_tags:',
	'- pasta', // 10
	'- quick',
	'source_type: website',
	'source_url: https://example.com/carbonara',
	'ingredients:',
	'- id: butter', // 15
	'  amount: 2.0',
	"  unit: ''",
	'  item: butter',
	'  group: Sauce',
	'rating: 5', // 20 — the user's
	'pairing:', // 21 — the user's, with children that share managed names
	'  title: Frascati',
	'  steps: chill it',
	'steps:',
	"- text: 'Melt {butter} in a pan.", // 25
	'',
	"    Then stir until glossy.'",
	'  timer_seconds: 300',
	'- text: Serve.',
	'art:', // 30
	"- '   .-~~-.   '",
	"- '  (      )  '",
	'art_colors:',
	"- '   aaaaaa   '",
	"- '  bbbbbbbb  '", // 35
	'art_palette:',
	"  a: '#e8c070'",
	'---',
	'',
	'Free notes.' // 40
].join('\n');

const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);

describe('hiddenMetadataLines — recipe notes', () => {
	it('hides the whole record, block by block, and keeps the user’s keys whole', () => {
		expect(hiddenMetadataLines(stateFrom(RECIPE_NOTE))).toEqual([
			...range(1, 19),
			...range(24, 38)
		]);
	});

	it('hides one run before and one after the user’s keys', () => {
		expect(hiddenRuns(RECIPE_NOTE)).toEqual([
			[1, 19],
			[24, 38]
		]);
	});

	it('hides a blank line only where the block it sits in goes on', () => {
		const doc = [
			'---',
			'recipe: 1',
			'servings: 1',
			'',
			'mine: kept',
			'',
			'steps: []',
			'---',
			'Body.'
		].join('\n');
		// 4 sits before the user's key, so it stays; 6 sits before a managed one.
		expect(hiddenMetadataLines(stateFrom(doc))).toEqual([1, 2, 3, 6, 7, 8]);
	});

	it('hides everything but the body on a recipe with no notes', () => {
		const doc = [
			'---',
			'title: Toast',
			"created: '2026-10-04T14:22:56Z'",
			"modified: '2026-10-04T14:22:56Z'",
			'recipe: 1',
			'servings: 1',
			'ingredients: []',
			'steps: []',
			'---',
			'',
			''
		].join('\n');
		const state = stateFrom(doc);
		expect(hiddenMetadataLines(state)).toEqual(range(1, 9));
		// One block, ending at the fence: the empty body is still somewhere to type.
		expect(spans(buildMetadataDecorations(state, false))).toEqual([[0, state.doc.line(9).to]]);
	});

	it('treats `recipe: true` as an ordinary note’s own key', () => {
		const doc = ['---', 'title: T', 'recipe: true', 'servings: 2', '---', 'Body.'].join('\n');
		expect(hiddenMetadataLines(stateFrom(doc))).toEqual([1, 2, 5]);
	});

	it('re-walks the record when a key in it is edited', () => {
		expect(survives(RECIPE_NOTE, (line) => ({ from: line(20).to, insert: '0' }))).toBe(false);
		expect(survives(RECIPE_NOTE, (line) => ({ from: line(40).to, insert: ' More.' }))).toBe(true);
	});

	// Between the title and the body a recipe shows what a media note and an
	// ordinary note show: one hidden block ending at the closing fence, then
	// the blank line the server writes before the body.
	it('leaves the same space above the body as a media note and an ordinary one', () => {
		const recipe = [
			'---',
			'title: Pie',
			"created: '2026-10-04T14:22:56Z'",
			"modified: '2026-10-04T14:22:56Z'",
			'recipe: 1',
			'servings: 2',
			'ingredients:',
			'- id: egg',
			'  amount: 2.0',
			"  unit: ''",
			'  item: egg',
			'steps:',
			'- text: Whisk {egg}.',
			'art:',
			"- '  (  )  '",
			'art_palette:',
			"  a: '#e8c070'",
			'---',
			'',
			'Notes.'
		];
		const media = ['---', 'title: Dune', 'media: film', 'year: 1984', '---', '', 'A review.'];
		const plain = ['---', 'title: T', "created: '2026-10-04'", "modified: '2026-10-04'", '---', '', 'Body.'];
		for (const lines of [recipe, media, plain]) {
			const fence = lines.lastIndexOf('---') + 1;
			const state = stateFrom(lines.join('\n'));
			expect(spans(buildMetadataDecorations(state, false))).toEqual([[0, state.doc.line(fence).to]]);
		}
	});
});

describe('contiguousRuns', () => {
	it('groups adjacent lines and splits at gaps', () => {
		expect(contiguousRuns([1, 2, 3, 5, 8, 9])).toEqual([
			[1, 3],
			[5, 5],
			[8, 9]
		]);
	});

	it('has no runs for no lines', () => {
		expect(contiguousRuns([])).toEqual([]);
	});
});

/** The hidden runs as `[firstLine, lastLine]`, read back off the decorations.
 *  The caret defaults to the end of the note, well clear of the metadata. */
function hiddenRuns(doc: string, at?: number): [number, number][] {
	const base = stateFrom(doc);
	const state = base.update({
		selection: EditorSelection.cursor(at ?? base.doc.length)
	}).state;
	return spans(buildMetadataDecorations(state)).map(([from, to]) => [
		state.doc.lineAt(from).number,
		state.doc.lineAt(to).number
	]);
}

describe('buildMetadataDecorations — one block per run', () => {
	it('emits a single replacement for a contiguous hidden stretch', () => {
		// Lines 1-3 and 5-7: `project:` on line 4 splits them.
		expect(hiddenRuns(PLAIN_NOTE)).toEqual([
			[1, 3],
			[5, 7]
		]);
	});

	it('reveals the run the caret is inside, and only that one', () => {
		const inFrontmatter = stateFrom(PLAIN_NOTE).doc.line(2).from + 2;
		expect(hiddenRuns(PLAIN_NOTE, inFrontmatter)).toEqual([[5, 7]]);
	});

	it('reveals a run the caret touches at its very end', () => {
		expect(hiddenRuns(PLAIN_NOTE, stateFrom(PLAIN_NOTE).doc.line(7).to)).toEqual([[1, 3]]);
	});

	it('keeps a run hidden when the caret is on the line after it', () => {
		expect(hiddenRuns(PLAIN_NOTE, stateFrom(PLAIN_NOTE).doc.line(8).from)).toEqual([
			[1, 3],
			[5, 7]
		]);
	});

	it('hides every run while no caret is live — a note opens clean', () => {
		// A new editor puts its cursor at 0, touching the first run. Revealing
		// on that would mean every note opened showing its raw frontmatter.
		const state = stateFrom(PLAIN_NOTE);
		expect(state.selection.main.head).toBe(0);
		const runs = spans(buildMetadataDecorations(state, false)).map(([from]) => [
			state.doc.lineAt(from).number
		]);
		expect(runs).toEqual([[1], [5]]);
	});

	it('never emits a zero-length block replacement', () => {
		// CodeMirror rejects one, and a lone blank hidden line would produce it.
		for (const [from, to] of spans(buildMetadataDecorations(stateFrom(MEDIA_NOTE)))) {
			expect(to).toBeGreaterThan(from);
		}
	});
});

/** Run a change against a note and ask whether the cached scan still holds —
 *  and, when it does, prove it by rescanning and comparing. */
function survives(doc: string, change: (line: (n: number) => { from: number; to: number }) => ChangeSpec) {
	const state = stateFrom(doc);
	const scan = scanMetadata(state);
	const tr = state.update({ changes: change((n) => state.doc.line(n)) });
	const kept = scanSurvives(scan, tr.startState.doc, tr.state.doc, tr.changes);
	if (kept) {
		// The whole point of the cache: reusing it must give the same answer a
		// fresh walk would.
		expect(scanMetadata(tr.state).lines).toEqual(scan.lines);
	}
	return kept;
}

describe('scanSurvives — when the cached document walk still holds', () => {
	it('survives ordinary typing in the body', () => {
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(9).to, insert: ' More.' }))).toBe(true);
		expect(survives(PLAIN_NOTE, (line) => ({ from: line(9).to, insert: 'x' }))).toBe(true);
	});

	it('invalidates on any edit inside the frontmatter', () => {
		expect(survives(PLAIN_NOTE, (line) => ({ from: line(2).to, insert: '!' }))).toBe(false);
		// Even a line the scan leaves visible — it decides the block's extent.
		expect(survives(PLAIN_NOTE, (line) => ({ from: line(4).to, insert: '!' }))).toBe(false);
	});

	it('invalidates on an edit inside the media log, heading included', () => {
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(11).to, insert: '!' }))).toBe(false);
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(12).to, insert: '!' }))).toBe(false);
		// `## Notes` closes the log; editing it could reopen it.
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(14).to, insert: '!' }))).toBe(false);
	});

	it('invalidates when a line count changes — the cache is line numbers', () => {
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(9).to, insert: '\nnew' }))).toBe(false);
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(9).from, to: line(10).from }))).toBe(false);
	});

	it('invalidates when an edited line starts to look structural', () => {
		// A new heading could open or close a log section.
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(15).from, insert: '## ' }))).toBe(false);
		// A code fence flips whether a later `## Log` counts at all.
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(15).from, insert: '```' }))).toBe(false);
		// A task-id comment is hidden the moment it exists.
		expect(
			survives(MEDIA_NOTE, (line) => ({
				from: line(15).from,
				to: line(15).to,
				insert: '<!-- exscalia-task:{"id":"t1"} -->'
			}))
		).toBe(false);
		// …and so does one that stops looking like a heading.
		expect(survives(MEDIA_NOTE, (line) => ({ from: line(14).from, to: line(14).from + 3 }))).toBe(false);
	});
});

describe('decoration cost', () => {
	/** A note long enough that an O(document)-per-keystroke rebuild would show. */
	function bigNote(): string {
		const lines = ['---', 'title: Big', 'media: film', '---', ''];
		for (let i = 0; i < 5000; i += 1) {
			lines.push(i % 7 === 0 ? `## Section ${i}` : `- item ${i} with **bold** and \`code\``);
		}
		return lines.join('\n');
	}

	it('builds a viewport and scans the whole note well inside a frame budget', () => {
		const state = stateFrom(bigNote(), MD);
		expect(state.doc.lines).toBeGreaterThan(5000);
		// What one keystroke actually costs: the visible ~50 lines, plus the
		// metadata walk of the whole document.
		const viewport = [{ from: state.doc.line(2500).from, to: state.doc.line(2550).to }];
		const start = performance.now();
		buildLiveDecorations(state, viewport);
		scanMetadata(state);
		expect(performance.now() - start).toBeLessThan(50);
	});

	it('a body keystroke does not need the document walk at all', () => {
		const state = stateFrom(bigNote());
		const scan = scanMetadata(state);
		const tr = state.update({ changes: { from: state.doc.line(4000).to, insert: '!' } });
		expect(scanSurvives(scan, tr.startState.doc, tr.state.doc, tr.changes)).toBe(true);
	});
});

/** Apply a toggle at `pos` and read the document back. */
function toggled(doc: string, pos: number): string {
	const state = stateFrom(doc);
	const change = toggleTaskAt(state, pos);
	return change ? state.update({ changes: change as ChangeSpec }).state.doc.toString() : doc;
}

describe('toggleTaskAt — hand-typed checkboxes', () => {
	it('completes an open task', () => {
		expect(toggled('- [ ] wash up', 0)).toBe('- [x] wash up');
	});

	it('reopens a completed one', () => {
		expect(toggled('- [x] wash up', 0)).toBe('- [ ] wash up');
	});

	it('works from any position on the line, and on an indented item', () => {
		const doc = 'notes\n  * [ ] nested';
		expect(toggled(doc, doc.length)).toBe('notes\n  * [x] nested');
	});

	it('touches only its own line', () => {
		const doc = '- [ ] one\n- [ ] two';
		expect(toggled(doc, doc.indexOf('two'))).toBe('- [ ] one\n- [x] two');
	});

	it('declines a line that is not a task', () => {
		expect(toggleTaskAt(stateFrom('- just a bullet'), 0)).toBeNull();
		expect(toggleTaskAt(stateFrom('prose [ ] not a task'), 0)).toBeNull();
	});

	it('declines the in-progress marker — it has no plain-markdown meaning', () => {
		expect(toggleTaskAt(stateFrom('- [/] doing'), 0)).toBeNull();
	});
});

/** The inline-media widgets of a marked document, all of it visible. */
function rich(marked: string, extensions: Extension[] = MD): [number, number][] {
	const state = stateFrom(marked, extensions);
	return spans(buildRichDecorations(state, [{ from: 0, to: state.doc.length }]));
}

describe('inline media and math stay out of code', () => {
	it('reads `$` in a fenced block as code, not math', () => {
		// The same line outside a fence IS math (`HOME:`) — which is what the
		// fence used to render too.
		expect(rich('echo $HOME:$PATH\n\n|')).toHaveLength(1);
		expect(rich('```bash\necho $HOME:$PATH\n```\n\n|')).toEqual([]);
	});

	it('reads image syntax in inline code as code', () => {
		expect(rich('![alt](x.png)\n\n|')).toEqual([[0, 13]]);
		expect(rich('`![alt](x.png)`\n\n|')).toEqual([]);
	});

	it('reads `$…$` in inline code as code', () => {
		expect(rich('`$a$`\n\n|')).toEqual([]);
	});

	it('still renders math that starts right where inline code ends', () => {
		expect(rich('`x`$a$\n\n|')).toEqual([[3, 6]]);
	});

	it('still leaves prices alone', () => {
		expect(rich('$5 and $10\n\n|')).toEqual([]);
	});
});

describe('checkboxes stay out of code', () => {
	const whole = (state: EditorState) => [{ from: 0, to: state.doc.length }];

	it('draws no checkbox for a `- [ ]` inside a fence, and will not toggle one', () => {
		const state = stateFrom('```\n- [ ] not a task\n```\n\n|', MD);
		expect(spans(buildTaskControls(state, whole(state), () => {}))).toEqual([]);
		expect(toggleTaskAt(state, state.doc.line(2).from)).toBeNull();
	});

	it('still draws and toggles a real one', () => {
		const state = stateFrom('- [ ] a task\n\n|', MD);
		expect(spans(buildTaskControls(state, whole(state), () => {}))).toEqual([[2, 5]]);
		expect(toggleTaskAt(state, 0)).toEqual({ from: 3, to: 4, insert: 'x' });
	});
});

const TWO_BLOCKS = [
	'$$', // 1
	'a^2 + b^2', // 2
	'= c^2', // 3
	'$$', // 4
	'Prose between the blocks, with $x$ inline.', // 5
	'$$', // 6
	'\\int_0^1 f', // 7
	'$$', // 8
	'', // 9
	'end' // 10
].join('\n');

describe('scanDisplayMath — multi-line `$$` blocks, over the whole document', () => {
	it('finds each block as whole lines, with its TeX', () => {
		const doc = stateFrom(TWO_BLOCKS).doc;
		expect(scanDisplayMath(doc)).toEqual([
			{ from: doc.line(1).from, to: doc.line(4).to, tex: 'a^2 + b^2\n= c^2' },
			{ from: doc.line(6).from, to: doc.line(8).to, tex: '\\int_0^1 f' }
		]);
	});

	it('leaves a one-line `$$x$$` to the plugin', () => {
		expect(scanDisplayMath(stateFrom('$$x$$').doc)).toEqual([]);
		expect(rich('$$x$$\n\n|')).toEqual([[0, 5]]);
	});

	it('reads `$$` inside a fence as code', () => {
		expect(scanDisplayMath(stateFrom('```\n$$\nx\n$$\n```').doc)).toEqual([]);
		// …and starts looking again once the fence closes.
		expect(scanDisplayMath(stateFrom('~~~\n$$\n~~~\n$$\ny\n$$').doc)).toHaveLength(1);
	});

	it('does not let a mid-line `$$` pair up and swallow the rest of the note', () => {
		expect(scanDisplayMath(stateFrom('$$\nx $$ and prose\nmore\n$$').doc)).toEqual([]);
	});
});

describe('displayMathField — display math as a legal block', () => {
	const WITH_FIELD = [...MD, displayMathField];

	it('never lets a plugin decoration cross a line break, wherever the viewport starts', () => {
		// With and without the field: the plugin matches a line at a time.
		for (const extensions of [MD, WITH_FIELD]) {
			const state = stateFrom(`${TWO_BLOCKS}|`, extensions);
			const doc = state.doc;
			// A scroll that leaves the viewport starting inside block one used to
			// pair its closer with block two's opener — across the prose.
			const ranges = [{ from: doc.line(3).from, to: doc.length }];
			const found = spans(buildLiveDecorations(state, ranges).decorations);
			for (const [from, to] of found) {
				expect(doc.lineAt(from).number).toBe(doc.lineAt(to).number);
			}
			// …and ate the prose's own inline math on the way.
			const x = doc.line(5).from + doc.line(5).text.indexOf('$x$');
			expect(spans(buildRichDecorations(state, ranges))).toContainEqual([x, x + 3]);
		}
		expect(stateFrom(TWO_BLOCKS, WITH_FIELD).field(displayMathField).blocks).toHaveLength(2);
	});

	it('leaves the lines of a block to the field', () => {
		// Inline math inside a display block would nest a replacement in it.
		const inside = '$$\n\\text{$a$}\n$$\n\n|';
		expect(rich(inside)).toHaveLength(1);
		expect(rich(inside, WITH_FIELD)).toEqual([]);
	});

	/** The rendered blocks as `[firstLine, lastLine]`, caret at `at`. */
	function renderedBlocks(at: (doc: Text) => number) {
		const base = stateFrom(TWO_BLOCKS, WITH_FIELD);
		const state = base.update({ selection: EditorSelection.cursor(at(base.doc)) }).state;
		return spans(state.field(displayMathField).decorations).map(([from, to]) => [
			state.doc.lineAt(from).number,
			state.doc.lineAt(to).number
		]);
	}

	it('renders every block the selection is not in, as a block over whole lines', () => {
		expect(renderedBlocks((doc) => doc.length)).toEqual([
			[1, 4],
			[6, 8]
		]);
		const state = stateFrom(`${TWO_BLOCKS}|`, WITH_FIELD);
		const decorations = state.field(displayMathField).decorations;
		for (const iter = decorations.iter(); iter.value; iter.next()) {
			expect(iter.value.spec.block).toBe(true);
			expect(iter.from).toBe(state.doc.lineAt(iter.from).from);
			expect(iter.to).toBe(state.doc.lineAt(iter.to).to);
		}
	});

	it('reveals a block when the caret is on any of its lines, the middle included', () => {
		expect(renderedBlocks((doc) => doc.line(7).from + 2)).toEqual([[1, 4]]);
		expect(renderedBlocks((doc) => doc.line(3).from)).toEqual([[6, 8]]);
		expect(renderedBlocks((doc) => doc.line(6).from)).toEqual([[1, 4]]);
		expect(renderedBlocks((doc) => doc.line(8).to)).toEqual([[1, 4]]);
		// The prose between them opens neither.
		expect(renderedBlocks((doc) => doc.line(5).from + 3)).toEqual([
			[1, 4],
			[6, 8]
		]);
	});

	it('shows an empty block as its source', () => {
		const state = stateFrom('$$\n\n$$\n\n|', WITH_FIELD);
		expect(state.field(displayMathField).blocks).toHaveLength(1);
		expect(spans(state.field(displayMathField).decorations)).toEqual([]);
	});

	it('returns the same value when a caret move opens or closes nothing', () => {
		const state = stateFrom(`${TWO_BLOCKS}|`, WITH_FIELD);
		const moved = state.update({ selection: EditorSelection.cursor(state.doc.line(9).from) }).state;
		expect(moved.field(displayMathField)).toBe(state.field(displayMathField));
		// A note without display math never rebuilds anything, typing included.
		const plain = stateFrom('Just prose.|', WITH_FIELD);
		const typed = plain.update({ changes: { from: plain.doc.length, insert: ' More.' } }).state;
		expect(typed.field(displayMathField)).toBe(plain.field(displayMathField));
	});

	/** Edit a note under the field; its cached blocks must be what a fresh walk
	 *  finds. Returns whether the edit was allowed to skip that walk. */
	function edit(change: (line: (n: number) => { from: number; to: number }) => ChangeSpec) {
		const state = stateFrom(TWO_BLOCKS, [displayMathField]);
		const tr = state.update({ changes: change((n) => state.doc.line(n)) });
		expect(tr.state.field(displayMathField).blocks).toEqual(scanDisplayMath(tr.state.doc));
		return {
			skipped: displayMathSurvives(tr.startState.doc, tr.state.doc, tr.changes),
			blocks: tr.state.field(displayMathField).blocks
		};
	}

	it('carries its blocks through ordinary typing without walking the note', () => {
		expect(edit((line) => ({ from: line(10).to, insert: ' words' })).skipped).toBe(true);
		expect(edit((line) => ({ from: line(9).from, insert: 'A new\nparagraph' })).skipped).toBe(true);
		// Inside a block: still a block, with the TeX read again.
		const inside = edit((line) => ({ from: line(7).to, insert: ' dx' }));
		expect(inside.skipped).toBe(true);
		expect(inside.blocks[1].tex).toBe('\\int_0^1 f dx');
		expect(edit((line) => ({ from: line(2).to, insert: '\n+ d^2' })).skipped).toBe(true);
	});

	it('walks the note again when a `$` line or a fence changes', () => {
		// A delimiter goes: block one's opener now pairs with block two's.
		expect(edit((line) => ({ from: line(4).from, to: line(4).to })).skipped).toBe(false);
		// A fence opens above everything: no display math left at all.
		const fenced = edit((line) => ({ from: line(1).from, insert: '```\n' }));
		expect(fenced.skipped).toBe(false);
		expect(fenced.blocks).toEqual([]);
		// A prose line gains a `$`, or a delimiter line is joined onto one.
		expect(edit((line) => ({ from: line(10).to, insert: ' $5' })).skipped).toBe(false);
		expect(edit((line) => ({ from: line(4).to, to: line(5).from })).skipped).toBe(false);
	});
});

describe('setext headings are disabled', () => {
	// The editor passes {remove: ['SetextHeading']} to markdown() — a lone `-`
	// typed under a paragraph (the first keystroke of a new bullet) must not
	// re-parse the paragraph above as a heading and flash it large.
	it('a lone dash under text stays plain', () => {
		const state = stateFrom('some text\n-|', [
			markdown({ base: markdownLanguage, extensions: [{ remove: ['SetextHeading'] }] })
		]);
		const names: string[] = [];
		syntaxTree(state).iterate({ enter: (n) => void names.push(n.name) });
		expect(names.some((n) => n.startsWith('SetextHeading'))).toBe(false);
	});
});

describe('images are one object', () => {
	it('stays a picture on the caret’s line', () => {
		expect(rich('![a](pic.png)|')).toEqual([[0, 13]]);
		expect(rich('Look: ![a](pic.png) |here')).toEqual([[6, 19]]);
	});

	it('still reveals math on the caret’s line', () => {
		expect(rich('$x$ |')).toEqual([]);
	});

	it('reads a selection that is exactly one image', () => {
		const state = stateFrom('Before\n![cat](/api/attachments/ab.png)\nAfter', MD).update({
			selection: EditorSelection.single(7, 38)
		}).state;
		expect(selectedImage(state)).toEqual({
			from: 7,
			to: 38,
			alt: 'cat',
			url: '/api/attachments/ab.png'
		});
	});

	it('is not fooled by a selection around or inside an image', () => {
		const base = stateFrom('A ![cat](x.png) B', MD);
		for (const [from, to] of [
			[0, 17],
			[3, 8],
			[2, 2]
		]) {
			expect(selectedImage(base.update({ selection: EditorSelection.single(from, to) }).state)).toBeNull();
		}
	});
});

describe('removeImage', () => {
	/** Remove the image the two `|` select, and return the result in marked form. */
	function remove(marked: string): string {
		let state = stateFrom(marked);
		const { from, to } = state.selection.main;
		const view = {
			get state() {
				return state;
			},
			dispatch: (spec: Parameters<EditorState['update']>[0]) => {
				state = state.update(spec).state;
			}
		} as unknown as EditorView;
		removeImage(view, { from, to });
		return markDoc(state);
	}

	it('takes the line of a picture that had it to itself', () => {
		expect(remove('One\n|![a](x.png)|\nTwo')).toBe('One\n|Two');
	});

	it('takes the newline before a picture on the last line', () => {
		expect(remove('One\n|![a](x.png)|')).toBe('One|');
	});

	it('leaves the text around a picture inside a line', () => {
		expect(remove('See |![a](x.png)| here')).toBe('See | here');
	});
});
