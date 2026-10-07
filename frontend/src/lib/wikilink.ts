import {
	acceptCompletion,
	autocompletion,
	closeCompletion,
	moveCompletionSelection,
	type Completion,
	type CompletionContext,
	type CompletionResult
} from '@codemirror/autocomplete';
import { Prec, type Extension } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { vault } from './stores.svelte';

/** `[[...` autocomplete fed by the live note list. */
function wikilinkCompletions(ctx: CompletionContext): CompletionResult | null {
	const line = ctx.state.doc.lineAt(ctx.pos);
	const before = line.text.slice(0, ctx.pos - line.from);
	const open = before.lastIndexOf('[[');
	if (open === -1) return null;
	const frag = before.slice(open + 2);
	if (frag.includes(']]') || frag.includes('|') || frag.includes('#')) return null;

	const options = vault.notes
		.filter((n) => !n.path.startsWith('templates/'))
		.map((n) => ({
			label: n.name,
			detail: n.path,
			// The closing `]]` may already be there — `closeBrackets` writes it the
			// moment the second `[` is typed — so the insert is decided against the
			// live document at the moment of acceptance, not against the fragment
			// this source was called with. Either way the caret ends up past the
			// brackets, where the next word goes.
			apply: (view: EditorView, _completion: Completion, from: number, to: number) => {
				const closed = view.state.sliceDoc(to, to + 2) === ']]';
				view.dispatch({
					changes: { from, to, insert: closed ? n.name : `${n.name}]]` },
					selection: { anchor: from + n.name.length + 2 },
					userEvent: 'input.complete',
					scrollIntoView: true
				});
				closeCompletion(view);
			}
		}));
	return {
		from: line.from + open + 2,
		options,
		validFor: /^[^\]|#]*$/
	};
}

/** The `[[` popup, plus the only keys it is allowed to take.
 *
 *  Nothing is preselected: with `selectOnOpen`, Enter accepted the first note
 *  in the list the instant `[[` was typed, and — worse — swallowed Enter for as
 *  long as the popup stood, so list continuation stopped working mid-list. The
 *  user now arrows onto a candidate before Enter means "accept"; until then
 *  every one of these commands reports false and the key falls through to
 *  lang-markdown's `insertNewlineContinueMarkup` and the editor's own keymap. */
export const wikilinkAutocomplete: Extension = [
	autocompletion({
		override: [wikilinkCompletions],
		icons: false,
		defaultKeymap: false,
		selectOnOpen: false
	}),
	Prec.highest(
		keymap.of([
			{ key: 'Tab', run: acceptCompletion },
			{ key: 'Enter', run: acceptCompletion },
			{ key: 'Escape', run: closeCompletion },
			{ key: 'ArrowDown', run: moveCompletionSelection(true) },
			{ key: 'ArrowUp', run: moveCompletionSelection(false) }
		])
	)
];

/** Cmd/Ctrl+click on a `[[target]]` opens it. */
export function wikilinkClick(onOpen: (target: string) => void) {
	return EditorView.domEventHandlers({
		mousedown(e, view) {
			if (!(e.metaKey || e.ctrlKey)) return false;
			const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
			if (pos === null) return false;
			const line = view.state.doc.lineAt(pos);
			const col = pos - line.from;
			const re = /\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]/g;
			let m: RegExpExecArray | null;
			while ((m = re.exec(line.text))) {
				if (col >= m.index && col <= m.index + m[0].length) {
					onOpen(m[1].trim());
					return true;
				}
			}
			return false;
		}
	});
}
