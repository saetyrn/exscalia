// The daily typing keys: headings, task markers, strikethrough, and the
// wrap-on-type handler. All of these are state commands, so they run headless
// against the `|`-marked documents of the cm.ts harness.
import { insertBracket } from '@codemirror/autocomplete';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { describe, expect, it } from 'vitest';
import {
	insertWikilink,
	markdownKeys,
	setHeading,
	toggleBullet,
	toggleStrikethrough,
	toggleTaskMarker,
	wrapSelectionWith
} from './editorkeys';
import { applyCommand, markDoc, runCommand, stateFrom } from './testing/cm';

const MD = [markdown({ base: markdownLanguage })];

describe('setHeading', () => {
	it('sets a heading on a plain line, keeping the caret on the text', () => {
		expect(runCommand('a title|', setHeading(2), MD)).toBe('## a title|');
	});

	it('replaces a heading of a different level rather than stacking hashes', () => {
		expect(runCommand('### deep|', setHeading(1), MD)).toBe('# deep|');
	});

	it('toggles off when the line is already at that exact level', () => {
		expect(runCommand('## same|', setHeading(2), MD)).toBe('same|');
	});

	it('takes every line of a multi-line selection', () => {
		expect(runCommand('|one\ntwo|', setHeading(3), MD)).toBe('|### one\n### two|');
	});

	it('toggles a multi-line selection off only when all of it is at the level', () => {
		expect(runCommand('|## one\n## two|', setHeading(2), MD)).toBe('|one\ntwo|');
		expect(runCommand('|## one\ntwo|', setHeading(2), MD)).toBe('|## one\n## two|');
	});

	it('skips list lines in a multi-line selection', () => {
		expect(runCommand('|intro\n- milk\n1. eggs\noutro|', setHeading(2), MD)).toBe(
			'|## intro\n- milk\n1. eggs\n## outro|'
		);
	});

	it('refuses a selection that is nothing but list lines', () => {
		expect(applyCommand('- milk|', setHeading(2), MD).handled).toBe(false);
		expect(applyCommand('|- milk\n- eggs|', setHeading(2), MD).handled).toBe(false);
	});

	it('makes an empty line a heading', () => {
		expect(runCommand('|', setHeading(1), MD)).toBe('# |');
	});
});

describe('toggleTaskMarker', () => {
	it('cycles a bullet through unchecked, in progress, checked, and back to plain', () => {
		expect(runCommand('- wash up|', toggleTaskMarker, MD)).toBe('- [ ] wash up|');
		expect(runCommand('- [ ] wash up|', toggleTaskMarker, MD)).toBe('- [/] wash up|');
		expect(runCommand('- [/] wash up|', toggleTaskMarker, MD)).toBe('- [x] wash up|');
		expect(runCommand('- [x] wash up|', toggleTaskMarker, MD)).toBe('- wash up|');
	});

	it('advances an in-progress task instead of stacking a second box on it', () => {
		expect(runCommand('- [/] draft the essay|', toggleTaskMarker, MD)).toBe(
			'- [x] draft the essay|'
		);
	});

	it('keeps indentation and the item marker', () => {
		expect(runCommand('  * wash up|', toggleTaskMarker, MD)).toBe('  * [ ] wash up|');
		expect(runCommand('    + [ ] wash up|', toggleTaskMarker, MD)).toBe('    + [/] wash up|');
		expect(runCommand('  * [/] wash up|', toggleTaskMarker, MD)).toBe('  * [x] wash up|');
		expect(runCommand('\t+ [/] wash up|', toggleTaskMarker, MD)).toBe('\t+ [x] wash up|');
		expect(runCommand('  * [x] wash up|', toggleTaskMarker, MD)).toBe('  * wash up|');
	});

	it('treats an uppercase X as checked', () => {
		expect(runCommand('- [X] done|', toggleTaskMarker, MD)).toBe('- done|');
	});

	it('cycles every bullet a selection covers', () => {
		expect(runCommand('|- one\n- two|', toggleTaskMarker, MD)).toBe('|- [ ] one\n- [ ] two|');
	});

	it('advances each line of a mixed selection from its own state', () => {
		expect(runCommand('|- one\n- [ ] two\n- [/] three\n- [x] four|', toggleTaskMarker, MD)).toBe(
			'|- [ ] one\n- [/] two\n- [x] three\n- four|'
		);
	});

	it('refuses lines that are not bullet items', () => {
		expect(applyCommand('plain prose|', toggleTaskMarker, MD).handled).toBe(false);
		expect(applyCommand('1. ordered|', toggleTaskMarker, MD).handled).toBe(false);
		expect(applyCommand('---|', toggleTaskMarker, MD).handled).toBe(false);
	});

	it('is bound to Mod-Enter ahead of anything defaultKeymap would do', () => {
		const binding = markdownKeys.find((key) => key.key === 'Mod-Enter');
		expect(binding?.run).toBe(toggleTaskMarker);
	});
});

describe('toggleStrikethrough', () => {
	it('wraps a selection in the standard markers', () => {
		expect(runCommand('|gone|', toggleStrikethrough, MD)).toBe('~~|gone|~~');
	});

	it('unwraps a selection that already carries them', () => {
		expect(runCommand('|~~gone~~|', toggleStrikethrough, MD)).toBe('|gone|');
	});

	it('unwraps when the markers sit just outside the selection', () => {
		expect(runCommand('~~|gone|~~', toggleStrikethrough, MD)).toBe('|gone|');
	});

	it('is on Mod-Shift-x', () => {
		expect(markdownKeys.find((key) => key.key === 'Mod-Shift-x')?.run).toBe(toggleStrikethrough);
	});
});

