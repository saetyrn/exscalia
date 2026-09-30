<script lang="ts">
	// The Paper library: every handwritten notebook, as covers.
	//
	// Read-only by design. Notebooks are created and drawn with a Pencil in the
	// native iPad app; this view exists so the same work is reachable from a
	// laptop, from search, and from OMNIA. Nothing here writes.
	import { untrack } from 'svelte';
	import { api } from '$lib/api';
	import type { PaperNotebook, PaperSearchHit } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { createLiveList } from '$lib/media/livelist.svelte';
	import { notebookHref, notebookTitleFromPath } from '$lib/paperurl';
	import { threadTimeLabel } from '$lib/stores.svelte';

	let { data } = $props();

	// A page saved on the iPad lands here as a `paper` live event. Coalesce a
	// burst — a sync pushing twenty pages is one library change.
	const list = createLiveList(() => api.paperLibrary(), {
		match: (d) => d.type === 'paper',
		initial: untrack(() => data.library)
	});
	let library = $derived(list.data);

	let q = $state('');
	let hits: PaperSearchHit[] = $state([]);
	let searching = $state(false);
	let searchNote = $state('');

	let notebooks = $derived(library?.notebooks ?? []);

	// Handwriting search is a server round trip against the recognition index,
	// not a filter over what is already on screen — a notebook's title says
	// almost nothing about what is written inside it. The debounce cannot
	// recall a request already sent, so every change of query takes a ticket
	// and only a request for the current one may paint hits or end the
	// spinner.
	let searchTimer: ReturnType<typeof setTimeout> | null = null;
	let searchSeq = 0;
	$effect(() => {
		const needle = q.trim();
		const mine = ++searchSeq;
		if (searchTimer) clearTimeout(searchTimer);
		if (!needle) {
			hits = [];
			searching = false;
			searchNote = '';
			return;
		}
		searching = true;
		searchTimer = setTimeout(async () => {
			searchTimer = null;
			try {
				const found = await api.paperSearch(needle);
				if (mine !== searchSeq) return;
				hits = found;
				searchNote = '';
			} catch {
				if (mine !== searchSeq) return;
				hits = [];
				searchNote = 'Search is unavailable right now.';
			} finally {
				if (mine === searchSeq) searching = false;
			}
		}, 250);
		return () => {
			if (searchTimer) clearTimeout(searchTimer);
		};
	});

	$effect(() => list.listen());

	$effect(() => {
		if (list.failed && !library) void list.load();
	});

	// Favourites are lifted out of their folders rather than repeated in them:
	// one notebook, one card, so the count under a folder label is the truth.
	let favourites = $derived(notebooks.filter((n) => n.favorite));
	let sections = $derived.by(() => {
		const rest = notebooks.filter((n) => !n.favorite);
		const groups = new Map<string, PaperNotebook[]>();
		for (const notebook of rest) {
			const folder = (notebook.folder ?? '').replace(/^\/+|\/+$/g, '');
			const list = groups.get(folder);
			if (list) list.push(notebook);
			else groups.set(folder, [notebook]);
		}
		return [...groups.entries()]
			.sort((a, b) => (a[0] === '' ? -1 : b[0] === '' ? 1 : a[0].localeCompare(b[0])))
			.map(([folder, items]) => ({
				folder,
				label: folder || 'Notebooks',
				items: items.sort((a, b) => (b.modified ?? '').localeCompare(a.modified ?? ''))
			}));
	});

	// Page proportions, so a cover reserves the right box before it loads and
	// a missing render leaves a notebook-shaped hole instead of a line.
	const RATIOS: Record<string, string> = {
		a4: '595 / 842',
		letter: '612 / 792',
		ipad: '1024 / 1366',
		infinite: '1024 / 1366'
	};
	const ratioOf = (size?: string) => RATIOS[(size ?? '').toLowerCase()] ?? RATIOS.a4;

	const titleOf = (notebook: PaperNotebook) =>
		notebook.title || notebookTitleFromPath(notebook.path);
</script>

<svelte:head><title>Paper — EXSCALIA</title></svelte:head>

