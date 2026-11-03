<script lang="ts">
	// The small tracked words after a scaled amount, after the recipe app's
	// Markers: "estimated" (the AI guessed the amount), "rounded" (a
	// whole-number item was rounded) and "starting point" (a to-taste item at
	// a changed serving count). With `onconfirm`, "estimated" is a button that
	// confirms the amount.
	import type { Marker } from './scaling';

	let {
		markers,
		onconfirm,
		busy = false
	}: {
		markers: Marker[];
		onconfirm?: () => void;
		/** A confirm is saving. */
		busy?: boolean;
	} = $props();

	const LABEL: Record<Marker, string> = {
		estimated: 'estimated',
		rounded: 'rounded',
		to_taste: 'starting point'
	};
	const TITLE: Record<Marker, string> = {
		estimated: 'Estimated amount',
		rounded: 'Rounded to a whole number',
		to_taste: 'Scaled to taste'
	};
</script>

{#if markers.length > 0}
	<span class="markers">
		{#each markers as marker (marker)}
			{#if marker === 'estimated' && onconfirm}
				<button
					type="button"
					class="marker confirm"
					title="Confirm amount"
					disabled={busy}
					onclick={onconfirm}>{LABEL[marker]}</button
				>
			{:else}
				<span class="marker" title={TITLE[marker]}>{LABEL[marker]}</span>
			{/if}
		{/each}
	</span>
{/if}

<style>
	.markers {
		display: inline-flex;
		align-items: baseline;
		gap: 6px;
	}
	.marker {
		display: inline-block;
		padding: 0;
		color: var(--rc-muted);
		font: 600 0.6rem / 1.5 var(--rc-sans);
		letter-spacing: var(--rc-label-track);
		text-transform: uppercase;
	}
	.confirm {
		min-height: 0;
		border: 0;
		border-radius: 0;
		background: none;
		color: var(--rc-focus);
		text-decoration: underline dotted;
		text-underline-offset: 0.3em;
		cursor: pointer;
		transition: color 0.15s ease;
	}
	.confirm:hover:not(:disabled) {
		background: none;
		color: var(--rc-ink);
	}
	.confirm:disabled {
		opacity: 0.5;
		cursor: default;
	}

	/* A 44px box for the thumb; the negative margin keeps the row its height. */
	/* Phones, and touch screens wider than one (an iPad): thumb-sized. */
	@media (max-width: 760px), (hover: none) and (pointer: coarse) {
		.marker {
			font-size: calc(var(--m-label) * 0.88);
		}
		.confirm {
			min-height: 44px;
			margin: -12px 0;
			padding: 0 6px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.confirm {
			transition: none;
		}
	}
</style>
