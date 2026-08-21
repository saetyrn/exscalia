<script lang="ts">
	import { starTitle } from '$lib/media/helpers';

	// Five stars, half-star steps. `value` stays on the stored 1–10 scale
	// (1 = half a star, 10 = five stars); only the presentation is stars.
	let {
		value = null,
		disabled = false,
		onrate
	}: {
		value?: number | null;
		disabled?: boolean;
		onrate: (rating10: number) => void;
	} = $props();

	let preview: number | null = $state(null);
	let shown = $derived(preview ?? value ?? 0);

	const STAR = 'M12 2l2.9 6.6 7.1.6-5.4 4.7 1.6 7-6.2-3.7-6.2 3.7 1.6-7L2 9.2l7.1-.6z';

	function portion(star: number): 0 | 1 | 2 {
		return Math.max(0, Math.min(2, shown - (star - 1) * 2)) as 0 | 1 | 2;
	}
</script>

<div
	class="stars"
	role="radiogroup"
	tabindex="-1"
	aria-label="Rating out of five stars"
	onmouseleave={() => (preview = null)}
>
	{#each [1, 2, 3, 4, 5] as star (star)}
		{@const filled = portion(star)}
		<span class="star" class:previewing={preview != null}>
			<svg viewBox="0 0 24 24" class="base" aria-hidden="true">
				<path d={STAR} />
			</svg>
			{#if filled > 0}
				<span class="fill" style:width={filled === 1 ? '50%' : '100%'}>
					<svg viewBox="0 0 24 24" aria-hidden="true"><path d={STAR} /></svg>
				</span>
			{/if}
			<button
				type="button"
				class="zone left"
				role="radio"
				aria-checked={value === star * 2 - 1}
				aria-label={starTitle(star * 2 - 1)}
				title={starTitle(star * 2 - 1)}
				{disabled}
				onmouseenter={() => (preview = star * 2 - 1)}
				onfocus={() => (preview = star * 2 - 1)}
				onblur={() => (preview = null)}
				onclick={() => onrate(star * 2 - 1)}
			></button>
			<button
				type="button"
				class="zone right"
				role="radio"
				aria-checked={value === star * 2}
				aria-label={starTitle(star * 2)}
				title={starTitle(star * 2)}
				{disabled}
				onmouseenter={() => (preview = star * 2)}
				onfocus={() => (preview = star * 2)}
				onblur={() => (preview = null)}
				onclick={() => onrate(star * 2)}
			></button>
		</span>
	{/each}
</div>

<style>
	.stars {
		display: inline-flex;
		gap: 3px;
	}
	.star {
		position: relative;
		width: var(--star-size, 24px);
		height: var(--star-size, 24px);
	}
	.star svg {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
	}
	.base path {
		fill: none;
		stroke: var(--star-dim);
		stroke-width: 1.6;
		stroke-linejoin: round;
	}
	.fill {
		position: absolute;
		inset: 0;
		overflow: hidden;
	}
	.fill svg {
		width: var(--star-size, 24px);
		height: var(--star-size, 24px);
	}
	.fill path {
		fill: var(--star);
	}
	/* While previewing, show the hovered value as a quieter fill. */
	.star.previewing .fill path {
		opacity: 0.75;
	}
	.zone {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 50%;
		padding: 0;
		border: none;
		background: none;
		cursor: pointer;
	}
	.zone.left {
		left: 0;
	}
	.zone.right {
		right: 0;
	}
	.zone:disabled {
		cursor: default;
	}
</style>
