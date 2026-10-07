<script lang="ts">
	// The Paper library: folders and documents, a folder at a time, laid out
	// as the iPad library's list.
	//
	// Read-only by design. Documents are created and drawn with a Pencil in the
	// native iPad app; this view exists so the same work is reachable from a
	// laptop, from search, and from OMNIA. Nothing here writes: no menus, and
	// the star only shows what the iPad starred.
	//
	// A folder is a query parameter on /paper (`?folder=paper%2FWork`), never a
	// pathname. The layout remounts the page per pathname; a query change keeps
	// this component and the library it already holds, so opening a folder is
	// a plain link: one history entry, and the tab follows it. Browser Back
	// walks back through the folders that were opened. The back chip goes up
	// to the parent instead, wherever the folder was entered from: a folder
	// OMNIA opened has no parent in history to go back to.
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api } from '$lib/api';
	import type { PaperNotebook, PaperSearchHit } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { createLiveList } from '$lib/media/livelist.svelte';
	import {
		FILTER_KEY,
		SORT_KEY,
		arrange,
		boardName,
		buildLibraryTree,
		childrenOf,
		coverAspect,
		findFolder,
		folderColorHex,
		formatLibraryDate,
		itemName,
		paperColorHex,
		parentFolder,
		parseFolderIcon,
		parseLibraryFilter,
		parseLibrarySort,
		type LibraryFilter,
		type LibraryItem,
		type LibrarySort
	} from '$lib/paperlibrary';
	import {
		folderHref,
		folderTitleFromPath,
		notebookHref,
		notebookTitleFromPath,
		parseFolderParam
	} from '$lib/paperurl';
	import { omniaContext } from '$lib/stores.svelte';
	import type { Snapshot } from './$types';

	let { data } = $props();

	// A page saved on the iPad, or a folder made, renamed or starred there,
	// lands here as a `paper` live event. Coalesce a burst — a sync pushing
	// twenty pages is one library change.
	const list = createLiveList(() => api.paperLibrary(), {
		match: (d) => d.type === 'paper',
		initial: untrack(() => data.library)
	});
	let library = $derived(list.data);
	let tree = $derived(buildLibraryTree(library));

	// --- where we are ---------------------------------------------------------

	let folderPath = $derived(parseFolderParam(page.url.searchParams));
	let atRoot = $derived(folderPath === 'paper');
	let folder = $derived(atRoot ? null : findFolder(tree, folderPath));
	let missing = $derived(!atRoot && !!library && !folder);
	let upHref = $derived(folderHref(folder ? folder.parent : parentFolder(folderPath)));
	let upName = $derived.by(() => {
		const up = folder ? folder.parent : parentFolder(folderPath);
		return up === 'paper' ? 'Documents' : (findFolder(tree, up)?.name ?? 'Back');
	});

	// The folder on screen moved (renamed or moved on the iPad): the URL still
	// names its old path. Follow it by id, in place of the old entry, rather
	// than state that it is gone. Only a folder seen at this very URL is
	// followed; a link to a path that was never there stays a dead end.
	let seen: { path: string; id: string } | null = null;
	$effect(() => {
		if (folder) {
			seen = folder.id ? { path: folderPath, id: folder.id } : null;
			return;
		}
		if (!missing || !seen || seen.path !== folderPath) return;
		const moved = tree.byId.get(seen.id);
		if (moved) void goto(folderHref(moved.path), { replaceState: true, noScroll: true, keepFocus: true });
	});

	// --- sort and filter ------------------------------------------------------

	// Per device, like the iPad's own choice, under the same key names.
	function stored(key: string): string | null {
		try {
			return localStorage.getItem(key);
		} catch {
			return null;
		}
	}
	let sort: LibrarySort = $state(parseLibrarySort(stored(SORT_KEY)));
	let filter: LibraryFilter = $state(parseLibraryFilter(stored(FILTER_KEY)));
	$effect(() => {
		try {
			localStorage.setItem(SORT_KEY, sort);
			localStorage.setItem(FILTER_KEY, filter);
		} catch {
			// Storage unavailable — the choice holds for this visit.
		}
	});

	const SORTS: { value: LibrarySort; label: string }[] = [
		{ value: 'date', label: 'Date' },
		{ value: 'name', label: 'Name' },
		{ value: 'type', label: 'Type' }
	];
	const FILTERS: { value: LibraryFilter; label: string; none: string }[] = [
		{ value: 'all', label: 'All', none: '' },
		{ value: 'folders', label: 'Folders', none: 'No folders.' },
		{ value: 'notebooks', label: 'Notebooks', none: 'No notebooks.' },
		{ value: 'whiteboards', label: 'Whiteboards', none: 'No whiteboards.' }
	];

	let rows = $derived(arrange(childrenOf(tree, folder?.path ?? folderPath), sort, filter));
	let emptyNote = $derived(
		filter !== 'all'
			? (FILTERS.find((f) => f.value === filter)?.none ?? '')
			: atRoot
				? 'No documents yet.'
				: 'This folder is empty.'
	);

	// --- scroll ---------------------------------------------------------------

	// The query changes under a mounted page, so nothing resets the scroller
	// by itself: a folder opens at its top. Back and Forward put it where it
	// was — SvelteKit restores a snapshot after this effect has run — and so
	// does coming back from a notebook, which remounts the page.
	let scroller: HTMLElement | undefined = $state();
	$effect(() => {
		void folderPath;
		untrack(() => {
			if (scroller) scroller.scrollTop = 0;
		});
	});
	export const snapshot: Snapshot<number> = {
		capture: () => scroller?.scrollTop ?? 0,
		restore: (top) => {
			if (scroller) scroller.scrollTop = top;
		}
	};

	// What OMNIA sees when asked "what's on here?". The layout sets it per
	// pathname; a folder is only a query change, so this keeps it truthful.
	$effect(() => {
		omniaContext.view = folder ? `Paper · ${folder.name} (${folder.path})` : 'Paper';
	});

	// --- handwriting search ---------------------------------------------------

	let q = $state('');
	let hits: PaperSearchHit[] = $state([]);
	let searching = $state(false);
	let searchNote = $state('');

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

	const hitTitle = (hit: PaperSearchHit) =>
		hit.title ||
		(hit.kind === 'whiteboard' && typeof hit.position === 'number'
			? boardName({ title: null }, hit.position)
			: notebookTitleFromPath(hit.notebook));

	$effect(() => list.listen());

	$effect(() => {
		if (list.failed && !library) void list.load();
	});

	// --- rows -----------------------------------------------------------------

	// The thumbnail slot, and a cover box of the document's own shape inside
	// it, so a row never jumps when its render arrives.
	const THUMB = { width: 72, height: 56 };
	function coverBox(notebook: PaperNotebook): string {
		const ratio = coverAspect(notebook);
		const wide = ratio >= THUMB.width / THUMB.height;
		return wide ? `width: 100%; aspect-ratio: ${ratio}` : `height: 100%; aspect-ratio: ${ratio}`;
	}

	const itemHref = (item: LibraryItem) =>
		item.kind === 'folder' ? folderHref(item.path) : notebookHref(item.path);
	const itemFavorite = (item: LibraryItem) =>
		item.kind === 'folder' ? item.folder.favorite : !!item.notebook.favorite;
	const itemDate = (item: LibraryItem) =>
		formatLibraryDate(item.kind === 'folder' ? item.folder.modified : item.notebook.modified);
