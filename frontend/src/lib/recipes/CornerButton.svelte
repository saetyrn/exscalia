<script lang="ts">
	// One bare icon in a recipe page's corner row: a glyph, a tooltip, no
	// circle. With `href` it is a link that slides forward to it (Cmd-click
	// still opens a background tab); `onclick` replaces the slide. Without
	// `href` it is a button; `pressed` lights it for a toggle.
	import type { HTMLAttributes } from 'svelte/elements';
	import Glyph from '$lib/Glyph.svelte';
	import { forward } from './nav';

	let {
		glyph,
		label,
		shortcut,
		href,
		onclick,
		pressed,
		disabled = false,
		...rest
	}: {
		glyph: string;
		label: string;
		/** Shown in the tooltip: "Edit (E)". */
		shortcut?: string;
		href?: string;
		onclick?: (e: MouseEvent) => void;
		pressed?: boolean;
		disabled?: boolean;
	} & Omit<HTMLAttributes<HTMLElement>, 'onclick'> = $props();

	let title = $derived(shortcut ? `${label} (${shortcut})` : label);

	function follow(e: MouseEvent) {
		if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
		e.preventDefault();
		if (onclick) onclick(e);
		else if (href) void forward(href);
	}
</script>

{#if href && !disabled}
	<a {...rest} class="rc-corner-btn" class:is-on={pressed} {href} aria-label={label} {title} onclick={follow}>
		<Glyph name={glyph} />
	</a>
{:else}
	<button
		{...rest}
		type="button"
		class="rc-corner-btn"
		class:is-on={pressed}
		aria-label={label}
		aria-pressed={'aria-expanded' in rest ? undefined : pressed}
		{title}
		{disabled}
		{onclick}
	>
		<Glyph name={glyph} />
	</button>
{/if}

<style>
	/* Bare icons: no circle, no glass, just the glyph at a finer stroke than
	   the app's (the CSS stroke-width wins over Glyph's attribute). Feedback is
	   opacity only. */
	.rc-corner-btn {
		display: grid;
		place-items: center;
		width: 40px;
		height: 40px;
		min-height: 0;
		padding: 0;
		border: 0;
		border-radius: 8px;
		background: none;
		color: var(--rc-ink, currentColor);
		opacity: 0.72;
		text-decoration: none;
		cursor: pointer;
		transition: opacity 0.15s ease;
	}
	.rc-corner-btn:hover,
	.rc-corner-btn.is-on {
		background: none;
		opacity: 1;
	}
	.rc-corner-btn:disabled {
		opacity: 0.3;
		cursor: default;
	}
	.rc-corner-btn:focus-visible {
		outline: 2px solid var(--rc-focus, var(--accent));
		outline-offset: 3px;
	}
	.rc-corner-btn :global(svg) {
		width: 22px;
		height: 22px;
		stroke-width: 1.6;
	}

	/* Phones, and touch screens wider than one (an iPad): thumb-sized. */
	@media (max-width: 760px), (hover: none) and (pointer: coarse) {
		.rc-corner-btn {
			width: 44px;
			height: 44px;
		}
		.rc-corner-btn :global(svg) {
			width: var(--m-icon-bar);
			height: var(--m-icon-bar);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.rc-corner-btn {
			transition: none;
		}
	}
</style>
