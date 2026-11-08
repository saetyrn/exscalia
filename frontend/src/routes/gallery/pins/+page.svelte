<script lang="ts">
	// The Gallery's grid: every picture, one board, or a search, under the
	// strip of boards. A board and a query are URL parameters
	// (/gallery/pins?board=art&q=castle), so switching keeps this component
	// and only refetches.
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import AddDialog from '$lib/gallery/AddDialog.svelte';
	import BoardDialog from '$lib/gallery/BoardDialog.svelte';
	import { newBoard } from '$lib/gallery/boards';
	import { isGalleryEvent, pictureDrop } from '$lib/gallery/drop';
	import GalleryHeader from '$lib/gallery/GalleryHeader.svelte';
	import Masonry from '$lib/gallery/Masonry.svelte';
	import { boardParam, pinsHref } from '$lib/gallery/paths';
	import { sendPictures } from '$lib/gallery/uploads.svelte';
	import UploadLine from '$lib/gallery/UploadLine.svelte';
	import type { PinCard } from '$lib/gallery/types';
	import { createLiveList } from '$lib/media/livelist.svelte';
	import { omniaContext } from '$lib/stores.svelte';
	import { notify } from '$lib/ui.svelte';

	const PAGE = 60;

	let board = $derived(boardParam(page.url.searchParams));
	let query = $derived(page.url.searchParams.get('q') ?? '');

	const overview = createLiveList(() => api.galleryOverview(), { match: isGalleryEvent });
	$effect(() => {
		void overview.load();
		return overview.listen();
	});
	let boards = $derived(overview.data?.boards ?? []);
	let current = $derived(boards.find((b) => b.name === board) ?? null);

	let pins: PinCard[] = $state([]);
	let next: number | null = $state(null);
	let failed = $state(false);
	let loaded = $state(false);
	let busy = false;
	let seq = 0;

	/** The first page, or as many as are on screen when `keep` (a live
	 * refresh must not scroll the reader back to the top). */
	async function reload(keep = false) {
		const mine = ++seq;
		busy = true;
		try {
			const limit = keep ? Math.min(500, Math.max(PAGE, pins.length)) : PAGE;
			const res = await api.galleryPins({ board, q: query, limit });
			if (mine !== seq) return;
			pins = res.pins;
			next = res.next;
			failed = false;
			loaded = true;
		} catch {
			if (mine === seq) failed = true;
		} finally {
			if (mine === seq) busy = false;
		}
	}

	async function more() {
		if (next === null || busy) return;
		const mine = seq;
		busy = true;
		try {
			const res = await api.galleryPins({ board, q: query, offset: next, limit: PAGE });
			if (mine !== seq) return;
			const known = new Set(pins.map((p) => p.path));
			pins = [...pins, ...res.pins.filter((p) => !known.has(p.path))];
			next = res.next;
		} catch {
			// The sentinel asks again when it next comes into view.
		} finally {
			if (mine === seq) busy = false;
		}
	}

	$effect(() => {
		void board;
		void query;
		untrack(() => {
			loaded = false;
			void reload();
		});
	});

	// A picture saved, described, moved or deleted anywhere.
	$effect(() => {
		let timer: ReturnType<typeof setTimeout> | null = null;
		const onLive = (event: Event) => {
			const detail = (event as CustomEvent).detail;
			if (!detail || !isGalleryEvent(detail)) return;
			if (timer) clearTimeout(timer);
			timer = setTimeout(() => void reload(true), 600);
		};
		window.addEventListener('exscalia:live', onLive);
		return () => {
			window.removeEventListener('exscalia:live', onLive);
			if (timer) clearTimeout(timer);
		};
	});

	$effect(() => {
		omniaContext.view = query
			? `Gallery · search "${query}"${board !== null ? ` in ${board || 'no board'}` : ''}`
			: `Gallery · ${board === null ? 'All pictures' : board || 'No board'}`;
	});

	let adding = $state(false);
	let editing = $state(false);

	async function drop(files: File[]) {
		const errors = await sendPictures(files, board ?? '', () => {});
		if (errors.length) notify(errors.slice(0, 3).join('\n'), { tone: 'error' });
	}

	async function createBoard() {
		const name = await newBoard();
		if (name !== null) void goto(pinsHref(name));
	}
