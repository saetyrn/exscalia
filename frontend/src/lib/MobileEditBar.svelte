<script lang="ts">
	// The five editing actions a phone keyboard cannot reach.
	//
	// Tab, Shift-Tab, `-`, Mod-Enter and `[[` are all one key away on a laptop
	// and unreachable on a soft keyboard, which is why nesting a list or ticking
	// a task off on a phone meant hand-typing the markdown. This bar sits on top
	// of the keyboard while it is up and runs the exact same commands the keys
	// do — no parallel implementation to drift.
	//
	// Touch only. On a laptop it never mounts anything visible: the keys are
	// right there, and a bar over the text would just be in the way.
	import { onDestroy, onMount } from 'svelte';
	import { indentLess, indentMore } from '@codemirror/commands';
	import type { EditorView } from '@codemirror/view';
	import { insertWikilink, toggleBullet, toggleTaskMarker } from './editorkeys';
	import { coarsePointer, measureKeyboard, softKeyboard, watchSoftKeyboard } from './keyboard.svelte';
	import { indentListItem } from './listindent';

	let { getView }: { getView: () => EditorView | null } = $props();

	// Whether a keyboard is up — and how far up — is measured once for the
	// whole app in keyboard.svelte.ts; the tab bar has to duck the same
	// keyboard this bar rides on. What stays local is the second half of the
	// condition: this bar only earns its place while the *editor* has the
	// caret, not whenever some search field is open.
	let editorFocused = $state(false);

	let visible = $derived(coarsePointer && softKeyboard.up && editorFocused);

	function refreshFocus() {
		// `focusout` fires before the incoming element takes focus, so reading
		// during the event would always say "nothing focused".
		setTimeout(() => {
			editorFocused = getView()?.hasFocus ?? false;
			measureKeyboard();
		}, 0);
	}

	onMount(() => {
		if (!coarsePointer) return;
		watchSoftKeyboard();
		editorFocused = getView()?.hasFocus ?? false;
		document.addEventListener('focusin', refreshFocus);
		document.addEventListener('focusout', refreshFocus);
	});

	onDestroy(() => {
		if (typeof window === 'undefined') return;
		document.removeEventListener('focusin', refreshFocus);
		document.removeEventListener('focusout', refreshFocus);
	});

	/** Run commands in order and stop at the first one that claims the action —
	 *  the same fall-through the Tab key uses. */
	function run(...commands: ((view: EditorView) => boolean)[]) {
		const view = getView();
		if (!view) return;
		commands.some((command) => command(view));
		// The button already refused focus, so the caret never left; this only
		// covers a browser that moved it anyway.
		view.focus();
	}

	const actions = [
		{
			label: 'Outdent',
			glyph: '⇤',
			run: () => run(indentListItem(-1), indentLess)
		},
		{
			label: 'Indent',
			glyph: '⇥',
			run: () => run(indentListItem(1), indentMore)
		},
		// A bullet and a ballot box draw far smaller than the arrows at the same
		// type size; these bring the five glyphs to the same visual weight.
		{ label: 'Bullet list', glyph: '•', size: '22px', run: () => run(toggleBullet) },
		{ label: 'Checkbox', glyph: '☐', size: '19px', run: () => run(toggleTaskMarker) },
		{ label: 'Link a note', glyph: '[[', run: () => run(insertWikilink) }
	] as { label: string; glyph: string; size?: string; run: () => void }[];
</script>

{#if visible}
	<div
		class="edit-bar"
		role="toolbar"
		aria-label="Editing actions"
		style:bottom={softKeyboard.inset
			? `${softKeyboard.inset}px`
			: 'env(safe-area-inset-bottom, 0px)'}
	>
		{#each actions as action (action.label)}
			<!-- Refusing pointerdown is the whole trick: without it the button takes
			     focus, the keyboard closes, and the command runs against a caret
			     that is no longer anywhere. -->
			<button
				type="button"
				title={action.label}
				aria-label={action.label}
				style:font-size={action.size}
				onpointerdown={(e) => e.preventDefault()}
				onmousedown={(e) => e.preventDefault()}
				onclick={action.run}>{action.glyph}</button>
		{/each}
	</div>
{/if}

<style>
	.edit-bar {
		position: fixed;
		left: 0;
		right: 0;
		z-index: 55;
		display: flex;
		justify-content: space-around;
		gap: 2px;
		padding: 6px 8px;
		border-top: 1px solid var(--border);
		background: var(--bg-panel);
	}
	.edit-bar button {
		flex: 1;
		min-width: 0;
		height: 34px;
		padding: 0;
		border: 0;
		border-radius: 6px;
		background: none;
		color: var(--text);
		font-family: var(--font-ui);
		font-size: 16px;
		line-height: 1;
		cursor: pointer;
	}
	.edit-bar button:active {
		background: var(--bg-active);
	}

	/* A mouse and keyboard have all of these one key away. */
	@media (pointer: fine) {
		.edit-bar {
			display: none;
		}
	}
</style>
