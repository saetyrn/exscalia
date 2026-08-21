// Input rules: the two or three characters that everyone expects an editor to
// tidy up as they are typed. `[] ` becomes a checkbox, `-- ` an em dash,
// `...` an ellipsis.
//
// These live on `EditorView.inputHandler`, NOT on a transaction filter. A
// filter sees every change reaching the document — the server diffs
// `replaceDoc` applies, OMNIA's edits, an autocorrect swap — and would rewrite
// text nobody typed. The input handler only ever fires for a real keystroke
// (or a real IME insertion), which is exactly the scope these rules want.

import { syntaxTree } from '@codemirror/language';
import { isolateHistory } from '@codemirror/commands';
import {
	EditorSelection,
	type EditorState,
	type Extension,
	type TransactionSpec
} from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import type { SyntaxNode } from '@lezer/common';

/** What a rule asks for: one replacement, in coordinates of the document as it
 *  stands BEFORE the typed character is inserted.
 *
 *  `to` is always the handler's `to` (the caret), and `insert` already includes
 *  whatever the typed character contributes — a rule describes the finished
 *  line, not a patch on top of the keystroke. */
export interface InputRuleChange {
	from: number;
	to: number;
	insert: string;
}

/** A rule is a pure function of the state, the range the input replaces and the
 *  text being inserted. Null means "not mine". Pure so every rule can be table
 *  tested without a DOM. */
export type InputRule = (
	state: EditorState,
	from: number,
	to: number,
	insert: string
) => InputRuleChange | null;

/** Nodes whose contents are literal text. Inside them `...` is three dots and
 *  `--` is two dashes, because that is what the author wrote. */
const CODE_NODES = new Set(['FencedCode', 'CodeBlock', 'CodeText', 'InlineCode', 'CodeMark']);

/** Whether `pos` sits inside code.
 *
 *  The viewport-parsed tree is enough here: every rule looks only at the line
 *  the caret is on, and the line the caret is on is always parsed. Forcing a
 *  parse (`ensureSyntaxTree`) on a keystroke would trade a wrong answer nobody
 *  can reach for a stall everybody can feel. */
function inCode(state: EditorState, pos: number): boolean {
	for (let node: SyntaxNode | null = syntaxTree(state).resolveInner(pos, -1); node; node = node.parent) {
		if (CODE_NODES.has(node.name)) return true;
	}
	return false;
}

/** The text of the current line up to `pos` — every rule matches against this. */
function textBefore(state: EditorState, pos: number): { start: number; text: string } {
	const line = state.doc.lineAt(pos);
	return { start: line.from, text: line.text.slice(0, pos - line.from) };
}

/** `- []` / `- [ ]` at the head of a list item's content. */
const BOX_ON_LIST = /^([ \t]*[-+*][ \t]+)\[ ?\]$/;
/** The same box with no list marker in front of it yet. */
const BOX_AT_LINE_START = /^([ \t]*)\[ ?\]$/;

/** A checkbox, from either half-written form. `[]` or `[ ]` followed by a space
 *  becomes a normalized `- [ ] `; on a line that is not a list item yet, the
 *  marker comes along too, because a checkbox without one is not a task. */
export const taskBoxRule: InputRule = (state, from, to, insert) => {
	if (insert !== ' ') return null;
	const { start, text } = textBefore(state, from);
	const onList = BOX_ON_LIST.exec(text);
	const match = onList ?? BOX_AT_LINE_START.exec(text);
	if (!match) return null;
	if (inCode(state, from)) return null;
	return { from: start + match[1].length, to, insert: onList ? '[ ] ' : '- [ ] ' };
};

/** `--` then a space becomes an em dash. A third dash means the author is
 *  drawing a rule or opening frontmatter, so `---` never converts. */
export const emDashRule: InputRule = (state, from, to, insert) => {
	if (insert !== ' ') return null;
	const { text } = textBefore(state, from);
	if (!text.endsWith('--') || text.endsWith('---')) return null;
	if (inCode(state, from)) return null;
	return { from: from - 2, to, insert: '— ' };
};

/** `...` becomes an ellipsis on the third dot.
 *
 *  The alternative — waiting for the character after the dots — was rejected:
 *  it leaves `...` sitting in the document for an unpredictable stretch and
 *  then rewrites text the author has already stopped looking at. Firing on the
 *  completing character is what the other two rules do, and it keeps the undo
 *  story simple: Mod-Z immediately after gives back the three dots. A fourth
 *  dot is left alone (`….`), so `....` is still typeable. */
export const ellipsisRule: InputRule = (state, from, to, insert) => {
	if (insert !== '.') return null;
	const { text } = textBefore(state, from);
	if (!text.endsWith('..') || text.endsWith('...')) return null;
	if (inCode(state, from)) return null;
	return { from: from - 2, to, insert: '…' };
};

/** Every rule, in the order they are offered the input. They key off different
 *  characters, so the order is documentation rather than policy. */
export const INPUT_RULES: readonly InputRule[] = [taskBoxRule, emDashRule, ellipsisRule];

// Same test the wrap handler and `closeBrackets` use: on Android the
// composition flags lie until the composition ends.
const android = typeof navigator === 'object' && /Android\b/.test(navigator.userAgent);

/** The slice of an EditorView these rules touch — enough to run them against a
 *  stub in a test, with no DOM anywhere. */
export interface InputTarget {
	state: EditorState;
	composing: boolean;
	compositionStarted: boolean;
	dispatch(spec: TransactionSpec): void;
}

/** Offer one keystroke to the rules; true when one of them claimed it.
 *
 *  A claimed keystroke dispatches TWO transactions: first the character exactly
 *  as CodeMirror would have inserted it, then the expansion that rewrites it,
 *  isolated in the history. That is what makes a single Mod-Z undo the
 *  expansion and leave the literal `...` / `--` / `[]` the author typed, rather
 *  than swallowing both. */
export function applyInputRules(
	view: InputTarget,
	from: number,
	to: number,
	insert: string
): boolean {
	if ((android ? view.composing : view.compositionStarted) || view.state.readOnly) return false;
	if (insert.length !== 1) return false;
	const selection = view.state.selection;
	// One plain caret only. A non-empty selection belongs to the wrap handler
	// above this one, and a multi-range edit is not what these rules describe.
	if (selection.ranges.length !== 1 || !selection.main.empty) return false;
	if (from !== selection.main.from || to !== selection.main.to) return false;

	for (const rule of INPUT_RULES) {
		const change = rule(view.state, from, to, insert);
		if (!change) continue;
		view.dispatch({
			changes: { from, to, insert },
			selection: EditorSelection.cursor(to + insert.length),
			userEvent: 'input.type',
			scrollIntoView: true
		});
		view.dispatch({
			// The rule spoke in pre-input coordinates; the character just landed
			// at `to`, so only the end of the replaced range moves.
			changes: { from: change.from, to: change.to + insert.length, insert: change.insert },
			selection: EditorSelection.cursor(change.from + change.insert.length),
			userEvent: 'input.type',
			annotations: isolateHistory.of('before'),
			scrollIntoView: true
		});
		return true;
	}
	return false;
}

/** Typing rules for the editor.
 *
 *  Default precedence, deliberately: the wrap-the-selection handler in
 *  editorkeys.ts sits at `Prec.high` and must keep first refusal on a keystroke
 *  that lands on a selection. (These rules decline a non-empty selection
 *  anyway, so the two can never both claim one.) */
export const inputRules: Extension = EditorView.inputHandler.of((view, from, to, insert) =>
	applyInputRules(view, from, to, insert)
);
