// The formatted view decides three things per line: what to hide, what the
// caret may enter, and what a checkbox click writes. All three are pure
// functions of an EditorState here, so they test without a DOM.
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { syntaxTree } from '@codemirror/language';
import { EditorSelection, type ChangeSpec } from '@codemirror/state';
import type { DecorationSet } from '@codemirror/view';
import { describe, expect, it } from 'vitest';
import {
	buildFormatDecorations,
	buildLiveDecorations,
	buildMetadataDecorations,
	buildRichDecorations,
	contiguousRuns,
	hiddenMetadataLines,
	scanMetadata,
	scanSurvives,
	toggleTaskAt
} from './formatmode';
import { stateFrom } from './testing/cm';

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
		expect(spans(decorations)).toEqual([[0, 1]]);
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
		expect(spans(decorations)).toEqual([[0, 2]]);
		expect(spans(atomic)).toEqual([]);
	});

	it('reveals markers on the line the selection touches', () => {
		expect(spans(formatMarked('## Ti|tle').decorations)).toEqual([]);
	});

	it('keeps the bullet on the selected line — bullets never reveal', () => {
		const { decorations, atomic } = formatMarked('- on|e');
		expect(spans(decorations)).toEqual([[0, 1]]);
		expect(spans(atomic)).toEqual([[0, 1]]);
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

	it('keeps the atomic set to the markers — media widgets stay clickable', () => {
		const { live, markers } = merged('![a](pic.png)\n\n## Title');
		expect(spans(live.atomic)).toEqual(spans(markers.atomic));
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
