<script lang="ts">
	import { untrack } from 'svelte';
	import { page } from '$app/state';
	import { api } from '$lib/api';
	import type { MediaItem, MediaStatus, MediaType } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import MediaCard from '$lib/media/MediaCard.svelte';
	import { openMediaAdd } from '$lib/media/adddialog.svelte';
	import { createLiveList, isMediaNoteEvent } from '$lib/media/livelist.svelte';

	let { data } = $props();

	// The hub stat tiles deep-link here with ?type=movie etc. The type chip
	// filters the loaded list client-side (see `visible`), so the unfiltered
	// SSR list already covers a deep link.
	const TYPES: MediaType[] = ['book', 'movie', 'tv', 'game'];
	const initialType = page.url.searchParams.get('type');

	let sort = $state('recent');

	// Only the sort order goes to the server. Sort changes, live refreshes and
	// retries can overlap: the list drops a response a newer load superseded
	// and keeps the last good items when one fails. A media note edited
	// elsewhere (another device, OMNIA, the editor) lands here as a live
	// `note` event; a burst of them is one refetch, like the boards page.
	//
	// The layout remounts the page per navigation, so plain init is safe; the
	// untrack mirrors the boards page and keeps the lint honest.
	const list = createLiveList<MediaItem[]>(() => api.mediaList({ sort }), {
		match: isMediaNoteEvent,
		initial: untrack(() => data.items)
	});
	let items = $derived(list.data ?? []);
	let typeFilter: '' | MediaType = $state(
		initialType && TYPES.includes(initialType as MediaType) ? (initialType as MediaType) : ''
	);
	let q = $state('');

	const TYPE_BUTTONS: { id: MediaType; label: string; noun: string }[] = [
		{ id: 'book', label: 'Books', noun: 'books' },
		{ id: 'movie', label: 'Movies', noun: 'movies' },
		{ id: 'tv', label: 'TV', noun: 'shows' },
		{ id: 'game', label: 'Games', noun: 'games' }
	];

	function setTypeFilter(next: '' | MediaType) {
		typeFilter = next === typeFilter ? '' : next;
	}

	$effect(() => {
		if (list.failed && items.length === 0) void list.load();
	});

	$effect(() => list.listen());

	// The search box and type chip filter what is already loaded; the + beside
	// the search carries the query into the add flow and looks it up. Watchlist
	// (planned) items live on their own page and never appear in the library.
	let visible = $derived.by(() => {
		const needle = q.trim().toLowerCase();
		return items.filter(
			(item) =>
				item.status !== 'planned' &&
				(!typeFilter || item.media_type === typeFilter) &&
				(!needle ||
					`${item.title} ${item.alias ?? ''} ${item.creator ?? ''}`.toLowerCase().includes(needle))
		);
	});

	// The library reads top-down: what you're in the middle of, then the
	// shelf of finished work (dropped items trail quietly when any exist).
	let inProgress = $derived(visible.filter((item) => item.status === 'active'));
	let finishedItems = $derived(visible.filter((item) => item.status === 'finished'));
	let droppedItems = $derived(visible.filter((item) => item.status === 'dropped'));

	// Why a non-empty library shows nothing: a search miss, a type with nothing
	// logged, or items that only sit on the watchlist.
	let emptyLine = $derived.by(() => {
		const needle = q.trim();
		if (needle) return `Nothing matches “${needle}” — press + to add it.`;
		const noun = TYPE_BUTTONS.find((t) => t.id === typeFilter)?.noun;
		if (!noun) return 'Everything here is on the watchlist.';
		return items.some((item) => item.media_type === typeFilter)
			? `Your ${noun} are all on the watchlist.`
			: `No ${noun} in the library yet.`;
	});

	// The + shares the sidebar's collective add dialog, seeded with the query.
	function openAddFromSearch() {
		openMediaAdd('active', q.trim());
	}
</script>

