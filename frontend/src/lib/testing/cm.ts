// Headless harness for CodeMirror commands.
//
// A `StateCommand` only ever touches `{state, dispatch}`, so it can run with no
// DOM, no EditorView and no jsdom — the same stub pattern autocorrect.test.ts
// uses for its state fields. Documents are written with `|` marking the caret
// (two `|` mark a selection), and results come back in the same notation, so a
// test reads as a before/after pair of literal documents.

import { ensureSyntaxTree } from '@codemirror/language';
import {
	EditorSelection,
	EditorState,
	type Extension,
	type StateCommand,
	type Transaction
} from '@codemirror/state';

/** The caret marker used in harness documents. */
export const CARET = '|';

/** A parsed harness document: the real text plus the selection `|` described. */
export interface MarkedDoc {
	doc: string;
	selection: EditorSelection;
}

/** Strip the `|` markers, returning the text and the selection they describe.
 *  No marker means a caret at the start; one marks a caret; two mark a range. */
export function parseDoc(marked: string): MarkedDoc {
	const marks: number[] = [];
	let doc = '';
	for (const char of marked) {
		if (char === CARET) marks.push(doc.length);
		else doc += char;
	}
	if (marks.length > 2) throw new Error(`Expected at most two "${CARET}" markers, got ${marks.length}`);
	const selection =
		marks.length === 2
			? EditorSelection.single(marks[0], marks[1])
			: EditorSelection.single(marks[0] ?? 0);
	return { doc, selection };
}

/** Re-embed the caret (or selection) markers into a state's document. */
export function markDoc(state: EditorState): string {
	const doc = state.doc.toString();
	const range = state.selection.main;
	if (range.empty) return doc.slice(0, range.head) + CARET + doc.slice(range.head);
	return (
		doc.slice(0, range.from) +
		CARET +
		doc.slice(range.from, range.to) +
		CARET +
		doc.slice(range.to)
	);
}

/** Build a state from a marked document, with the syntax tree already parsed.
 *  Commands that consult `syntaxTree()` would otherwise race the language
 *  field's time-sliced first parse. */
export function stateFrom(marked: string, extensions: Extension[] = []): EditorState {
	const { doc, selection } = parseDoc(marked);
	const state = EditorState.create({ doc, selection, extensions });
	ensureSyntaxTree(state, state.doc.length, 5000);
	return state;
}

export interface CommandRun {
	/** What the command returned — false means "not mine, try the next one". */
	handled: boolean;
	/** The resulting state (the original one when nothing was dispatched). */
	state: EditorState;
	/** The resulting document with the caret/selection markers re-embedded. */
	doc: string;
	/** The dispatched transaction, for asserting on userEvent or annotations. */
	transaction: Transaction | null;
}

/** Run a state command against a marked document and report everything about
 *  the outcome. Commands that dispatch more than once are a bug in the command;
 *  only the last transaction is kept. */
export function applyCommand(
	marked: string,
	command: StateCommand,
	extensions: Extension[] = []
): CommandRun {
	const state = stateFrom(marked, extensions);
	const dispatched: Transaction[] = [];
	const handled = command({ state, dispatch: (tr) => dispatched.push(tr) });
	const last = dispatched[dispatched.length - 1] ?? null;
	const next = last ? last.state : state;
	return { handled, state: next, doc: markDoc(next), transaction: last };
}

/** The common case: run a command and read the resulting document back. */
export function runCommand(
	marked: string,
	command: StateCommand,
	extensions: Extension[] = []
): string {
	return applyCommand(marked, command, extensions).doc;
}
