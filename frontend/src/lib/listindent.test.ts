// Tab in a list has to nest by the marker's own width, carry the item's
// children with it, and leave both ordered levels correctly numbered — none of
// which a generic indent command does.
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { indentMore, insertTab } from '@codemirror/commands';
import { EditorSelection, EditorState } from '@codemirror/state';
import { describe, expect, it } from 'vitest';
import { indentListItem, listIndentConfig } from './listindent';
import { applyCommand, runCommand, stateFrom } from './testing/cm';

const EXTENSIONS = [markdown({ base: markdownLanguage }), listIndentConfig];

const nest = (doc: string) => runCommand(doc, indentListItem(1), EXTENSIONS);
const lift = (doc: string) => runCommand(doc, indentListItem(-1), EXTENSIONS);
const nestHandled = (doc: string) => applyCommand(doc, indentListItem(1), EXTENSIONS).handled;
const liftHandled = (doc: string) => applyCommand(doc, indentListItem(-1), EXTENSIONS).handled;

describe('indentListItem — nesting', () => {
	it('nests a bullet by two columns', () => {
		expect(nest('- one\n- two|')).toBe('- one\n  - two|');
	});

	it('nests an ordered item by three, the width of its own marker', () => {
		expect(nest('1. one\n2. two|')).toBe('1. one\n   1. two|');
	});

	it('nests a task item by two — the checkbox is content, not marker', () => {
		expect(nest('- [ ] one\n- [ ] two|')).toBe('- [ ] one\n  - [ ] two|');
	});

	it('nests from a caret sitting at column zero, keeping it on the marker', () => {
		expect(nest('- one\n|- two')).toBe('- one\n  |- two');
	});

	it('refuses the first item of a list — CommonMark cannot express it', () => {
		expect(nestHandled('- one|\n- two')).toBe(false);
		expect(nestHandled('1. one|\n2. two')).toBe(false);
	});

	it('refuses a nested first item too', () => {
		expect(nestHandled('- one\n  - a|\n  - b')).toBe(false);
	});

	it('carries continuation lines and nested children along', () => {
		expect(nest('- one\n- two|\n  wrapped\n  - a\n    - deep')).toBe(
			'- one\n  - two|\n    wrapped\n    - a\n      - deep'
		);
	});

	it('leaves prose alone so the Tab chain can fall through', () => {
		expect(nestHandled('hello wo|rld')).toBe(false);
		expect(nestHandled('# heading|')).toBe(false);
	});

	it('leaves multiple cursors to the generic indent commands', () => {
		const state = stateFrom('- one\n- two\n- three', [
			...EXTENSIONS,
			EditorState.allowMultipleSelections.of(true)
		]).update({
			selection: EditorSelection.create([
				EditorSelection.cursor(11),
				EditorSelection.cursor(18)
			])
		}).state;
		let dispatched = false;
		expect(indentListItem(1)({ state, dispatch: () => (dispatched = true) })).toBe(false);
		expect(dispatched).toBe(false);
	});
});

describe('indentListItem — lifting', () => {
	it('lifts a nested bullet back to the root column', () => {
		expect(lift('- one\n  - a|')).toBe('- one\n- a|');
	});

	it('lifts one level at a time', () => {
		expect(lift('- one\n  - a\n    - x|')).toBe('- one\n  - a\n  - x|');
	});

	it('refuses at the outermost level so Shift-Tab falls through to indentLess', () => {
		expect(liftHandled('- one|')).toBe(false);
		expect(liftHandled('plain prose|')).toBe(false);
	});

	it('carries children out with the item', () => {
		expect(lift('- one\n  - a|\n    - deep')).toBe('- one\n- a|\n  - deep');
	});
});

describe('indentListItem — renumbering', () => {
	it('closes the gap at the source level and opens the destination at 1', () => {
		expect(nest('1. one\n2. two|\n3. three')).toBe('1. one\n   1. two|\n2. three');
	});

	it('appends to a nested list that already exists', () => {
		expect(nest('1. one\n   1. a\n2. two|\n3. three')).toBe(
			'1. one\n   1. a\n   2. two|\n2. three'
		);
	});

	it('renumbers both levels when lifting an item out', () => {
		expect(lift('1. one\n   1. a\n   2. b|\n2. two')).toBe('1. one\n   1. a\n2. b|\n3. two');
	});

	it('restarts the orphaned tail that stays behind', () => {
		expect(lift('1. one\n   1. a|\n   2. b')).toBe('1. one\n2. a|\n   1. b');
	});

	it('leaves a hand-numbered list exactly as written', () => {
		// 1/1/1 is a deliberate authoring style; renumbering it would be rude.
		expect(nest('1. one\n1. two|\n1. three')).toBe('1. one\n   1. two|\n1. three');
	});

	it('does not renumber bullets', () => {
		expect(nest('- one\n- two|\n- three')).toBe('- one\n  - two|\n- three');
	});
});

describe('the Tab fallback', () => {
	it('inserts at the caret mid-word instead of indenting the line', () => {
		expect(nestHandled('hel|lo')).toBe(false);
		expect(runCommand('hel|lo', insertTab, EXTENSIONS)).toBe('hel\t|lo');
	});

	it('indents whole lines when a range is selected', () => {
		expect(runCommand('he|llo wor|ld', insertTab, EXTENSIONS)).toBe('  he|llo wor|ld');
		expect(runCommand('he|llo wor|ld', indentMore, EXTENSIONS)).toBe('  he|llo wor|ld');
	});
});