<div class="media-page">
	<h1 class="sr-only">Media library</h1>

	<div class="topbar">
		<div class="top-left">
			<select aria-label="Sort" title="Sort" bind:value={sort} onchange={() => void list.load()}>
				<option value="recent">Recent</option>
				<option value="title">Title</option>
				<option value="year">Year</option>
				<option value="rating">Rating</option>
				<option value="finished">Finished</option>
			</select>
		</div>
		<form
			class="search-box"
			onsubmit={(event) => {
				event.preventDefault();
			}}
		>
			<input type="search" placeholder="Search…" aria-label="Search media" bind:value={q} />
			<button
				type="button"
				class="search-plus"
				title={q.trim() ? `Add “${q.trim()}”` : 'Add media'}
				aria-label={q.trim() ? `Add ${q.trim()}` : 'Add media'}
				onclick={openAddFromSearch}
			>
				<Glyph name="plus" />
			</button>
		</form>
		<div class="top-right typebar" aria-label="Type filter">
			{#each TYPE_BUTTONS as t (t.id)}
				<button
					class="ticon"
					class:active={typeFilter === t.id}
					title={t.label}
					aria-label={t.label}
					aria-pressed={typeFilter === t.id}
					onclick={() => setTypeFilter(t.id)}
				>
					<Glyph name={t.id} />
					{#if typeFilter === t.id}<span>{t.label}</span>{/if}
				</button>
			{/each}
			{#if typeFilter}
				<button
					class="ticon"
					title="Clear type filter"
					aria-label="Clear type filter"
					onclick={() => setTypeFilter('')}
				>
					<Glyph name="x" />
				</button>
			{/if}
		</div>
	</div>

	{#if list.failed && items.length === 0}
		<p class="state">Could not load the media library — retrying on the next change.</p>
	{:else if items.length === 0}
		<p class="state">
			Nothing here yet. Ask OMNIA to log something you're reading, watching, or playing — or
			press + in the search bar.
		</p>
	{:else if visible.length === 0}
		<p class="state">{emptyLine}</p>
	{:else}
		{#if inProgress.length > 0}
			<section class="shelf">
				<div class="shelf-head">
					<h2>In progress</h2>
					<span class="shelf-count">{inProgress.length}</span>
				</div>
				<div class="grid">
					{#each inProgress as item (item.path)}
						<MediaCard {item} />
					{/each}
				</div>
			</section>
		{/if}
		{#if finishedItems.length > 0}
			<section class="shelf">
				<div class="shelf-head">
					<h2>Finished</h2>
					<span class="shelf-count">{finishedItems.length}</span>
				</div>
				<div class="grid">
					{#each finishedItems as item (item.path)}
						<MediaCard {item} />
					{/each}
				</div>
			</section>
		{/if}
		{#if droppedItems.length > 0}
			<section class="shelf">
				<div class="shelf-head">
					<h2>Dropped</h2>
					<span class="shelf-count">{droppedItems.length}</span>
				</div>
				<div class="grid">
					{#each droppedItems as item (item.path)}
						<MediaCard {item} />
					{/each}
				</div>
			</section>
		{/if}
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
		display: grid;
		grid-template-columns: 1fr minmax(200px, 440px) 1fr;
		align-items: center;
		gap: 10px;
		margin-bottom: 10px;
	}
	.top-left {
		display: flex;
		gap: 6px;
		justify-self: start;
	}
	.top-right {
		justify-self: end;
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
	.search-box {
		position: relative;
	}
	.search-box input {
		width: 100%;
		height: 32px;
		padding: 0 36px 0 12px;
		font-family: inherit;
		font-size: 13px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.search-box input:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	/* Symbol buttons are bare monochrome glyphs: no boxes, no fills — state
	   and hover speak through color alone. */
	.search-plus,
	.ticon {
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 0;
		border: none;
		background: none;
		color: var(--text-muted);
		font-family: inherit;
		cursor: pointer;
		transition: color var(--motion-fast);
	}
	.search-plus:hover,
	.ticon:hover {
		color: var(--text);
	}
	.ticon.active {
		color: var(--accent);
	}
	.search-plus {
		position: absolute;
		right: 3px;
		top: 3px;
		width: 26px;
		height: 26px;
	}
	.search-plus :global(svg) {
		width: 15px;
		height: 15px;
	}
	.typebar {
		display: flex;
		align-items: center;
		gap: 14px;
	}
	.ticon {
		gap: 7px;
		height: 30px;
		font-size: 12px;
	}
	.ticon :global(svg) {
		width: 17px;
		height: 17px;
		flex: none;
	}
	.state {
		padding: 28px 10px;
		color: var(--text-muted);
		font-size: 12px;
		text-align: center;
	}
	.shelf + .shelf {
		margin-top: 34px;
	}
	.shelf-head {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 12px;
		padding-bottom: 6px;
		border-bottom: 1px solid var(--border);
	}
	.shelf-head h2 {
		margin: 0;
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.shelf-count {
		font-size: 12px;
		color: color-mix(in srgb, var(--text-muted) 70%, transparent);
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		gap: 24px 18px;
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
			padding-inline: var(--m-gutter);
		}
		/* 16px or iOS zooms the page in on focus; the height goes with it, both
		   so the taller text fits and so the control is a real tap target. */
		.topbar select,
		.search-box input {
			height: 44px;
			font-size: 16px;
		}
		.topbar {
			grid-template-columns: 1fr;
		}
		.top-left,
		.top-right {
			justify-self: stretch;
		}
		.top-right {
			display: flex;
			justify-content: flex-end;
		}
	}
</style>
