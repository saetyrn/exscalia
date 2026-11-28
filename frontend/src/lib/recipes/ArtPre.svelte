<script lang="ts">
	// A recipe's coloured ASCII drawing, after the recipe app's ArtPre: each
	// run painted in its palette colour with a soft glow of its own colour,
	// like phosphor. Size and placement are the caller's (`class`, `style`).
	import { artRows } from './art';

	let {
		art,
		colors = '',
		palette = {},
		label,
		class: className = '',
		style = ''
	}: {
		art: string;
		colors?: string;
		palette?: Record<string, string>;
		/** The accessible name: the dish. */
		label: string;
		class?: string;
		style?: string;
	} = $props();

	let rows = $derived(artRows(art, colors, palette));
</script>

<!-- One line on purpose: whitespace inside <pre> is drawn. -->
<pre class="rc-ascii {className}" role="img" aria-label={label} {style}>{#each rows as runs}<span class="line">{#each runs as run}{#if run.color}<span style:color={run.color}>{run.text}</span>{:else}{run.text}{/if}{/each}{'\n'}</span>{/each}</pre>

<style>
	.rc-ascii {
		margin: 0;
		font:
			400 10px / 1.08 var(--rc-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
		color: var(--rc-muted, currentColor);
		text-shadow: 0 0 4px currentColor;
		white-space: pre;
		text-align: left;
	}
	.line {
		display: inline;
	}
	.line span {
		text-shadow: inherit;
	}
</style>
