<script lang="ts">
	// One picture in a grid: the thumbnail at its stored shape over its
	// average colour, so the tile is the right size and colour before the
	// image arrives; the title under it when there is one.
	import { pinHref } from './paths';
	import { tileRatio } from './masonry';
	import type { PinCard } from './types';

	let { pin }: { pin: PinCard } = $props();
	let loaded = $state(false);
</script>

<a class="tile" href={pinHref(pin.path)} title={pin.title || undefined} data-path={pin.path}>
	<span
		class="frame"
		style:aspect-ratio={`1 / ${tileRatio(pin)}`}
		style:background-color={pin.color ?? 'var(--bg-panel)'}
	>
		<img
			src={pin.thumb}
			alt={pin.title}
			loading="lazy"
			decoding="async"
			class:loaded
			onload={() => (loaded = true)}
		/>
	</span>
	{#if pin.title}
		<span class="caption">{pin.title}</span>
	{/if}
</a>

<style>
	.tile {
		display: block;
		color: var(--text);
		text-decoration: none;
		border-radius: 16px;
	}
	.tile:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
	}
	.frame {
		position: relative;
		display: block;
		width: 100%;
		overflow: hidden;
		border-radius: 16px;
	}
	.frame::after {
		content: '';
		position: absolute;
		inset: 0;
		background: transparent;
		transition: background var(--motion-fast) var(--ease-out);
	}
	.tile:hover .frame::after {
		background: color-mix(in srgb, #000 22%, transparent);
	}
	img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: top;
		opacity: 0;
		transition: opacity var(--motion-base) var(--ease-out);
	}
	img.loaded {
		opacity: 1;
	}
	.caption {
		display: block;
		padding: 7px 6px 0;
		font-size: 13px;
		font-weight: 600;
		line-height: 1.3;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	@media (prefers-reduced-motion: reduce) {
		img,
		.frame::after {
			transition: none;
		}
	}
</style>
