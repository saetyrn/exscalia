<script lang="ts">
	// The top-left corner row every recipe screen shares: back, the page's own
	// actions (CornerButton children), and OMNIA last. Rendered by the recipes
	// layout in the pane's fixed corner (see chrome.svelte.ts), so it stays put
	// while the page scrolls and slides with the page. Place it anywhere in the
	// page; it draws nothing where it stands.
	//
	// With `back`, Left and Escape go back (the reverse slide, popping history
	// when the page was slid into). K, which opens OMNIA, is the layout's.
	import type { Snippet } from 'svelte';
	import { overseerStatus } from '$lib/stores.svelte';
	import CornerButton from './CornerButton.svelte';
	import { recipesChrome } from './chrome.svelte';
	import { plainKey } from './keys';
	import { back as goBack } from './nav';

	let {
		back,
		backLabel = 'Back',
		omnia = true,
		label = 'Page actions',
		children
	}: {
		/** Where back goes when the page was not slid into: a recipes URL. */
		back?: string;
		backLabel?: string;
		/** OMNIA's subject: a recipe's vault path, true for the Recipes
		 * conversation, false for no OMNIA button. */
		omnia?: string | boolean;
		/** The row's accessible name. */
		label?: string;
		children?: Snippet;
	} = $props();

	$effect(() => {
		recipesChrome.corner = row;
		return () => {
			if (recipesChrome.corner === row) recipesChrome.corner = null;
		};
	});

	$effect(() => {
		recipesChrome.omnia = omnia;
		return () => {
			recipesChrome.omnia = true;
		};
	});

	function goBackNow() {
		if (back) void goBack(back);
	}

	function onKeydown(e: KeyboardEvent) {
		if (!back || e.shiftKey || recipesChrome.omniaOpen || !plainKey(e)) return;
		if (e.key !== 'Escape' && e.key !== 'ArrowLeft') return;
		e.preventDefault();
		goBackNow();
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#snippet row()}
	<nav class="corner" aria-label={label}>
		{#if back}
			<CornerButton glyph="chevron-left" label={backLabel} shortcut="Esc" href={back} onclick={goBackNow} />
		{/if}
		{@render children?.()}
		{#if omnia !== false && overseerStatus.available}
			<CornerButton
				glyph="chat"
				label="OMNIA"
				shortcut="K"
				pressed={recipesChrome.omniaOpen}
				aria-haspopup="dialog"
				aria-expanded={recipesChrome.omniaOpen}
				data-omnia-trigger=""
				onclick={() => (recipesChrome.omniaOpen = !recipesChrome.omniaOpen)}
			/>
		{/if}
	</nav>
{/snippet}

<style>
	.corner {
		display: flex;
		align-items: center;
		gap: 4px;
	}
</style>
