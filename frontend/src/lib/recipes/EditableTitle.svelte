<script lang="ts">
	// The recipe's name in the stretched serif, after the recipe app's
	// EditableTitle. Double-click to rename in place: Enter or clicking away
	// saves, Escape cancels, and an empty or unchanged name saves nothing.
	// `onrename` throws to keep the field open with what was typed, so a
	// failed save (a conflict, the network) loses nothing.
	import { tick } from 'svelte';
	import StretchedTitle from './StretchedTitle.svelte';

	let {
		text,
		onrename
	}: {
		text: string;
		onrename: (title: string) => Promise<void>;
	} = $props();

	let editing = $state(false);
	let value = $state('');
	let saving = $state(false);
	let height = $state<number>();
	let box = $state<HTMLDivElement>();
	let input = $state<HTMLInputElement>();
	// Set once Enter, Escape or a blur has decided; the blur that follows
	// Enter or Escape must not decide again.
	let settled = false;

	$effect(() => {
		if (!editing || !input) return;
		input.focus();
		input.select();
	});

	function start() {
		if (editing) return;
		value = text;
		// Hold the title's height so nothing below jumps while the field is open.
		height = box?.offsetHeight;
		settled = false;
		editing = true;
	}

	async function finish(save: boolean) {
		if (settled) return;
		settled = true;
		const next = value.trim();
		if (!save || !next || next === text) {
			editing = false;
			return;
		}
		saving = true;
		try {
			await onrename(next);
			editing = false;
		} catch {
			// Stay open with the typed name; the page says what went wrong.
			saving = false;
			settled = false;
			await tick();
			input?.focus();
		} finally {
			saving = false;
		}
	}

	function onkeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' && !e.isComposing) {
			e.preventDefault();
			void finish(true);
		} else if (e.key === 'Escape') {
			// Escape is the page's "back" too; here it only cancels.
			e.preventDefault();
			e.stopPropagation();
			void finish(false);
		}
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	bind:this={box}
	class="editable-title"
	class:editing
	style:min-height={editing && height ? `${height}px` : undefined}
	title={editing ? undefined : 'Double-click to rename'}
	ondblclick={start}
>
	{#if editing}
		<input
			bind:this={input}
			bind:value
			class="title-input"
			maxlength="200"
			disabled={saving}
			aria-label="Recipe name"
			autocomplete="off"
			spellcheck="false"
			{onkeydown}
			onblur={() => void finish(true)}
		/>
	{:else}
		<StretchedTitle {text} align="center" />
	{/if}
</div>

<style>
	.editable-title {
		display: flex;
		flex-direction: column;
		justify-content: center;
		cursor: text;
	}
	/* The field keeps the title's look: the same serif and stretch, with a
	   caret and a hairline under it. The descendant selector outranks the
	   recipes shell's quiet field look. */
	.editable-title .title-input {
		width: 100%;
		min-height: 0;
		padding: 0.1em 0 0.15em;
		border: 0;
		border-bottom: 1px solid var(--rc-hair);
		border-radius: 0;
		background: transparent;
		box-shadow: none;
		color: var(--rc-ink);
		font: 400 clamp(2rem, 6.5vw, 3.6rem) / 1.15 var(--rc-serif);
		letter-spacing: -0.05em;
		text-align: center;
		text-shadow: var(--rc-fringe);
		/* The title's stretch; not feedback. */
		transform: scaleY(1.6);
	}
	.editable-title .title-input:focus {
		outline: none;
		border-bottom-color: var(--rc-muted);
		background: transparent;
	}
	.editable-title .title-input:disabled {
		opacity: 0.6;
	}

	/* A large title field, like the app's own opt-outs from the 16px phone
	   floor (app.css): it is far above 16px, so iOS does not zoom on it. */
	@media (max-width: 760px) {
		.editable-title .title-input {
			font-size: clamp(2rem, 6.5vw, 3.6rem) !important;
		}
	}
</style>