</script>

<svelte:head><title>{folder ? `${folder.name} — Paper` : 'Paper — EXSCALIA'}</title></svelte:head>

<div class="paper-page" bind:this={scroller}>
	<header class="head">
		{#if !atRoot}
			<div class="lead">
				<a class="chip" href={upHref} title={upName} aria-label="Back to {upName}">
					<Glyph name="chevron-left" />
				</a>
			</div>
			<h1 class="folder-title">{folder?.name ?? folderTitleFromPath(folderPath)}</h1>
		{:else}
			<h1 class="large-title">Documents</h1>
		{/if}
		<form class="search" role="search" onsubmit={(event) => event.preventDefault()}>
			<Glyph name="search" />
			<input
				type="search"
				placeholder="Search handwriting"
				aria-label="Search handwriting"
				bind:value={q}
			/>
		</form>
	</header>

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
							<span class="hit-title">{hitTitle(hit)}</span>
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
	{:else if missing}
		<p class="state">This folder is not in the vault.</p>
	{:else}
		<div class="controls">
			<label class="filter" title="Filter">
				<Glyph name="filter" />
				<select bind:value={filter} aria-label="Filter">
					{#each FILTERS as option (option.value)}
						<option value={option.value}>{option.label}</option>
					{/each}
				</select>
			</label>
			<div class="sort" role="group" aria-label="Sort">
				{#each SORTS as option (option.value)}
					<button
						type="button"
						title="Sort by {option.label.toLowerCase()}"
						aria-pressed={sort === option.value}
						onclick={() => (sort = option.value)}>{option.label}</button
					>
				{/each}
			</div>
		</div>

		{#if rows.length === 0}
			<p class="state">{emptyNote}</p>
		{:else}
			<ul class="rows">
				{#each rows as item (item.kind + item.path)}
					<li>
						<a class="row" href={itemHref(item)}>
							<span class="thumb" aria-hidden="true">
								{#if item.kind === 'folder'}
									{@render folderArt(item.folder.color, item.folder.icon)}
								{:else}
									<span
										class="cover"
										style="{coverBox(item.notebook)}; background: {paperColorHex(
											item.notebook.paperColor
										)}"
									>
										{#if item.notebook.cover_url}
											<img src={item.notebook.cover_url} alt="" loading="lazy" decoding="async" />
										{:else}
											<span class="cover-empty"><Glyph name="paper" /></span>
										{/if}
									</span>
								{/if}
							</span>
							<span class="text">
								<span class="name">{itemName(item)}</span>
								<span class="date">{itemDate(item)}</span>
							</span>
							<span class="mark">
								{#if itemFavorite(item)}
									<span class="star" title="Favorite"
										><Glyph name="star" filled /><span class="sr">Favorite</span></span
									>
								{/if}
							</span>
							<span class="mark go">
								{#if item.kind === 'folder'}<Glyph name="chevron" />{/if}
							</span>
						</a>
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
</div>

<!-- A folder drawn in its own colour: the colour is the owner's content, as
     a cover is. Its emoji, when it has one, sits on the front; a symbol icon
     is drawn by the iPad only, so here the folder stays plain. -->
{#snippet folderArt(color: string, icon: string | null)}
	{@const mark = parseFolderIcon(icon)}
	<span class="folder-art" style="--folder: {folderColorHex(color)}">
		<svg viewBox="0 0 64 50">
			<path
				class="folder-back"
				d="M0 5a4 4 0 0 1 4-4h17.4a4 4 0 0 1 3 1.4L28 6.5h32a4 4 0 0 1 4 4V46a4 4 0 0 1-4 4H4a4 4 0 0 1-4-4z"
			/>
			<rect class="folder-front" x="0" y="11" width="64" height="39" rx="4" />
		</svg>
		{#if mark?.type === 'emoji'}<span class="folder-emoji">{mark.text}</span>{/if}
	</span>
{/snippet}

<style>
	.paper-page {
		/* The layout's route view is a flex column: an auto margin here would
		 * absorb the cross-axis space and collapse the page to content width,
		 * so the centring at very wide viewports is done with padding. */
		width: 100%;
		padding: 12px max(28px, calc((100% - 1400px) / 2)) 48px;
		overflow-y: auto;
		flex: 1;
		min-height: 0;
	}

	/* The iPad library's header: a back chip and the folder's name centred
	 * in a folder; the large title at the top level; search at the right. */
	.head {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
		grid-template-areas:
			'lead title search'
			'large large large';
		align-items: center;
		column-gap: 12px;
	}
	.lead {
		grid-area: lead;
		display: flex;
	}
	.chip {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 40px;
		height: 40px;
		border-radius: 50%;
		background: var(--bg-panel);
		color: var(--text);
		transition: background-color var(--motion-fast);
	}
	.chip:hover {
		background: var(--bg-hover);
	}
	.chip :global(svg) {
		width: 20px;
		height: 20px;
	}
	.folder-title {
		grid-area: title;
		max-width: min(52vw, 640px);
		margin: 0;
		overflow: hidden;
		font-size: 16px;
		font-weight: 600;
		text-align: center;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.large-title {
		grid-area: large;
		margin: 10px 0 4px;
		font-size: 30px;
		font-weight: 700;
		letter-spacing: -0.3px;
	}
	.search {
		grid-area: search;
		justify-self: end;
		display: flex;
		align-items: center;
		gap: 8px;
		height: 40px;
		padding: 0 14px;
		border: 1px solid transparent;
		border-radius: 999px;
		background: var(--bg-panel);
		color: var(--text-muted);
		transition: border-color var(--motion-fast);
	}
	.search:focus-within {
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.search :global(svg) {
		flex: none;
		width: 16px;
		height: 16px;
	}
	.search input {
		width: min(240px, 30vw);
		padding: 0;
		border: none;
		background: none;
		color: var(--text);
		font-family: inherit;
		font-size: 13px;
	}
	.search input:focus {
		outline: none;
	}

	.controls {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin: 10px 0 4px;
	}
	.filter {
		display: flex;
		align-items: center;
		gap: 6px;
		color: var(--text);
		cursor: pointer;
	}
	.filter :global(svg) {
		width: 20px;
		height: 20px;
	}
	.filter select {
		appearance: none;
		height: 36px;
		padding: 0 6px;
		border: none;
		border-radius: 8px;
		background: none;
		color: var(--text);
		font-family: inherit;
		font-size: 15px;
		font-weight: 600;
		cursor: pointer;
		transition: background-color var(--motion-fast);
	}
	.filter select:hover {
		background: var(--bg-hover);
	}
	.sort {
		display: flex;
		gap: 2px;
		padding: 2px;
		border-radius: 999px;
		background: var(--bg-panel);
	}
	.sort button {
		height: 30px;
		padding: 0 12px;
		border: none;
		border-radius: 999px;
		background: none;
		color: var(--text-muted);
		font-family: inherit;
		font-size: 13px;
		cursor: pointer;
		transition:
			background-color var(--motion-fast),
			color var(--motion-fast);
	}
	.sort button:hover {
		color: var(--text);
	}
	.sort button[aria-pressed='true'] {
		background: var(--bg);
		color: var(--text);
		box-shadow: 0 0 0 1px var(--border);
	}

	/* Rows separated by hairlines, as in the iPad list. */
	.rows {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 16px;
		min-height: 82px;
		padding: 12px 4px;
		border-bottom: 1px solid var(--border);
		color: var(--text);
		text-decoration: none;
		/* Feedback is colour only — nothing moves. */
		transition: background-color var(--motion-fast);
	}
	.row:hover {
		background: var(--bg-hover);
	}
	.thumb {
		display: flex;
		flex: none;
		align-items: center;
		justify-content: center;
		width: 72px;
		height: 56px;
	}
	.cover {
		display: block;
		max-width: 100%;
		max-height: 100%;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 4px;
		box-shadow: 0 1px 2px rgb(0 0 0 / 0.18);
	}
	.cover img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: contain;
	}
	.cover-empty {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
		/* On paper of any colour, a mid grey reads. */
		color: rgb(128 128 128 / 0.6);
	}
	.cover-empty :global(svg) {
		width: 18px;
		height: 18px;
	}
	.folder-art {
		position: relative;
		display: block;
		width: 64px;
		height: 50px;
	}
	.folder-art svg {
		display: block;
		width: 100%;
		height: 100%;
	}
	.folder-back {
		fill: color-mix(in srgb, var(--folder) 78%, #000);
	}
	.folder-front {
		fill: var(--folder);
	}
	.folder-emoji {
		position: absolute;
		inset: 11px 0 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: 22px;
		line-height: 1;
	}
	.text {
		display: flex;
		flex: 1;
		flex-direction: column;
		gap: 3px;
		min-width: 0;
	}
	.name {
		overflow: hidden;
		font-size: 15px;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.date {
		color: var(--text-muted);
		font-size: 12px;
	}
	.mark {
		display: flex;
		flex: none;
		align-items: center;
		justify-content: center;
		width: 24px;
		color: var(--text);
	}
	.mark :global(svg) {
		width: 18px;
		height: 18px;
	}
	.go {
		width: 16px;
		color: var(--text-muted);
	}
	.go :global(svg) {
		width: 16px;
		height: 16px;
	}
	.star {
		display: flex;
	}
	.sr {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip-path: inset(50%);
		white-space: nowrap;
	}

	.state {
		padding: 28px 10px;
		color: var(--text-muted);
		font-size: 12px;
		text-align: center;
	}
	.hits {
		margin: 14px 0 0;
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

	@media (prefers-reduced-motion: reduce) {
		.chip,
		.search,
		.filter select,
		.sort button,
		.row {
			transition: none;
		}
	}

	@media (max-width: 760px) {
		.paper-page {
			padding: 8px var(--m-gutter) 24px;
		}
		.head {
			grid-template-columns: 44px minmax(0, 1fr) 44px;
			grid-template-areas:
				'lead title .'
				'large large large'
				'search search search';
		}
		.chip {
			width: 44px;
			height: 44px;
		}
		.chip :global(svg) {
			width: var(--m-icon-bar);
			height: var(--m-icon-bar);
		}
		.folder-title {
			max-width: none;
			font-size: var(--m-row-text);
		}
		.large-title {
			margin: 4px 0 0;
			font-size: var(--m-title);
			letter-spacing: -0.2px;
		}
		/* 16px or iOS zooms the page in on focus, and 44px is the tap floor. */
		.search {
			justify-self: stretch;
			height: 44px;
			margin-top: 10px;
		}
		.search input {
			width: 100%;
			font-size: 16px;
		}
		.controls {
			margin-top: 6px;
		}
		.filter :global(svg) {
			width: var(--m-icon);
			height: var(--m-icon);
		}
		.filter select {
			height: 44px;
			font-size: 16px;
		}
		.sort {
			padding: 0;
		}
		.sort button {
			height: 44px;
			padding: 0 14px;
			font-size: var(--m-meta);
		}
		.row {
			gap: 12px;
			min-height: 68px;
			padding: 10px 2px;
		}
		.thumb {
			width: 56px;
			height: 44px;
		}
		.folder-art {
			width: 52px;
			height: 41px;
		}
		.folder-emoji {
			inset: 9px 0 0;
			font-size: 18px;
		}
		.name {
			font-size: var(--m-row-text);
		}
		.date {
			font-size: var(--m-meta);
		}
		.state {
			font-size: var(--m-meta);
		}
		.hit-title {
			font-size: var(--m-row-text);
		}
		.hit-book,
		.hit-snippet {
			font-size: var(--m-meta);
		}
	}
</style>
