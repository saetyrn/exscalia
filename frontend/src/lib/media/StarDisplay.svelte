<script lang="ts">
	import { starTitle } from '$lib/media/helpers';

	// Read-only five-star display with half steps. `value` is the stored 1–10
	// scale (1 = half a star, 10 = five); the interactive twin is StarRating.
	let { value = null }: { value?: number | null } = $props();

	const STAR = 'M12 2l2.9 6.6 7.1.6-5.4 4.7 1.6 7-6.2-3.7-6.2 3.7 1.6-7L2 9.2l7.1-.6z';
	const shown = $derived(value ?? 0);

	function portion(star: number): 0 | 1 | 2 {
		return Math.max(0, Math.min(2, shown - (star - 1) * 2)) as 0 | 1 | 2;
	}
</script>

{#if value != null && value >= 1}
	<span class="stars" title={starTitle(value)} aria-label={starTitle(value)}>
		{#each [1, 2, 3, 4, 5] as star (star)}
			{@const filled = portion(star)}
			<span class="star">
				<svg viewBox="0 0 24 24" class="base" aria-hidden="true"><path d={STAR} /></svg>
				{#if filled > 0}
					<span class="fill" style:width={filled === 1 ? '50%' : '100%'}>
						<svg viewBox="0 0 24 24" aria-hidden="true"><path d={STAR} /></svg>
					</span>
				{/if}
			</span>
		{/each}
	</span>
{/if}

<style>
	.stars {
		display: inline-flex;
		gap: 2px;
		vertical-align: middle;
	}
	.star {
		position: relative;
		width: var(--star-size, 14px);
		height: var(--star-size, 14px);
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
		width: var(--star-size, 14px);
		height: var(--star-size, 14px);
	}
	.fill path {
		fill: var(--star);
	}
</style>
