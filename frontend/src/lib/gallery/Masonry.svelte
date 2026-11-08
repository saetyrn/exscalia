<script lang="ts">
	// The Gallery's masonry grid (masonry.ts places the tiles). `onmore` is
	// asked for the next page when the end of the grid nears the viewport.
	import { columnCount, GAP, placeColumns } from './masonry';
	import PinTile from './PinTile.svelte';
	import type { PinCard } from './types';

	let { pins, onmore }: { pins: PinCard[]; onmore?: () => void } = $props();

	let width = $state(0);
	let columns = $derived(columnCount(width));
	let columnWidth = $derived(width > 0 ? (width - GAP * (columns - 1)) / columns : 220);
	let placed = $derived(placeColumns(pins, columns, columnWidth));
	let sentinel: HTMLElement | undefined = $state();

	$effect(() => {
		if (!onmore || !sentinel) return;
		const more = onmore;
		const observer = new IntersectionObserver(
			(entries) => {
				if (entries.some((e) => e.isIntersecting)) more();
			},
			{ rootMargin: '900px 0px' }
		);
		observer.observe(sentinel);
		return () => observer.disconnect();
	});
</script>

<div class="masonry" bind:clientWidth={width} style:--gap={`${GAP}px`}>
	{#each placed as column, c (c)}
		<div class="column">
			{#each column as i (pins[i].path)}
				<PinTile pin={pins[i]} />
			{/each}
		</div>
	{/each}
</div>
{#if onmore}
	<div class="sentinel" bind:this={sentinel}></div>
{/if}

<style>
	.masonry {
		display: flex;
		gap: var(--gap);
		align-items: flex-start;
		width: 100%;
	}
	.column {
		flex: 1 1 0;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: var(--gap);
	}
	.sentinel {
		height: 1px;
	}
	@media (max-width: 760px) {
		.masonry,
		.column {
			gap: 10px;
		}
	}
</style>
