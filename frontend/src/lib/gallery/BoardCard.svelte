<script lang="ts">
	// A board as Pinterest draws one: a collage of its three newest pictures
	// (one large, two stacked), its name, its count and how long since it was
	// saved to. `onedit` adds the pencil that renames or deletes it.
	import Glyph from '$lib/Glyph.svelte';
	import { pictureCount, shortAge } from './format';

	let {
		name,
		href,
		count,
		updated = null,
		covers = [],
		onedit
	}: {
		name: string;
		href: string;
		count: number;
		updated?: string | null;
		covers?: string[];
		onedit?: () => void;
	} = $props();
</script>

<div class="board">
	<a class="link" {href}>
		<span class="collage">
			{#each [0, 1, 2] as slot (slot)}
				<span class="cell cell-{slot}">
					{#if covers[slot]}
						<img src={covers[slot]} alt="" loading="lazy" decoding="async" />
					{/if}
				</span>
			{/each}
		</span>
		<span class="name">{name}</span>
		<span class="meta">
			{pictureCount(count)}{#if updated}<span class="age">{shortAge(updated)}</span>{/if}
		</span>
	</a>
	{#if onedit}
		<button type="button" class="edit" title="Edit board" aria-label="Edit board {name}" onclick={onedit}>
			<Glyph name="edit" />
		</button>
	{/if}
</div>

<style>
	.board {
		position: relative;
		min-width: 0;
	}
	.link {
		display: block;
		color: var(--text);
		text-decoration: none;
		border-radius: 16px;
	}
	.link:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
	}
	.collage {
		display: grid;
		grid-template-columns: 2fr 1fr;
		grid-template-rows: 1fr 1fr;
		gap: 2px;
		aspect-ratio: 3 / 2;
		border-radius: 16px;
		overflow: hidden;
		background: var(--bg);
	}
	.cell {
		display: block;
		background: var(--bg-panel);
		overflow: hidden;
		transition: background var(--motion-fast) var(--ease-out);
	}
	.cell-0 {
		grid-row: 1 / 3;
	}
	.link:hover .cell {
		background: var(--bg-hover);
	}
	.link:hover img {
		filter: brightness(0.85);
	}
	img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		transition: filter var(--motion-fast) var(--ease-out);
	}
	.name {
		display: block;
		margin-top: 9px;
		font-size: 17px;
		font-weight: 700;
		letter-spacing: -0.2px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.meta {
		display: flex;
		gap: 9px;
		margin-top: 2px;
		font-size: 13px;
	}
	.age {
		color: var(--text-muted);
	}
	.edit {
		position: absolute;
		top: 10px;
		right: 10px;
		width: 36px;
		height: 36px;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 50%;
		background: var(--bg);
		color: var(--text);
		opacity: 0;
		cursor: pointer;
		transition: opacity var(--motion-fast) var(--ease-out), background var(--motion-fast) var(--ease-out);
	}
	.edit :global(svg) {
		width: 16px;
		height: 16px;
	}
	.board:hover .edit,
	.edit:focus-visible {
		opacity: 1;
	}
	.edit:hover {
		background: var(--bg-hover);
	}
	@media (hover: none) {
		.edit {
			opacity: 1;
		}
	}
	@media (max-width: 760px) {
		.name {
			font-size: var(--m-row-text);
		}
		.meta {
			font-size: var(--m-meta);
		}
		.edit {
			width: 44px;
			height: 44px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.cell,
		img,
		.edit {
			transition: none;
		}
	}
</style>
