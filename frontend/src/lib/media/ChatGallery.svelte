<script lang="ts">
	import type { MediaGallery } from '$lib/api';

	// Numbered so "use image 3" is unambiguous; clicking a thumb pre-fills the
	// chat input (via onpick) rather than auto-sending.
	let {
		gallery,
		onpick
	}: {
		gallery: MediaGallery;
		onpick?: (text: string) => void;
	} = $props();
</script>

<div class="gallery" role="group" aria-label="Image results">
	{#each gallery.images as image, index (image.url)}
		<button
			type="button"
			class="shot"
			title={`Use image ${index + 1}`}
			onclick={() =>
				onpick?.(
					`Use image ${index + 1} as the ${gallery.kind === 'cover' ? 'cover' : 'backdrop'}`
				)}
		>
			<img src={image.thumb} alt="Result {index + 1}" loading="lazy" />
			<span class="num">{index + 1}</span>
		</button>
	{/each}
</div>

<style>
	.gallery {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
		gap: 6px;
		margin: 8px 0 4px;
	}
	.shot {
		position: relative;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-panel);
		overflow: hidden;
		aspect-ratio: 16 / 10;
		cursor: pointer;
		transition: border-color var(--motion-fast);
	}
	.shot:hover {
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.shot img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.num {
		position: absolute;
		left: 5px;
		bottom: 5px;
		min-width: 18px;
		padding: 1px 5px;
		border-radius: 6px;
		background: color-mix(in srgb, #000 62%, transparent);
		color: #fff;
		font-size: 11px;
		text-align: center;
	}
</style>
