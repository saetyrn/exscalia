<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import { acceptCompletion, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
	import {
		EditorView,
		dropCursor,
		highlightSpecialChars,
		keymap,
		placeholder as cmPlaceholder,
		type Command
	} from '@codemirror/view';
	import { Compartment, EditorState, type Extension } from '@codemirror/state';
	import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
	import { languages } from '@codemirror/language-data';
	import { diff } from '@codemirror/merge';
	import {
		defaultKeymap,
		history,
		historyKeymap,
		indentLess,
		indentMore,
		redo,
		undo
	} from '@codemirror/commands';
	import { bracketMatching, syntaxHighlighting, HighlightStyle } from '@codemirror/language';
	import { CompositionQueue, settleHeld, type HeldReplacement } from './composequeue';
	import { isProgrammatic, programmaticChange } from './programmatic';
	import { highlightSelectionMatches, search, searchKeymap } from '@codemirror/search';
	import { tags } from '@lezer/highlight';
	import { uploadAttachment } from './api';
	import {
		IMAGE_ACTION_EVENT,
		UPLOADING,
		removeImage,
		selectedImage,
		type ImageActionDetail
	} from './formatmode';
	import { copyImage, downloadImage, pastedCopy } from './noteimages';
	import { modal } from './modal';
	import { fade } from './motion';
	import {
		markdownKeys,
		toggleBold,
		toggleItalic,
		toggleStrikethrough,
		toggleUnderline,
		wrapSelectionOnType
	} from './editorkeys';
	import { indentListItem, listIndentConfig } from './listindent';
	import { inputRules } from './inputrules';
	import { tabAlwaysIndents } from './tabmode';
	import { notify } from './ui.svelte';
	import { omniaContext } from './stores.svelte';

	// One shared empty list, so an omitted prop keeps a stable identity and
	// never reads as a change.
	const NO_EXTENSIONS: Extension[] = [];

	/** True when a finger or stylus is the primary pointer. */
	const coarsePointer =
		typeof window !== 'undefined' && typeof window.matchMedia === 'function'
			? window.matchMedia('(pointer: coarse)').matches
			: false;

	/** Run commands in order and stop at the first one that claims the key. */
	function chain(...commands: Command[]): Command {
		return (editor) => commands.some((command) => command(editor));
	}

	// Prose fallback for Tab: soft spaces, never a literal \t — a tab at the
	// start of a prose line would silently read as an indented code block.
	const insertSoftTab: Command = (view) => {
		if (view.state.selection.ranges.some((r) => !r.empty)) return indentMore(view);
		view.dispatch(view.state.replaceSelection('  '), {
			scrollIntoView: true,
			userEvent: 'input'
		});
		return true;
	};

	let {
		path,
		content,
		onChange,
		onReplacementConflict,
		onTaskCommand,
		onSave,
		extensions = NO_EXTENSIONS,
		dynamicExtensions = NO_EXTENSIONS
	}: {
		path: string;
		content: string;
		/** Every document change. `programmatic` is true for a `replaceDoc`
		 *  applied as requested — the server's text coming back, not an edit. */
		onChange: (doc: string, programmatic: boolean) => void;
		/** A remote `replaceDoc` held for an IME composition overlaps what was
		 *  composed meanwhile, so it will not go in (see `settleHeld`). */
		onReplacementConflict?: () => void;
		onTaskCommand?: (text: string) => boolean | Promise<boolean>;
		/** Mod-S. Saving is automatic, but the key is muscle memory — and it has
		 *  to be caught either way, or the browser opens its Save dialog. */
		onSave?: () => void;
		// Fixed for the life of the view. Anything holding a StateField belongs
		// here: leaving the configuration would throw its state away.
		extensions?: Extension[];
		// Swapped in and out live through a compartment — view plugins and themes
		// only. Changing this must never rebuild the view.
		dynamicExtensions?: Extension[];
	} = $props();

	let host: HTMLDivElement | undefined = $state();
	let view: EditorView | null = null;

	// The formatted-view bundle used to arrive through a `refreshKey` that tore
	// the whole EditorView down and rebuilt it from a serialized state. Scroll
	// position, focus, caret, every view plugin's state and the DOM all died on
	// each ¶ toggle. A compartment swaps the same extensions in place instead.
	const formatCompartment = new Compartment();
	// What the compartment currently holds, so the effect below only dispatches
	// on a real change (and a freshly created view starts already configured).
	let appliedDynamic: Extension[] = [];

	const mdHighlight = HighlightStyle.define([
		{ tag: tags.heading1, fontSize: '1.5em', fontWeight: '700' },
		{ tag: tags.heading2, fontSize: '1.3em', fontWeight: '700' },
		{ tag: tags.heading3, fontSize: '1.15em', fontWeight: '600' },
		{ tag: tags.heading4, fontWeight: '600' },
		{ tag: tags.heading5, fontWeight: '600' },
		{ tag: tags.heading6, fontWeight: '600', color: 'var(--text-muted)' },
		{ tag: tags.strong, fontWeight: '700' },
		{ tag: tags.emphasis, fontStyle: 'italic' },
		{ tag: tags.strikethrough, textDecoration: 'line-through' },
		{ tag: tags.link, color: 'var(--accent)' },
		{ tag: tags.url, color: 'var(--accent)' },
		{ tag: tags.monospace, fontFamily: 'var(--font-mono)', fontSize: '0.9em' },
		{ tag: tags.quote, color: 'var(--text-muted)', fontStyle: 'italic' },
		{ tag: tags.meta, color: 'var(--text-muted)' },
		{ tag: tags.comment, color: 'var(--text-muted)' },
		{ tag: tags.contentSeparator, color: 'var(--text-muted)' }
	]);

	const theme = EditorView.theme({
		'&': {
			height: '100%',
			fontSize: '15px',
			backgroundColor: 'transparent',
			color: 'var(--text)'
		},
		'.cm-content': {
			fontFamily: 'var(--font-ui)',
			maxWidth: '760px',
			margin: '0 auto',
			padding: '24px 32px 50vh',
			caretColor: 'var(--accent)',
			lineHeight: '1.65'
		},
		'.cm-cursor': { borderLeftColor: 'var(--accent)' },
		// Both of these ship hard-coded colors that only exist for CodeMirror's
		// own light/dark themes: a black drop cursor (invisible on a dark
		// background) and a red special-character marker.
		'.cm-dropCursor': { borderLeftColor: 'var(--accent)' },
		'.cm-specialChar': {
			color: 'var(--text-muted)',
			backgroundColor: 'var(--bg-active)',
			borderRadius: '2px',
			fontWeight: 'normal'
		},
		'&.cm-focused': { outline: 'none' },
		// The native selection (no drawSelection layer), in the app palette.
		'.cm-content ::selection, .cm-content::selection': {
			backgroundColor: 'var(--bg-active)'
		},
		'.cm-line::selection, .cm-line ::selection': {
			backgroundColor: 'var(--bg-active)'
		},
		'.cm-activeLine': { backgroundColor: 'transparent' },
		// Stock CodeMirror paints matching brackets teal and mismatched ones red.
		// Both are off-palette; a border-weight tint reads as "this is the other
		// half" without competing with the text.
		'&.cm-focused .cm-matchingBracket': {
			backgroundColor: 'var(--bg-active)',
			outline: 'none'
		},
		'&.cm-focused .cm-nonmatchingBracket': { backgroundColor: 'transparent' },
		'.cm-scroller': { fontFamily: 'var(--font-ui)' },
		// Find/replace panel, restyled from stock CodeMirror to the app's palette.
		'.cm-panels': {
			backgroundColor: 'var(--bg-panel)',
			color: 'var(--text)',
			border: 'none'
		},
		'.cm-panels.cm-panels-top': { borderBottom: '1px solid var(--border)' },
		'.cm-panel.cm-search': { padding: '8px 10px', fontFamily: 'var(--font-ui)' },
		'.cm-panel.cm-search label': { fontSize: '12px', color: 'var(--text-muted)' },
		'.cm-panel.cm-search input, .cm-panel.cm-search button': {
			fontFamily: 'var(--font-ui)',
			fontSize: '13px',
			color: 'var(--text)',
			backgroundColor: 'var(--bg)',
			border: '1px solid var(--border)',
			borderRadius: '6px',
			padding: '3px 8px'
		},
		'.cm-panel.cm-search input:focus': {
			outline: 'none',
			borderColor: 'var(--accent)'
		},
		'.cm-panel.cm-search button:hover': { borderColor: 'var(--accent)' },
		'.cm-panel.cm-search [name="close"]': {
			color: 'var(--text-muted)',
			background: 'none',
			border: 'none',
			fontSize: '18px'
		},
		'.cm-searchMatch': {
			backgroundColor: 'color-mix(in srgb, var(--accent) 24%, transparent)',
			borderRadius: '3px'
		},
		'.cm-searchMatch.cm-searchMatch-selected': {
			backgroundColor: 'color-mix(in srgb, var(--accent) 48%, transparent)'
		},
		'.cm-selectionMatch': {
			backgroundColor: 'color-mix(in srgb, var(--accent) 16%, transparent)'
		}
	});

	// Treat `/tasks something` (and `/ tasks something`) as a transient command
	// line. Enter removes the command from the note and sends its argument to
	// the app's task creator.
	const slashTaskShortcut = EditorView.domEventHandlers({
		keydown(event, editor) {
			if (
				event.key !== 'Enter' ||
				event.isComposing ||
				event.metaKey ||
				event.ctrlKey ||
				event.altKey ||
				event.shiftKey ||
				!onTaskCommand
			)
				return false;
			const selection = editor.state.selection.main;
			if (!selection.empty) return false;
			const line = editor.state.doc.lineAt(selection.head);
			if (editor.state.sliceDoc(selection.head, line.to).trim()) return false;
			const command = /^\s*\/\s*tasks?\s+(.+?)\s*$/i.exec(line.text);
			if (!command) return false;

			event.preventDefault();
			editor.dispatch({
				changes: { from: line.from, to: line.to, insert: '' },
				selection: { anchor: line.from },
				scrollIntoView: true
			});
			queueMicrotask(() => editor.focus());
			void Promise.resolve(onTaskCommand(command[1].trim()))
				.then((created) => {
					if (created) return;
					// If creation failed and the user has not started typing on
					// the cleared line, restore the command so its text is never
					// silently lost.
					const retryPosition = Math.min(line.from, editor.state.doc.length);
					const retryLine = editor.state.doc.lineAt(retryPosition);
					if (retryLine.from !== line.from || retryLine.text !== '') return;
					editor.dispatch({
						changes: { from: retryLine.from, insert: line.text },
						selection: { anchor: retryLine.from + line.text.length },
						scrollIntoView: true
					});
					editor.focus();
				})
				.catch(() => {});
			return true;
		}
	});

	// Server normalization (timestamps and hidden task metadata) can cause a
	// browser to momentarily focus the document body while the user is typing.
	// Recover only that "focus went nowhere" case; a real destination element
	// means the user intentionally clicked or tabbed away.
	// Finishing a pointer selection opens a small B/I/U/S toolbar immediately.
	// Clicking inside an existing selection remains a fallback that reopens it
	// without collapsing the selection. preventDefault is what keeps it alive.
	let selMenu = $state<{ x: number; y: number } | null>(null);

	function showSelectionMenu(x: number, y: number) {
		// A selected picture has its own toolbar; bold does nothing to it.
		if (view && selectedImage(view.state)) return;
		selMenu = {
			x: Math.max(8, Math.min(x, window.innerWidth - 138)),
			y: y < 70 ? y + 24 : y - 46
		};
	}

	const selectionMenu = EditorView.domEventHandlers({
		mousedown(event, editor) {
			if (event.button !== 0 || event.detail > 1) return false;
			if (selMenu) {
				selMenu = null;
				return false;
			}
			const sel = editor.state.selection.main;
			if (sel.empty) return false;
			const pos = editor.posAtCoords({ x: event.clientX, y: event.clientY });
			if (pos === null || pos < sel.from || pos > sel.to) return false;
			event.preventDefault();
			showSelectionMenu(event.clientX, event.clientY);
			return true;
		},
		mouseup(event, editor) {
			if (event.button !== 0) return false;
			const { clientX, clientY } = event;
			// CodeMirror finalizes a drag/double-click selection in its own DOM
			// handler. Run after it so the menu reads the completed range.
			queueMicrotask(() => {
				if (view !== editor) return;
				if (editor.state.selection.main.empty) {
					selMenu = null;
					return;
				}
				showSelectionMenu(clientX, clientY);
			});
			return false;
		},
		// A finger never sends mousedown/mouseup around a selection: the whole
		// range is made by long-pressing and dragging the OS handles, and the
		// only event that reliably marks the end of that is `touchend`.
		touchend(event, editor) {
			if (!coarsePointer) return false;
			queueMicrotask(() => {
				if (view === editor) anchorMenuToSelection();
			});
			return false;
		}
	});

	/** Put the toolbar above the caret end of the selection.
	 *
	 *  Touch gives no usable pointer coordinates for this — the last touch was
	 *  on a drag handle, which sits below the text it adjusts — so the anchor
	 *  comes from the document position instead. */
	function anchorMenuToSelection() {
		const editor = view;
		if (!editor) return;
		const sel = editor.state.selection.main;
		if (sel.empty) {
			selMenu = null;
			return;
		}
		const coords = editor.coordsAtPos(sel.head);
		if (!coords) return;
		showSelectionMenu(coords.left, coords.top);
	}

	// On touch, the selection also changes without any event the editor sees:
	// dragging the iOS/Android handles fires `selectionchange` on the document
	// and nothing else. Debounced, because the handles fire it continuously
	// while they move, and only while this editor holds focus so a selection
	// made in the chat panel or a text field never opens the note's toolbar.
	let selectionChangeTimer: ReturnType<typeof setTimeout> | null = null;

	function onDocumentSelectionChange() {
		if (!view?.hasFocus) return;
		if (selectionChangeTimer !== null) clearTimeout(selectionChangeTimer);
		selectionChangeTimer = setTimeout(() => {
			selectionChangeTimer = null;
			if (!view?.hasFocus) return;
			anchorMenuToSelection();
		}, 250);
	}

	if (coarsePointer && typeof document !== 'undefined')
		document.addEventListener('selectionchange', onDocumentSelectionChange);

	function runFormat(command: (v: EditorView) => boolean) {
		if (view) command(view);
		selMenu = null;
		view?.focus();
	}

	const accidentalBlurGuard = EditorView.domEventHandlers({
		blur(event, editor) {
			const destination = event.relatedTarget;
			if (
				destination instanceof Element &&
				destination !== document.body &&
				destination !== document.documentElement
			)
				return false;
			requestAnimationFrame(() => {
				if (
					view === editor &&
					document.hasFocus() &&
					(document.activeElement === document.body ||
						document.activeElement === document.documentElement)
				) {
					editor.focus();
				}
			});
			return false;
		}
	});

	// --- IME-safe document replacement -------------------------------------

	/** Replacements that arrived mid-composition, applied once the IME lets go.
	 *
	 *  Each carries its `base` — the document it was computed against — beside
	 *  the target text. The composition commits characters while the value
	 *  waits, so on release the document may no longer be `base`; applying the
	 *  target as-is would make the document exactly that string and delete the
	 *  freshly typed word. `reconcileHeld` merges the two instead (keeping the
	 *  typing if they overlap).
	 *
	 *  For the server's text the page has already moved its base to the held
	 *  value's revision, so while it waits `onChange` and `getDoc` report the
	 *  document as it will be once the value lands (`settled`): typing saved
	 *  on that base without the replacement reverted it on the server. By
	 *  release the page holds the merged text already, so it goes in marked as
	 *  requested. */
	type HeldDoc = HeldReplacement & { restoreFocus: boolean };
	const docQueue = new CompositionQueue<HeldDoc>({
		composing: () => view?.composing ?? false,
		apply: (held) => {
			if (!view) return;
			applyDoc(settled(view.state.doc.toString(), held), held.restoreFocus, held.programmatic);
		}
	});

	function settled(current: string, held = docQueue.pendingValue): string {
		return settleHeld(held, current, () => onReplacementConflict?.());
	}

	// Two release triggers, because neither alone is reliable. Chrome on Android
	// fires `compositionend` after the text is already in the document, and can
	// still report the composing flag set for a tick afterwards, so the DOM event
	// is deferred by a task; and some compositions end without a usable event at
	// all, leaving only the update listener to notice the flag went false.
	const compositionRelease = EditorView.domEventHandlers({
		compositionend() {
			setTimeout(() => docQueue.release(), 0);
			return false;
		}
	});

	// The release waits out the update: dispatched from inside it, the
	// listeners after this one got the outgoing update, and `onChange` reported
	// the pre-replacement text last — which the page then saved.
	let wasComposing = false;
	const compositionWatcher = EditorView.updateListener.of((u) => {
		const composing = u.view.composing;
		if (wasComposing && !composing) queueMicrotask(() => docQueue.release());
		wasComposing = composing;
	});

	/** `text` as a block of its own at the selection: on a fresh line when the
	 *  caret sits after other text, with the line ended behind it. */
	function insertOnOwnLine(editor: EditorView, text: string) {
		const { from } = editor.state.selection.main;
		const line = editor.state.doc.lineAt(from);
		const lead = editor.state.sliceDoc(line.from, from).trim() ? '\n' : '';
		editor.dispatch(editor.state.replaceSelection(lead + text + '\n'), {
			scrollIntoView: true,
			userEvent: 'input.paste'
		});
	}

	/** Upload dropped/pasted files and swap each placeholder for the real link
	 * once it lands. Placeholders are matched by text, so concurrent uploads and
	 * edits made while uploading both stay correct. */
	async function insertFiles(editor: EditorView, files: File[]) {
		for (const file of files) {
			const label = file.type.startsWith('image/') ? file.name || 'image' : file.name;
			const token = `![${label}](${UPLOADING})`;
			insertOnOwnLine(editor, token);
			try {
				const { url } = await uploadAttachment(file);
				const prefix = file.type.startsWith('image/') ? '!' : '';
				replacePlaceholder(editor, token, `${prefix}[${label}](${url})`);
			} catch (cause) {
				replacePlaceholder(editor, token, '');
				notify((cause as Error).message, { tone: 'error' });
			}
		}
	}

	function replacePlaceholder(editor: EditorView, token: string, replacement: string) {
		const at = editor.state.doc.toString().indexOf(token);
		if (at === -1) return; // the user removed it mid-upload; leave the doc alone
		editor.dispatch({
			changes: { from: at, to: at + token.length, insert: replacement }
		});
	}

	function copyPicture(url: string, markdown: string) {
		copyImage(url, markdown).then(
			() => notify('Image copied.'),
			(cause) =>
				notify(
					(cause as Error).name === 'NotAllowedError'
						? 'Could not copy — your browser blocked clipboard access.'
						: (cause as Error).message || 'Could not copy the image.',
					{ tone: 'error' }
				)
		);
	}

	/** Cmd+C / Cmd+X on a selected picture copies the picture. The markdown
	 *  goes on the clipboard first, synchronously, so a browser that refuses
	 *  the image write still leaves something that pastes back into a note. */
	function clipSelectedImage(event: ClipboardEvent, editor: EditorView, cut: boolean): boolean {
		const image = selectedImage(editor.state);
		if (!image || !event.clipboardData) return false;
		const markdown = editor.state.sliceDoc(image.from, image.to);
		event.preventDefault();
		event.clipboardData.setData('text/plain', markdown);
		copyImage(image.url, markdown).catch(() => {});
		if (cut) removeImage(editor, image);
		return true;
	}

	const ATTACHMENT_TYPES = /^(image\/|application\/pdf$)/;

	const attachmentHandlers = EditorView.domEventHandlers({
		copy(event, editor) {
			return clipSelectedImage(event, editor, false);
		},
		cut(event, editor) {
			return clipSelectedImage(event, editor, true);
		},
		paste(event, editor) {
			const files = [...(event.clipboardData?.files ?? [])].filter((f) =>
				ATTACHMENT_TYPES.test(f.type)
			);
			if (!files.length) return false;
			event.preventDefault();
			// A picture copied out of a note goes back in as the same attachment.
			const copied = pastedCopy(files);
			if (copied) insertOnOwnLine(editor, copied);
			else void insertFiles(editor, files);
			return true;
		},
		drop(event, editor) {
			const files = [...(event.dataTransfer?.files ?? [])].filter((f) =>
				ATTACHMENT_TYPES.test(f.type)
			);
			if (!files.length) return false;
			event.preventDefault();
			// Drop at the cursor under the pointer, not wherever the caret was.
			const pos = editor.posAtCoords({ x: event.clientX, y: event.clientY });
			if (pos !== null) editor.dispatch({ selection: { anchor: pos } });
			void insertFiles(editor, files);
			return true;
		}
	});

	/** The picture open at full size, if any. */
	let viewing = $state<{ url: string; alt: string } | null>(null);

	function onImageAction(event: Event) {
		const { action, image } = (event as CustomEvent<ImageActionDetail>).detail;
		const markdown = `![${image.alt}](${image.url})`;
		if (action === 'copy') copyPicture(image.url, markdown);
		else if (action === 'view') viewing = { url: image.url, alt: image.alt };
		else
			downloadImage(image.url, image.alt).catch((cause) =>
				notify((cause as Error).message, { tone: 'error' })
			);
	}

	$effect(() => {
		const node = host;
		if (!node) return;
		node.addEventListener(IMAGE_ACTION_EVENT, onImageAction);
		return () => node.removeEventListener(IMAGE_ACTION_EVENT, onImageAction);
	});

	export function insertAttachments(files: File[]) {
		if (view) void insertFiles(view, files);
	}

	/** Scroll a 1-based document line into view and put the caret on it. */
	export function revealLine(line: number) {
		if (!view) return;
		const target = view.state.doc.line(Math.min(Math.max(line, 1), view.state.doc.lines));
		view.dispatch({
			selection: { anchor: target.from },
			effects: EditorView.scrollIntoView(target.from, { y: 'start', yMargin: 12 })
		});
		view.focus();
	}

	function create(doc: string) {
		view?.destroy();
		// Anything held for the outgoing view describes a document that is about
		// to stop existing.
		docQueue.cancel();
		wasComposing = false;
		// A new view starts with whatever the compartment should hold right now,
		// so switching notes while formatted keeps the formatted view.
		appliedDynamic = untrack(() => dynamicExtensions);
		const editorExtensions: Extension[] = [
			accidentalBlurGuard,
			slashTaskShortcut,
			attachmentHandlers,
			selectionMenu,
			compositionRelease,
			compositionWatcher,
			// Cmd/Ctrl+F searches within the note. The browser's own find cannot
			// see CodeMirror's virtualized lines, so this is the only one that works.
			search({ top: true }),
			highlightSelectionMatches(),
			// Dragging text inside the note shows where it will land.
			dropCursor(),
			// Zero-width and control characters would otherwise be invisible text
			// that still moves the caret — pasted-in artifacts, mostly.
			highlightSpecialChars(),
			bracketMatching(),
			// drawSelection() is deliberately absent: its layer-drawn caret
			// renders 0px tall whenever it draws from unmeasured geometry (first
			// paint after focus, rAF-suspended tabs) — a caret that can vanish.
			// The native caret cannot, and it is themed via caretColor; the
			// selection color the drawn layer provided comes from ::selection in
			// the theme instead.
			// Keep a generous editing history and support the literal Control key
			// on macOS in addition to CodeMirror's platform-native Command+Z.
			history({ minDepth: 500, newGroupDelay: 750 }),
			// Escape must never leave Tab moving browser focus, and the tab-focus
			// toggle must stay unreachable.
			tabAlwaysIndents,
			listIndentConfig,
			// Auto-pairs, and typing a marker over a selection wraps it. Both stay
			// at default precedence: lang-markdown's Prec.high Backspace
			// (`deleteMarkupBackward`, which strips a list marker) must keep first
			// refusal, and `deleteBracketPair` takes the fall-through.
			closeBrackets(),
			wrapSelectionOnType,
			// Typing rules (`[] ` → checkbox, `-- ` → em dash, `...` → ellipsis).
			// Default precedence, below the Prec.high wrap handler above.
			inputRules,
			keymap.of([
				{ key: 'Ctrl-z', run: undo, preventDefault: true },
				{ key: 'Ctrl-Shift-z', run: redo, preventDefault: true },
				{ key: 'Ctrl-y', run: redo, preventDefault: true },
				// Swallowed whether or not a handler is wired: the browser's Save
				// dialog is never the right answer inside a note.
				{
					key: 'Mod-s',
					run: () => {
						onSave?.();
						return true;
					},
					preventDefault: true
				},
				// Tab does the obvious thing for wherever the caret is: accept the
				// `[[` completion, else nest the list item, else insert at the
				// caret (indenting whole lines only when text is selected).
				{
					key: 'Tab',
					run: chain(acceptCompletion, indentListItem(1), insertSoftTab),
					preventDefault: true
				},
				{ key: 'Shift-Tab', run: chain(indentListItem(-1), indentLess), preventDefault: true },
				...markdownKeys,
				...searchKeymap,
				// Before defaultKeymap, so `deleteBracketPair` gets Backspace ahead
				// of `deleteCharBackward`.
				...closeBracketsKeymap,
				...defaultKeymap,
				...historyKeymap
			]),
			markdown({
				base: markdownLanguage,
				codeLanguages: languages,
				// No Setext headings: a lone `-` typed under a paragraph (the
				// first keystroke of every new bullet) would momentarily parse
				// the paragraph above as a heading and flash it large. ATX `#`
				// headings are unaffected.
				extensions: [{ remove: ['SetextHeading'] }]
			}),
			// Prose auto-pairs, scoped through markdown's language data. Quotes are
			// excluded on purpose — `handleSame` would double a trailing apostrophe
			// — and so is `*`, which the wrap handler above owns. `[` closing is
			// what makes `[[` produce `[[|]]` for the wikilink completion.
			markdownLanguage.data.of({
				closeBrackets: { brackets: ['(', '[', '`'], before: ')]}:;>' }
			}),
			syntaxHighlighting(mdHighlight),
			EditorView.lineWrapping,
			// Soft-keyboard behaviour. CodeMirror's contenteditable ships with
			// autocapitalize/autocorrect off, which is right for code and wrong for
			// a notebook: on a phone that means no sentence capitals, no OS spell
			// correction and a "go" key that will not insert a newline. (Unrelated
			// to OMNIA's own Autocorrect — that is a server-side proofreader; this
			// is the on-device keyboard.)
			EditorView.contentAttributes.of({
				autocapitalize: 'sentences',
				autocorrect: 'on',
				spellcheck: 'true',
				enterkeyhint: 'enter'
			}),
			cmPlaceholder('Start writing…'),
			theme,
			EditorView.updateListener.of((u) => {
				if (u.docChanged)
					onChange(settled(u.state.doc.toString()), isProgrammatic(u.transactions));
				// Any selection movement (arrow keys, programmatic) outdates the
				// toolbar's anchor; the formatting buttons close it themselves.
				if (u.selectionSet && selMenu) selMenu = null;
				// Publish the current selection so OMNIA can resolve "rewrite this",
				// plus the exact offsets so its selection-editing tools splice the
				// precise range (docLen is the backend's staleness guard).
				if (u.selectionSet || u.docChanged) {
					const sel = u.state.selection.main;
					omniaContext.selection = sel.empty
						? ''
						: u.state.sliceDoc(sel.from, sel.to).slice(0, 4000);
					omniaContext.from = sel.from;
					omniaContext.to = sel.to;
					omniaContext.head = sel.head;
					omniaContext.docLen = u.state.doc.length;
					omniaContext.path = path;
				}
			}),
			...extensions,
			formatCompartment.of(appliedDynamic)
		];
		view = new EditorView({
			parent: host,
			state: EditorState.create({ doc, extensions: editorExtensions })
		});
		// Opening a note is almost always followed by typing — hand the editor
		// focus so the caret is there without a click. The microtask lets the
		// page finish mounting; the offsetParent guard keeps a hidden editor
		// (Read mode) from stealing focus.
		queueMicrotask(() => {
			if (view && host?.offsetParent) view.focus();
		});
	}

	// Recreate the editor only when switching notes (keyed by path). Typing
	// updates flow one-way out via onChange, so `content` is read untracked.
	let builtFor: string | null = null;
	$effect(() => {
		const currentPath = path;
		if (!host) return;
		// Compare values, don't just re-run. `path` comes from `note.path`, and
		// the note object is reassigned on every save — so this effect fired on
		// each autosave with an identical path and tore the editor down under
		// the cursor. That is what unfocused the box and swallowed keystrokes.
		if (view && builtFor === currentPath) return;
		const doc = untrack(() => content);
		builtFor = currentPath;
		// `create` reads the `extensions` prop, and reading it here would make it
		// a dependency of this effect — so every time the parent's $derived
		// extension array got a new identity (which happens on each save), the
		// whole EditorView was destroyed and rebuilt underneath the cursor. That
		// is what dropped keystrokes and threw focus to <body> mid-typing.
		untrack(() => create(doc));
	});

	// Swap the live half of the configuration. Dispatching from an effect is
	// deliberate: a reconfigure raised from inside an update listener would
	// re-enter the update cycle.
	$effect(() => {
		const next = dynamicExtensions;
		if (!view || next === appliedDynamic) return;
		appliedDynamic = next;
		view.dispatch({ effects: formatCompartment.reconfigure(next) });
		// The old refreshKey teardown ended in create()'s focus(), so every
		// view-mode toggle used to hand focus (and the caret) back to the
		// editor. The reconfigure must do the same, or the caret is gone
		// until the user clicks.
		view.focus();
	});

	onDestroy(() => {
		if (typeof document !== 'undefined')
			document.removeEventListener('selectionchange', onDocumentSelectionChange);
		if (selectionChangeTimer !== null) clearTimeout(selectionChangeTimer);
		docQueue.cancel();
		view?.destroy();
		// The editor is gone; a stale selection must not follow the user elsewhere.
		omniaContext.selection = '';
		omniaContext.path = '';
		omniaContext.from = omniaContext.to = omniaContext.head = omniaContext.docLen = 0;
	});

	/** The live EditorView, for extensions that own state the page must read
	 * (proofreading anchors). Null before the first mount. */
	export function getView(): EditorView | null {
		return view;
	}

	/** The document, with a replacement held for an IME already in (see
	 *  `settled`). */
	export function getDoc(): string {
		return view ? settled(view.state.doc.toString()) : content;
	}

	export function hasFocus(): boolean {
		return view?.hasFocus ?? false;
	}

	/** Re-measure after the editor is revealed again.
	 *
	 * Read mode hides this component rather than unmounting it — unmounting
	 * threw away unsaved text, undo history and scroll position. CodeMirror
	 * cannot measure while it is `display: none`, so it needs a nudge once it
	 * is back on screen or it paints with a zero-height viewport. */
	export function remeasure(): void {
		view?.requestMeasure();
	}

	export function restoreFocusIfLost(shouldRestore = true): void {
		if (!shouldRestore || !view || !document.hasFocus()) return;
		const active = document.activeElement;
		if (
			active &&
			active !== document.body &&
			active !== document.documentElement
		)
			return;
		const editor = view;
		queueMicrotask(() => {
			const currentActive = document.activeElement;
			if (
				view === editor &&
				document.hasFocus() &&
				(!currentActive ||
					currentActive === document.body ||
					currentActive === document.documentElement)
			)
				editor.focus();
		});
	}

	/** A rename: same document, new name. Adopt the path so the keying effect
	 * sees it as already built — rebuilding here would throw away the DOM,
	 * cursor, scroll position and undo history of a note that never changed. */
	export function adoptPath(newPath: string): void {
		if (view) builtFor = newPath;
	}

	/** Put `doc` in the editor, preserving the caret.
	 *
	 *  `programmatic` (the default) marks the change so `onChange` reports it
	 *  as the app's, not the user's; pass false for an edit made on the user's
	 *  behalf that must be saved like typing. `remote` marks text carrying
	 *  another device's or OMNIA's edit: if an IME holds it and the composition
	 *  overlaps it, `onReplacementConflict` asks. Identical text dispatches
	 *  nothing.
	 *
	 *  Not necessarily synchronous: while an IME composition is in flight the
	 *  replacement is held (see `composequeue.ts`) and applied — reconciled
	 *  with whatever was composed meanwhile — once the composition ends, or
	 *  after the hold cap, whichever comes first. */
	export function replaceDoc(
		doc: string,
		{
			restoreFocus = false,
			programmatic = true,
			remote = false
		}: { restoreFocus?: boolean; programmatic?: boolean; remote?: boolean } = {}
	): void {
		if (!view) return;
		docQueue.request({
			base: view.state.doc.toString(),
			doc,
			restoreFocus,
			programmatic,
			remote
		});
	}

	function applyDoc(doc: string, restoreFocus: boolean, programmatic: boolean): void {
		if (!view) return;
		const current = view.state.doc.toString();
		if (current === doc) {
			restoreFocusIfLost(restoreFocus);
			return;
		}

		const editor = view;
		const keepFocus = editor.hasFocus || restoreFocus;
		const selectionBeforeSave = editor.state.selection;

		// The server may update several separate regions at once (for example
		// the modified timestamp, hidden task metadata, and a final newline).
		// Apply those as distinct changes so unchanged text — including the
		// caret's surrounding line — keeps its identity.
		const changes = diff(current, doc, { scanLimit: 5000, timeout: 100 }).map((change) => ({
			from: change.fromA,
			to: change.toA,
			insert: doc.slice(change.fromB, change.toB)
		}));
		const changeSet = editor.state.changes(changes);

		// Use a before-insertion association for cursors. In particular, if
		// normalization appends a newline exactly at the caret, the caret stays
		// on the line the user was editing instead of jumping to the new line.
		const preservedSelection = selectionBeforeSave.map(changeSet, -1);
		view.dispatch({
			changes: changeSet,
			selection: preservedSelection,
			annotations: programmatic ? programmaticChange.of(true) : undefined
		});
		if (keepFocus) restoreFocusIfLost(true);
	}
</script>

<svelte:window
	onscrollcapture={() => (selMenu = null)}
	onkeydown={(e) => {
		if (e.key === 'Escape') selMenu = null;
	}}
/>

<div class="editor-host" bind:this={host}></div>
{#if viewing}
	<div
		class="viewer"
		role="presentation"
		transition:fade={{ duration: 120 }}
		onclick={() => (viewing = null)}
	>
		<!-- Any click closes it, and so does Esc; the dialog is the modal node,
		     so focus goes back to the editor when it does. -->
		<div
			class="viewer-frame"
			role="dialog"
			aria-modal="true"
			aria-label={viewing.alt || 'Image'}
			tabindex="-1"
			use:modal={{ onEscape: () => (viewing = null) }}
		>
			<img src={viewing.url} alt={viewing.alt} />
		</div>
	</div>
{/if}
{#if selMenu}
	<div
		class="sel-menu"
		role="toolbar"
		aria-label="Format selection"
		style:left="{selMenu.x}px"
		style:top="{selMenu.y}px"
	>
		<!-- onmousedown preventDefault: the editor must keep focus and
		     selection while the button is pressed. -->
		<button
			title="Bold"
			aria-label="Bold"
			onmousedown={(e) => e.preventDefault()}
			onclick={() => runFormat(toggleBold)}><strong>B</strong></button>
		<button
			title="Italic"
			aria-label="Italic"
			onmousedown={(e) => e.preventDefault()}
			onclick={() => runFormat(toggleItalic)}><em>I</em></button>
		<button
			title="Underline"
			aria-label="Underline"
			onmousedown={(e) => e.preventDefault()}
			onclick={() => runFormat(toggleUnderline)}><u>U</u></button>
		<button
			title="Strikethrough"
			aria-label="Strikethrough"
			onmousedown={(e) => e.preventDefault()}
			onclick={() => runFormat(toggleStrikethrough)}><s>S</s></button>
	</div>
{/if}

<style>
	.editor-host {
		height: 100%;
		overflow: hidden;
	}
	.editor-host :global(.cm-editor) {
		height: 100%;
	}

	.viewer {
		position: fixed;
		inset: 0;
		z-index: var(--z-dialog);
		display: grid;
		place-items: center;
		padding: 24px;
		background: color-mix(in srgb, #000 72%, transparent);
		cursor: zoom-out;
	}
	.viewer-frame {
		display: grid;
		place-items: center;
		max-width: 100%;
		max-height: 100%;
		outline: none;
	}
	.viewer img {
		max-width: 100%;
		max-height: calc(100dvh - 48px);
		object-fit: contain;
		border-radius: 8px;
		box-shadow: var(--lift-shadow);
	}

	.sel-menu {
		position: fixed;
		z-index: 60;
		display: flex;
		gap: 2px;
		padding: 3px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-panel);
		box-shadow: var(--lift-shadow);
	}
	.sel-menu button {
		width: 28px;
		height: 26px;
		padding: 0;
		border: 0;
		border-radius: 6px;
		background: none;
		color: var(--text);
		font-size: 13px;
		cursor: pointer;
	}
	.sel-menu button:hover {
		background: var(--bg-hover);
	}

	@media (max-width: 760px) {
		/* 112px is the mobile chrome above the editor — the note bar (a 44px
		   action row in 4px of padding, plus its hairline) and the note
		   heading; --tabbar-h is the chrome below it. Both come off the
		   viewport height or the editor claims space it does not have and the
		   last lines sit under the tab bar. */
		.editor-host,
		.editor-host :global(.cm-editor) {
			height: auto !important;
			min-height: calc(100dvh - 112px - var(--tabbar-h, 0px));
			overflow: visible;
		}
		.editor-host :global(.cm-content) {
			max-width: none;
			min-height: calc(100dvh - 112px - var(--tabbar-h, 0px));
			padding: 18px var(--m-gutter) 10vh !important;
			font-size: var(--m-row-text);
			line-height: 1.7;
		}
		.editor-host :global(.cm-scroller) {
			overflow: visible !important;
			-webkit-overflow-scrolling: touch;
		}
	}
</style>
