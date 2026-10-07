<script lang="ts">
	import { encodePath } from '$lib/api';
	import type { MediaItem } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { TYPE_WORD, cardProgress, displayTitle, starText } from '$lib/media/helpers';

	// compact = poster-first hub rows (Letterboxd style): no title/creator text,
	// just the artwork with the type glyph + rating beneath.
	let { item, compact = false }: { item: MediaItem; compact?: boolean } = $props();

	let progress = $derived(cardProgress(item));
</script>

<a class="card" class:compact href={'/media/' + encodePath(item.path)} title={displayTitle(item)}>
	<div class="cover">
		{#if item.cover}
			<img src={item.cover} alt="" loading="lazy" />
		{:else}
			<span>{TYPE_WORD[item.media_type]}</span>
		{/if}
	</div>
	{#if compact}
		<div class="compact-meta">
			<span class="type-glyph" title={TYPE_WORD[item.media_type]}>
				<Glyph name={item.media_type} />
			</span>
			{#if item.rating != null}<span class="score">{starText(item.rating)}</span>{/if}
		</div>
	{:else}
		<div class="card-title">
			<span class="type-glyph" title={TYPE_WORD[item.media_type]}>
				<Glyph name={item.media_type} />
			</span>{displayTitle(item)}
		</div>
		{#if item.creator || item.year}
			<div class="card-sub">
				{[item.creator, item.year].filter(Boolean).join(' · ')}
			</div>
		{/if}
		<div class="card-meta">
			{#if item.rating != null}<span class="score">{starText(item.rating)}</span>{/if}
			{#if progress}<span>{progress}</span>{/if}
		</div>
	{/if}
</a>

<style>
	.card {
		display: block;
		text-decoration: none;
		color: var(--text);
		min-width: 0;
	}
	.cover {
		aspect-ratio: 2 / 3;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-panel);
		box-shadow: 0 3px 10px color-mix(in srgb, var(--text) 4%, transparent);
		transition: border-color var(--motion-fast);
	}
	.cover img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.cover span {
		font-size: 12px;
		color: var(--text-muted);
	}
	.card:hover .cover {
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
	}
	.card-title {
		margin-top: 8px;
		font-size: 14px;
		font-weight: 600;
		line-height: 1.3;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.card-sub {
		margin-top: 3px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.card-meta {
		margin-top: 3px;
		display: flex;
		gap: 8px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.score {
		color: var(--star);
	}
	.compact-meta {
		margin-top: 5px;
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--text-muted);
	}
	/* The same monochrome type glyphs as the library filters, so a mixed grid
	   reads at a glance. */
	.type-glyph {
		display: inline-flex;
		vertical-align: -2px;
		margin-right: 6px;
		color: var(--text-muted);
	}
	.type-glyph :global(svg) {
		width: 13px;
		height: 13px;
	}
	.compact-meta .type-glyph {
		margin-right: 0;
	}
</style>