<div class="paper-page">
	<div class="topbar">
		<h1>Paper</h1>
		<form class="search-box" onsubmit={(event) => event.preventDefault()}>
			<input
				type="search"
				placeholder="Search handwriting…"
				aria-label="Search handwriting"
				bind:value={q}
			/>
		</form>
	</div>

	<p class="hint">Write in Paper on iPad.</p>

	{#if q.trim()}
		{#if searching}
			<p class="state">Searching…</p>
		{:else if searchNote}
			<p class="state">{searchNote}</p>
		{:else if hits.length === 0}
			<p class="state">No handwriting matches “{q.trim()}”.</p>
		{:else}
			<ul class="hits">
				{#each hits as hit (hit.path)}
					<li>
						<a href={hit.durable_url ?? hit.url ?? notebookHref(hit.notebook)}>
							<span class="hit-title"
								>{hit.title || notebookTitleFromPath(hit.notebook)}</span
							>
							<span class="hit-book"
								>{hit.notebook_title || notebookTitleFromPath(hit.notebook)}</span
							>
						</a>
						{#if hit.snippet}<p class="hit-snippet">{hit.snippet}</p>{/if}
					</li>
				{/each}
			</ul>
		{/if}
	{:else if list.failed && !library}
		<p class="state">Could not load Paper — retrying on the next change.</p>
	{:else if notebooks.length === 0}
		<p class="state">No notebooks yet.</p>
	{:else}
		{#if favourites.length > 0}
			<section class="shelf">
				<h2>Favourites</h2>
				<div class="grid">
					{#each favourites as notebook (notebook.path)}
						{@render card(notebook)}
					{/each}
				</div>
			</section>
		{/if}
		{#each sections as section (section.folder)}
			<section class="shelf">
				<h2>{section.label}</h2>
				<div class="grid">
					{#each section.items as notebook (notebook.path)}
						{@render card(notebook)}
					{/each}
				</div>
			</section>
		{/each}
	{/if}
</div>

{#snippet card(notebook: PaperNotebook)}
	<a class="card" href={notebookHref(notebook.path)}>
		<span class="cover" style="aspect-ratio: {ratioOf(notebook.paperSize)}">
			{#if notebook.cover_url}
				<img src={notebook.cover_url} alt="" loading="lazy" decoding="async" />
			{:else}
				<span class="cover-empty" aria-hidden="true"><Glyph name="paper" /></span>
			{/if}
		</span>
		<span class="card-title">{titleOf(notebook)}</span>
		<span class="card-meta">
			{#if notebook.modified}{threadTimeLabel(notebook.modified)}{/if}
			{#if notebook.page_count}<span class="pages"
					>{notebook.page_count} page{notebook.page_count === 1 ? '' : 's'}</span
				>{/if}
		</span>
	</a>
{/snippet}

<style>
	.paper-page {
		/* The layout's route view is a flex column: an auto margin here would
		 * absorb the cross-axis space and collapse the page to content width,
		 * so the centring at very wide viewports is done with padding. */
		width: 100%;
		padding: 14px max(28px, calc((100% - 1760px) / 2)) 48px;
		container-type: inline-size;
		overflow-y: auto;
		flex: 1;
		min-height: 0;
	}
	.topbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		margin-bottom: 4px;
	}
	h1 {
		margin: 0;
		font-size: 16px;
		font-weight: 600;
	}
	.search-box input {
		width: min(360px, 46vw);
		height: 32px;
		padding: 0 12px;
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
	.hint {
		margin: 0 0 18px;
		color: var(--text-muted);
		font-size: 12px;
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
	.shelf h2 {
		margin: 0 0 12px;
		padding-bottom: 6px;
		border-bottom: 1px solid var(--border);
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		gap: 24px 18px;
	}
	.card {
		display: flex;
		flex-direction: column;
		gap: 6px;
		min-width: 0;
		color: var(--text);
		text-decoration: none;
	}
	.cover {
		display: block;
		width: 100%;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--bg-panel);
		/* Feedback is colour and shadow only — nothing moves. */
		transition:
			border-color var(--motion-fast),
			box-shadow var(--motion-fast);
	}
	.card:hover .cover {
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
		box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 24%, transparent);
	}
	.cover img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: top center;
	}
	.cover-empty {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
		color: color-mix(in srgb, var(--text-muted) 55%, transparent);
	}
	.cover-empty :global(svg) {
		width: 28px;
		height: 28px;
	}
	.card-title {
		overflow: hidden;
		font-size: 13px;
		font-weight: 600;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.card-meta {
		display: flex;
		gap: 8px;
		color: var(--text-muted);
		font-size: 11px;
	}
	.hits {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.hits li {
		padding: 10px 0;
		border-bottom: 1px solid var(--border);
	}
	.hits a {
		display: flex;
		align-items: baseline;
		gap: 10px;
		color: var(--text);
		text-decoration: none;
	}
	.hits a:hover .hit-title {
		color: var(--accent);
	}
	.hit-title {
		font-size: 14px;
		font-weight: 600;
	}
	.hit-book {
		color: var(--text-muted);
		font-size: 12px;
	}
	.hit-snippet {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 12px;
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
		.paper-page {
			padding: 12px var(--m-gutter) 24px;
		}
		.topbar {
			flex-direction: column;
			align-items: stretch;
			gap: 10px;
		}
		h1 {
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		/* 16px or iOS zooms the page in on focus, and 44px is the tap floor. */
		.search-box input {
			width: 100%;
			height: 44px;
			font-size: 16px;
		}
		.hint {
			font-size: var(--m-meta);
		}
		.shelf h2 {
			font-size: var(--m-label);
			letter-spacing: 0.5px;
		}
		.card-title {
			font-size: var(--m-row-text);
		}
		.card-meta {
			font-size: var(--m-meta);
		}
	}
</style>