describe('wrapSelectionWith', () => {
	it('wraps the selection and keeps it over the inner text', () => {
		expect(runCommand('say |word| now', wrapSelectionWith('*'), MD)).toBe('say *|word|* now');
	});

	it('reaches **word** after two presses', () => {
		const once = applyCommand('|word|', wrapSelectionWith('*'), MD);
		const twice = wrapSelectionWith('*');
		let out = once.state;
		twice({
			state: out,
			dispatch: (tr) => {
				out = tr.state;
			}
		});
		expect(markDoc(out)).toBe('**|word|**');
	});

	it('handles every marker character it claims', () => {
		for (const char of ['*', '_', '~', '"', "'"]) {
			expect(runCommand('|word|', wrapSelectionWith(char), MD)).toBe(`${char}|word|${char}`);
		}
	});

	it('preserves a backwards selection', () => {
		const state = stateFrom('|word|', MD).update({
			selection: { anchor: 4, head: 0 }
		}).state;
		let out = state;
		wrapSelectionWith('*')({
			state,
			dispatch: (tr) => {
				out = tr.state;
			}
		});
		expect(out.doc.toString()).toBe('*word*');
		expect(out.selection.main.anchor).toBe(5);
		expect(out.selection.main.head).toBe(1);
	});

	it('falls through when nothing is selected', () => {
		expect(applyCommand('word|', wrapSelectionWith('*'), MD).handled).toBe(false);
		expect(applyCommand('|', wrapSelectionWith('_'), MD).handled).toBe(false);
	});

	it('groups as ordinary typing so undo folds it in', () => {
		expect(applyCommand('|word|', wrapSelectionWith('~'), MD).transaction?.isUserEvent('input.type')).toBe(
			true
		);
	});
});

// The auto-pair token set is language data, so it can be checked without a
// view: `insertBracket` is the exact function the closeBrackets input handler
// runs, and it reads the same `closeBrackets` language data.
describe('prose auto-pairs', () => {
	const PROSE = [
		...MD,
		markdownLanguage.data.of({
			closeBrackets: { brackets: ['(', '[', '`'], before: ')]}:;>' }
		})
	];
	const type = (doc: string, bracket: string) => {
		const state = stateFrom(doc, PROSE);
		const tr = insertBracket(state, bracket);
		return tr ? markDoc(tr.state) : null;
	};

	it('closes a paren at the end of a line', () => {
		expect(type('see |', '(')).toBe('see (|)');
	});

	it('closes a bracket, so the second [ gives the wikilink its pair', () => {
		expect(type('see |', '[')).toBe('see [|]');
		expect(type('see [|]', '[')).toBe('see [[|]]');
	});

	it('leaves apostrophes alone — the reason quotes are off the token list', () => {
		expect(type('don|', "'")).toBe(null);
		expect(type('|', "'")).toBe(null);
		expect(type('|', '"')).toBe(null);
	});

	it('leaves the emphasis markers to the wrap handler', () => {
		expect(type('|', '*')).toBe(null);
		expect(type('|', '_')).toBe(null);
	});

	it('wraps a selection in the pair', () => {
		expect(type('an |aside| here', '(')).toBe('an (|aside|) here');
	});
});

// --- the soft-keyboard bar's two new commands ------------------------------

describe('toggleBullet', () => {
	it('makes a plain line a bullet, keeping the caret on the text', () => {
		expect(runCommand('milk|', toggleBullet, MD)).toBe('- milk|');
	});

	it('strips the bullet when the line already has one', () => {
		expect(runCommand('- milk|', toggleBullet, MD)).toBe('milk|');
	});

	it('takes the checkbox with the marker it belongs to', () => {
		expect(runCommand('- [ ] milk|', toggleBullet, MD)).toBe('milk|');
		expect(runCommand('- [/] milk|', toggleBullet, MD)).toBe('milk|');
	});

	it('keeps the indentation it found', () => {
		expect(runCommand('    milk|', toggleBullet, MD)).toBe('    - milk|');
	});

	it('converts an ordered item rather than nesting under it', () => {
		expect(runCommand('1. milk|', toggleBullet, MD)).toBe('- milk|');
	});

	it('toggles a whole selection off only when all of it is bulleted', () => {
		expect(runCommand('|- one\n- two|', toggleBullet, MD)).toBe('|one\ntwo|');
		expect(runCommand('|- one\ntwo|', toggleBullet, MD)).toBe('|- one\n- two|');
	});

	it('leaves an already-bulleted line — checkbox included — alone while adding', () => {
		expect(runCommand('|- [x] one\ntwo|', toggleBullet, MD)).toBe('|- [x] one\n- two|');
		expect(runCommand('|- [/] one\ntwo|', toggleBullet, MD)).toBe('|- [/] one\n- two|');
	});

	it('bullets an empty line, which is how the button is used', () => {
		expect(runCommand('|', toggleBullet, MD)).toBe('- |');
	});
});

describe('insertWikilink', () => {
	it('opens an empty pair with the caret where the completion reads', () => {
		expect(runCommand('see |', insertWikilink, MD)).toBe('see [[|]]');
	});

	it('wraps the selection and keeps it selected', () => {
		expect(runCommand('see |Ideas| now', insertWikilink, MD)).toBe('see [[|Ideas|]] now');
	});

	it('reports itself as typing, which is what opens the popup', () => {
		const run = applyCommand('see |', insertWikilink, MD);
		expect(run.transaction?.isUserEvent('input.type')).toBe(true);
	});
});