</script>

<svelte:head><title>{board ? `${board} · Gallery` : 'Gallery'}</title></svelte:head>

<div class="page" use:pictureDrop={{ onfiles: (files) => void drop(files) }}>
	<GalleryHeader
		active="pictures"
		{query}
		{board}
		onadd={() => (adding = true)}
		onnewboard={() => void createBoard()}
	/>

	<div class="stripline">
		<nav class="strip" aria-label="Boards">
			<a href={pinsHref(null, query)} class:active={board === null} aria-current={board === null ? 'page' : undefined}>All</a>
			{#each boards as b (b.name)}
				<a
					href={pinsHref(b.name, query)}
					class:active={board === b.name}
					aria-current={board === b.name ? 'page' : undefined}>{b.name}</a
				>
			{/each}
		</nav>
		{#if current}
			<button type="button" class="edit" title="Edit board" aria-label="Edit board {current.name}" onclick={() => (editing = true)}>
				<Glyph name="edit" />
			</button>
		{/if}
	</div>

	<UploadLine />

	{#if pins.length}
		<Masonry {pins} onmore={next !== null ? () => void more() : undefined} />
	{:else if failed}
		<p class="quiet">
			The pictures did not load. <button type="button" class="retry" onclick={() => void reload()}>Retry</button>
		</p>
	{:else if loaded}
		<p class="quiet">{query ? 'No pictures match.' : 'No pictures yet.'}</p>
	{/if}
</div>

{#if adding}
	<AddDialog
		boards={boards.map((b) => b.name)}
		board={board ?? ''}
		onclose={() => (adding = false)}
		onsaved={(_pin, created) => {
			if (!created) notify('Already on that board');
		}}
	/>
{/if}
{#if editing && current}
	<BoardDialog
		name={current.name}
		count={current.count}
		onclose={() => (editing = false)}
		ondone={(what) => {
			if (what.deleted) void goto(pinsHref(), { replaceState: true });
			else if (what.renamed) void goto(pinsHref(what.renamed, query), { replaceState: true });
		}}
	/>
{/if}

<style>
	.page {
		box-sizing: border-box;
		min-height: 100%;
		padding: 28px 32px 48px;
		border-radius: 18px;
		outline: 2px dashed transparent;
		outline-offset: -10px;
		transition: outline-color var(--motion-fast) var(--ease-out);
	}
	.page:global(.dropping) {
		outline-color: var(--text-muted);
	}
	.stripline {
		position: sticky;
		top: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 8px;
		margin: -8px -32px 18px;
		padding: 8px 32px;
		background: var(--bg);
	}
	.strip {
		flex: 1;
		min-width: 0;
		display: flex;
		gap: 26px;
		overflow-x: auto;
		scrollbar-width: none;
	}
	.strip::-webkit-scrollbar {
		display: none;
	}
	.strip a {
		flex: none;
		padding: 6px 0;
		color: var(--text);
		font-size: 17px;
		font-weight: 500;
		text-decoration: none;
		white-space: nowrap;
		border-bottom: 3px solid transparent;
		transition: border-color var(--motion-fast) var(--ease-out);
	}
	.strip a:hover {
		border-bottom-color: var(--border);
	}
	.strip a.active {
		border-bottom-color: var(--text);
		font-weight: 600;
	}
	.strip a:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
		border-radius: 4px;
	}
	.edit {
		flex: none;
		width: 36px;
		height: 36px;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 50%;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.edit:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.edit :global(svg) {
		width: 16px;
		height: 16px;
	}
	.quiet {
		color: var(--text-muted);
		font-size: 14px;
	}
	.retry {
		border: none;
		background: none;
		color: var(--text);
		font: inherit;
		text-decoration: underline;
		cursor: pointer;
	}
	@media (max-width: 760px) {
		.page {
			padding: 14px var(--m-gutter) 32px;
		}
		.stripline {
			margin: -4px calc(-1 * var(--m-gutter)) 12px;
			padding: 4px var(--m-gutter);
		}
		.strip {
			gap: 20px;
		}
		.strip a {
			font-size: var(--m-row-text);
			padding: 11px 0;
		}
		.edit {
			width: 44px;
			height: 44px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.page,
		.strip a {
			transition: none;
		}
	}
</style>
