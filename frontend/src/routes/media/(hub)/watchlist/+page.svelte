<script lang="ts">
	import { untrack } from 'svelte';
	import { api } from '$lib/api';
	import type { MediaItem } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { loadHub } from '$lib/mediahub.svelte';
	import MediaCard from '$lib/media/MediaCard.svelte';
	import { openMediaAdd } from '$lib/media/adddialog.svelte';
	import { statusLabel } from '$lib/media/helpers';
	import { notify } from '$lib/ui.svelte';

	let { data } = $props();

	let items: MediaItem[] | null = $state(untrack(() => data.items));
	let sort = $state('recent');
	let starting: string | null = $state(null);

	async function load() {
		try {
			items = await api.mediaList({ status: 'planned', sort });
		} catch {
			items = null;
		}
	}

	// One tap to take something off the list and into the middle of it. The
	// card's own tap target still goes to the item page, where the action bar
	// can log the first session; this is the shortcut for "I've begun".
	async function start(item: MediaItem) {
		if (starting) return;
		starting = item.path;
		try {
			const result = await api.mediaPatch(item.path, { status: 'active' });
			notify(result.summary + '.');
			await load();
			void loadHub().catch(() => {
				/* the last good payload stays on screen */
			});
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			starting = null;
		}
	}

	$effect(() => {
		if (items === null) void load();
	});

	let liveRefresh: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const onLive = (e: Event) => {
			const detail = (e as CustomEvent).detail as { type?: string; path?: string };
			if (detail?.type !== 'note' && detail?.type !== 'note_removed') return;
			if (!detail.path?.startsWith('media/')) return;
			if (liveRefresh) clearTimeout(liveRefresh);
			liveRefresh = setTimeout(() => {
				liveRefresh = null;
				void load();
			}, 600);
		};
		window.addEventListener('exscalia:live', onLive);
		return () => {
			window.removeEventListener('exscalia:live', onLive);
			if (liveRefresh) clearTimeout(liveRefresh);
		};
	});
</script>

<div class="media-page">
	<h1 class="sr-only">Watchlist</h1>

	<div class="topbar">
		<select aria-label="Sort" title="Sort" bind:value={sort} onchange={() => void load()}>
			<option value="recent">Recent</option>
			<option value="title">Title</option>
			<option value="year">Year</option>
		</select>
		<!-- Mobile only: the sidebar carries "Add to watchlist" on a laptop and
		     the sidebar is not there on a phone, which left the list with no
		     way to grow except through OMNIA. -->
		<button
			type="button"
			class="add"
			title="Add to watchlist"
			aria-label="Add to watchlist"
			onclick={() => openMediaAdd('planned')}
		>
			<Glyph name="plus" />
		</button>
	</div>

	{#if items === null}
		<p class="state">Loading watchlist…</p>
	{:else if items.length === 0}
		<p class="state">
			Nothing on the watchlist. Add something from the library's search, or ask OMNIA to put
			it on your watchlist.
		</p>
	{:else}
		<div class="grid">
			{#each items as item (item.path)}
				<div class="cell">
					<MediaCard {item} />
					<button
						type="button"
						class="start"
						title={`Start — ${statusLabel('active', item.media_type)}`}
						aria-label={`Start ${item.title}`}
						disabled={starting !== null}
						onclick={() => void start(item)}
					>
						Start
					</button>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.media-page {
		padding: 14px 28px 48px;
		max-width: 1760px;
		margin: 0 auto;
		container-type: inline-size;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}
	.topbar {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 12px;
		margin-bottom: 6px;
	}
	.topbar select {
		font-family: inherit;
		font-size: 12px;
		color: var(--text-muted);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 5px 8px;
		height: 32px;
	}
	.topbar select:hover {
		color: var(--text);
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
	}
	.add,
	.start {
		display: none;
	}
	.state {
		padding: 28px 10px;
		color: var(--text-muted);
		font-size: 12px;
		text-align: center;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		gap: 24px 18px;
		margin-top: 12px;
	}
	@container (max-width: 1049px) {
		.grid {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
	}
	@container (max-width: 799px) {
		.grid {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}
	}
	@container (max-width: 599px) {
		.grid {
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 18px 12px;
		}
	}
	@media (max-width: 760px) {
		.media-page {
			padding-inline: 14px;
			padding-bottom: 24px;
		}
		.add {
			display: grid;
			place-items: center;
			width: 44px;
			height: 44px;
			padding: 0;
			border: 0;
			background: none;
			color: var(--text-muted);
			cursor: pointer;
		}
		.add:active {
			color: var(--text);
			background: var(--bg-hover);
		}
		.add :global(svg) {
			width: 20px;
			height: 20px;
		}
		.cell {
			display: flex;
			flex-direction: column;
		}
		.start {
			display: block;
			width: 100%;
			min-height: 44px;
			margin-top: 8px;
			font-family: inherit;
			font-size: 13px;
			color: var(--text);
			background: transparent;
			border: 1px solid var(--border);
			border-radius: var(--radius);
			cursor: pointer;
		}
		.start:active {
			background: var(--bg-hover);
			border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
		}
		.start:disabled {
			opacity: 0.55;
			cursor: default;
		}
	}
</style>
