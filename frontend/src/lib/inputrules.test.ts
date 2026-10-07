// The rules are pure functions of a state, so they table-test directly; the
// handler that composes them needs nothing but a `{state, dispatch}` stub, the
// same headless pattern the rest of the editor tests use.
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { history, isolateHistory, undo } from '@codemirror/commands';
import { EditorState, type Extension, type Transaction, type TransactionSpec } from '@codemirror/state';
import { describe, expect, it } from 'vitest';
import {
	applyInputRules,
	ellipsisRule,
	emDashRule,
	taskBoxRule,
	type InputRule,
	type InputTarget
} from './inputrules';
import { markDoc, stateFrom } from './testing/cm';

const MD: Extension[] = [markdown({ base: markdownLanguage })];

/** Run one rule against a marked document and report the document it asks for,
 *  with the caret where it would leave it. `null` when the rule declines. */
function fire(rule: InputRule, marked: string, insert: string, extensions = MD): string | null {
	const state = stateFrom(marked, extensions);
	const { from, to } = state.selection.main;
	const change = rule(state, from, to, insert);
	if (!change) return null;
	// Same mapping the handler does: the typed character has landed at `to`.
	const next = state.update({
		changes: { from, to, insert },
		sequential: true
	});
	const applied = next.state.update({
		changes: { from: change.from, to: change.to + insert.length, insert: change.insert },
		selection: { anchor: change.from + change.insert.length }
	});
	return markDoc(applied.state);
}

describe('taskBoxRule', () => {
	it('normalizes `[]` at the head of a list item', () => {
		expect(fire(taskBoxRule, '- []|', ' ')).toBe('- [ ] |');
	});

	it('accepts the already-spaced `[ ]` form', () => {
		expect(fire(taskBoxRule, '- [ ]|', ' ')).toBe('- [ ] |');
	});

	it('adds the missing list marker at line start', () => {
		expect(fire(taskBoxRule, '[]|', ' ')).toBe('- [ ] |');
	});

	it('keeps the indentation of a nested line, marker and all', () => {
		expect(fire(taskBoxRule, 'a\n  []|', ' ')).toBe('a\n  - [ ] |');
	});

	it('works under `*` and `+` markers', () => {
		expect(fire(taskBoxRule, '* []|', ' ')).toBe('* [ ] |');
		expect(fire(taskBoxRule, '+ [ ]|', ' ')).toBe('+ [ ] |');
	});

	it('declines a box that is not at the head of the content', () => {
		expect(fire(taskBoxRule, '- milk []|', ' ')).toBeNull();
		expect(fire(taskBoxRule, 'prose []|', ' ')).toBeNull();
	});

	it('declines any character but a space', () => {
		expect(fire(taskBoxRule, '- []|', 'x')).toBeNull();
	});

	it('is inert inside a fenced code block', () => {
		expect(fire(taskBoxRule, '```\n- []|\n```', ' ')).toBeNull();
	});
});

describe('emDashRule', () => {
	it('turns `--` plus a space into an em dash', () => {
		expect(fire(emDashRule, 'a --|', ' ')).toBe('a — |');
	});

	it('leaves a third dash alone — that is a rule or a frontmatter fence', () => {
		expect(fire(emDashRule, '---|', ' ')).toBeNull();
	});

	it('declines a single dash', () => {
		expect(fire(emDashRule, 'a -|', ' ')).toBeNull();
	});

	it('is inert inside inline code', () => {
		expect(fire(emDashRule, 'a `x --| y` b', ' ')).toBeNull();
	});

	it('is inert inside a fenced code block', () => {
		expect(fire(emDashRule, '```\nx --|\n```', ' ')).toBeNull();
	});
});

describe('ellipsisRule', () => {
	it('fires on the third dot', () => {
		expect(fire(ellipsisRule, 'wait..|', '.')).toBe('wait…|');
	});

	it('leaves a fourth dot alone, so `....` stays typeable', () => {
		expect(fire(ellipsisRule, 'wait...|', '.')).toBeNull();
	});

	it('declines a second dot', () => {
		expect(fire(ellipsisRule, 'wait.|', '.')).toBeNull();
	});

	it('is inert inside inline code', () => {
		expect(fire(ellipsisRule, 'a `x ..| y` b', '.')).toBeNull();
	});
});

/** A stand-in for the EditorView the handler runs against: it keeps the state
 *  moving between dispatches, exactly as the real view does. */
function target(marked: string, extensions: Extension[], composing = false) {
	const state = stateFrom(marked, extensions);
	const transactions: Transaction[] = [];
	const view: InputTarget & { transactions: Transaction[] } = {
		state,
		composing,
		compositionStarted: composing,
		transactions,
		dispatch(spec: TransactionSpec) {
			const tr = view.state.update(spec);
			transactions.push(tr);
			view.state = tr.state;
		}
	};
	return view;
}

/** Type one character through the whole handler. */
function type(marked: string, insert: string, extensions: Extension[] = MD) {
	const view = target(marked, extensions);
	const { from, to } = view.state.selection.main;
	const handled = applyInputRules(view, from, to, insert);
	return { handled, doc: markDoc(view.state), transactions: view.transactions };
}

describe('applyInputRules — the handler', () => {
	it('claims a keystroke a rule matches and leaves the finished text', () => {
		expect(type('- []|', ' ')).toMatchObject({ handled: true, doc: '- [ ] |' });
		expect(type('a --|', ' ')).toMatchObject({ handled: true, doc: 'a — |' });
		expect(type('wait..|', '.')).toMatchObject({ handled: true, doc: 'wait…|' });
	});

	it('declines ordinary typing, so CodeMirror inserts it itself', () => {
		const run = type('hello|', 'x');
		expect(run.handled).toBe(false);
		expect(run.transactions).toHaveLength(0);
	});

	it('declines while an IME composition is in flight', () => {
		const view = target('wait..|', MD, true);
		const { from, to } = view.state.selection.main;
		expect(applyInputRules(view, from, to, '.')).toBe(false);
	});

	it('declines a non-empty selection — that keystroke belongs to the wrap handler', () => {
		const view = target('wait|..|', MD);
		const { from, to } = view.state.selection.main;
		expect(applyInputRules(view, from, to, '.')).toBe(false);
	});

	it('declines a read-only document', () => {
		const view = target('wait..|', [...MD, EditorState.readOnly.of(true)]);
		const { from, to } = view.state.selection.main;
		expect(applyInputRules(view, from, to, '.')).toBe(false);
	});

	it('types the character first, then rewrites it in an isolated history event', () => {
		const { transactions } = type('wait..|', '.');
		expect(transactions).toHaveLength(2);
		expect(transactions[0].state.doc.toString()).toBe('wait...');
		expect(transactions[0].annotation(isolateHistory)).toBeUndefined();
		expect(transactions[1].annotation(isolateHistory)).toBe('before');
		for (const tr of transactions) expect(tr.isUserEvent('input.type')).toBe(true);
	});

	it('gives the typed characters back on one undo', () => {
		const view = target('wait..|', [...MD, history()]);
		const { from, to } = view.state.selection.main;
		applyInputRules(view, from, to, '.');
		expect(view.state.doc.toString()).toBe('wait…');
		undo({ state: view.state, dispatch: (tr) => (view.state = tr.state) });
		expect(view.state.doc.toString()).toBe('wait...');
	});
});
