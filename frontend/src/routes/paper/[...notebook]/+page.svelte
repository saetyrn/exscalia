<script lang="ts">
	// One notebook, read from the web: its pages as renders, top to bottom,
	// with the recognised transcript of whichever page you are looking at.
	//
	// A page is a hash, never a pathname. The layout remounts the route view on
	// every pathname change, so giving each page a URL of its own would tear
	// the scroller (and every decoded render in it) down on each page turn;
	// the hash moves without remounting anything, which is exactly what a
	// scroll position needs.
	import { onMount, untrack } from 'svelte';
	import { replaceState } from '$app/navigation';
	import { page as routeState } from '$app/state';
	import { ApiError, api, apiFetch, encodePath } from '$lib/api';
	import type { PaperNotebook, PaperPage, PaperPageText } from '$lib/api';
	import BottomSheet from '$lib/BottomSheet.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import { createLiveList } from '$lib/media/livelist.svelte';
	import {
		notebookTitleFromPath,
		parsePaperHash,
		parsePaperRegion,
		paperVaultPath
	} from '$lib/paperurl';
	import { conflictStamp, pickCurrentIndex } from '$lib/paperview';
	import { omniaContext } from '$lib/stores.svelte';
	import type { Snapshot } from './$types';

	let { data } = $props();

	let missing = $state(untrack(() => data.missing));

	// A durable link resolves its ids again on every load. A 404 is the dead
	// end the loader states plainly, but only while nothing is on screen: a
	// notebook already showing stays, as after any failed refresh. A fresh
	// manifest takes the cached transcripts with it.
	async function fetchBook(): Promise<PaperNotebook> {
		try {
			const notebook = data.stable
				? (await api.paperResolve(data.stable.notebookId, data.stable.pageId)).notebook
				: data.notebook;
			const next = await api.paperNotebook(notebook);
			missing = false;
			texts.clear();
			return next;
		} catch (error) {
			if (error instanceof ApiError && error.status === 404 && !list.data) missing = true;
			throw error;
		}
	}

	// Only this notebook's `paper` events matter. Until something has loaded,
	// the path to match is not known yet (a durable link only has ids), so
	// any Paper change is a retry.
	const list = createLiveList(fetchBook, {
		match: (d): boolean =>
			d.type === 'paper' && (!list.data || !!d.path?.startsWith(`${vaultPath}/`)),
		initial: untrack(() => data.book)
	});
	let book = $derived(list.data);

	let vaultPath = $derived(paperVaultPath(book?.path || data.notebook));
	let title = $derived(book?.title || notebookTitleFromPath(data.notebook));

	// The manifest carries the pages in full when it can. When it only carries
	// the order, the blob route is addressable from the id alone — a notebook
	// still reads, it just cannot say whether a render is behind the ink.
	let pages: PaperPage[] = $derived.by(() => {
		if (book?.pages_detail?.length) return book.pages_detail;
		return (book?.pages ?? []).map((id) => ({
			id,
			render_url: `/api/paper/blob/${encodePath(vaultPath)}/${encodeURIComponent(id)}/render.png`
		}));
	});

	let index = $state(0);
	let current = $derived(pages[index]);
	const pagePath = (id: string) => `${vaultPath}/pages/${id}`;

	let reader: HTMLElement | undefined = $state();
	let sheets: (HTMLElement | undefined)[] = $state([]);

	// One retry when the loader failed short of a 404; after that the next
	// Paper change is the retry.
	$effect(() => {
		if (list.failed && !book && !missing) void list.load();
	});

	// --- page position -------------------------------------------------------

	// A jump (hash, link, arrow key, Back) pins the page it lands on until the
	// reader scrolls by hand. Without the pin the jump's own scroll event
	// re-decides the page, and a short last page can never win that vote.
	let pinned = false;

	function scrollToIndex(next: number) {
		index = Math.max(0, Math.min(next, Math.max(pages.length - 1, 0)));
		pinned = true;
		// Instant, never smooth: this is a jump, and a Reduce Motion setting
		// should not have to argue with it.
		sheets[index]?.scrollIntoView({ block: 'start', behavior: 'auto' });
	}

	function jumpTo(id: string | null) {
		if (!id) return;
		const at = pages.findIndex((p) => p.id === id);
		if (at >= 0) scrollToIndex(at);
	}

	// `window.location`, not the router's page.url: `replaceState` from
	// $app/navigation never updates page.url, so its hash is the entry one for
	// the life of the route.
	function jumpToHash() {
		jumpTo(parsePaperHash(window.location.hash) || data.initialPageId);
	}

	// The page you are reading is the one filling most of the reader, or the
	// last one once there is nothing left to scroll.
	let scrollFrame = 0;
	function onScroll() {
		if (pinned || scrollFrame) return;
		scrollFrame = requestAnimationFrame(() => {
			scrollFrame = 0;
			if (pinned || !reader) return;
			const box = reader.getBoundingClientRect();
			const boxes = pages.map(
				(_, i) => sheets[i]?.getBoundingClientRect() ?? { top: 0, bottom: 0 }
			);
			const atBottom = reader.scrollTop + reader.clientHeight >= reader.scrollHeight - 2;
			index = pickCurrentIndex(box, boxes, atBottom);
		});
	}

	const unpin = () => (pinned = false);

	onMount(() => {
		jumpToHash();
		const onHashChange = () => jumpToHash();
		window.addEventListener('hashchange', onHashChange);
		return () => {
			window.removeEventListener('hashchange', onHashChange);
			if (scrollFrame) cancelAnimationFrame(scrollFrame);
		};
	});

	// Hand scrolling on the reader lets the scroll position speak again;
	// pointerdown covers a scrollbar drag. The reader only exists once there
	// are pages, so this follows it in and out.
	$effect(() => {
		const el = reader;
		if (!el) return;
		const events = ['wheel', 'touchstart', 'pointerdown'] as const;
		for (const name of events) el.addEventListener(name, unpin, { passive: true });
		return () => {
			for (const name of events) el.removeEventListener(name, unpin);
		};
	});

	// The manifest can arrive after mount (a failed load that retried); jump
	// once the page named in the hash (or a restored snapshot) exists.
	let jumped = false;
	let pendingRestore: string | null = null;
	$effect(() => {
		if (jumped || pages.length === 0) return;
		jumped = true;
		untrack(() => (pendingRestore ? jumpTo(pendingRestore) : jumpToHash()));
		pendingRestore = null;
	});

	// Back into the notebook returns to the page that was being read, whatever
	// the entry URL named. SvelteKit restores after the page has mounted, so
	// this jumps straight away unless the pages are still on their way.
	export const snapshot: Snapshot<string | null> = {
		capture: () => current?.id ?? null,
		restore: (id) => {
			if (!id) return;
			if (jumped) jumpTo(id);
			else pendingRestore = id;
		}
	};

	// Keep the URL on the page being read, so a copied link lands here. The
	// hash is replaced, not pushed: forty page turns are not forty history
	// entries, and replaceState fires no hashchange to bounce off. The search
	// stays: a durable link's ?region= is the point of the link. A view on its
	// way out can still run this after the next route's URL is in place; it
	// must never stamp its page onto that one.
	const routePath = window.location.pathname;
	$effect(() => {
		const id = current?.id;
		if (!id) return;
		if (window.location.pathname !== routePath) return;
		if (parsePaperHash(window.location.hash) === id) return;
		try {
			// Untracked: replaceState reads the router's page.url, and a hash
			// typed into the address bar updates that before the hashchange
			// jump runs. Tracked, this would re-run then and write the page
			// being left back over the one asked for.
			untrack(() =>
				replaceState(
					`${window.location.pathname}${window.location.search}#${encodeURIComponent(id)}`,
					{}
				)
			);
		} catch {
			// Router not ready yet (SSR/hydration edge); the next turn syncs it.
		}
	});

	// What OMNIA sees when asked "what's on here?". The layout resets this on
	// every pathname change and a page turn is only a hash change, so this
	// effect is the only thing that keeps it truthful — and it runs after the
	// layout's, which is created first.
	$effect(() => {
		const id = current?.id;
		omniaContext.view = id
			? `Paper · ${title} · page ${index + 1} (${pagePath(id)})`
			: `Paper · ${title}`;
	});

	const SCROLL_KEYS = new Set(['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' ']);

	function onKeydown(event: KeyboardEvent) {
		const target = event.target as HTMLElement | null;
		if (
			target?.isContentEditable ||
			['INPUT', 'TEXTAREA', 'SELECT'].includes(target?.tagName ?? '')
		)
			return;
		// Keyboard scrolling is hand scrolling too (Cmd+Down included).
		if (SCROLL_KEYS.has(event.key)) unpin();
		if (event.metaKey || event.ctrlKey || event.altKey) return;
		if (event.key === 'ArrowRight') {
			event.preventDefault();
			scrollToIndex(index + 1);
		} else if (event.key === 'ArrowLeft') {
			event.preventDefault();
			scrollToIndex(index - 1);
		}
	}

	// --- transcript ----------------------------------------------------------

	let transcriptOpen = $state(false);
	let narrow = $state(false);
	const texts = new Map<string, PaperPageText>();
	let text: PaperPageText | null = $state(null);
	let textState: 'idle' | 'loading' | 'failed' = $state('idle');

	onMount(() => {
		const query = window.matchMedia('(max-width: 760px)');
		narrow = query.matches;
		const onChange = (e: MediaQueryListEvent) => (narrow = e.matches);
		query.addEventListener('change', onChange);
		return () => query.removeEventListener('change', onChange);
	});

	// Derived data, fetched only when it is being looked at: transcripts are
	// a separate blob per page and a 200-page notebook should not pull 200.
	$effect(() => {
		const target = current;
		if (!transcriptOpen || !target?.id) return;
		const cached = texts.get(target.id);
		if (cached) {
			text = cached;
			textState = 'idle';
			return;
		}
		const url = target.text_url;
		if (!url) {
			text = null;
			textState = 'idle';
			return;
		}
		let cancelled = false;
		textState = 'loading';
		void apiFetch(url)
			.then((res) => (res.ok ? res.json() : Promise.reject(new Error(String(res.status)))))
			.then((body: PaperPageText) => {
				if (cancelled) return;
				texts.set(target.id, body);
				text = body;
				textState = 'idle';
			})
			.catch(() => {
				if (cancelled) return;
				text = null;
				textState = 'failed';
			});
		return () => {
			cancelled = true;
		};
	});

	// `.by` rather than a bare expression: TypeScript narrows `text` to its
	// initial `null` in an inline argument, and only a closure widens it back.
	let transcriptBody = $derived.by(
		() =>
			text?.corrected_transcript?.trim() ||
			text?.transcript?.trim() ||
			text?.transcript_ai?.text?.trim() ||
			''
	);

	// Durable links carry the selected lasso rectangle in page points. The
	// highlight is a quiet outline over the full render and never changes ink.
	// It belongs to the page the link names, not to whichever page is being
	// read. The router's page.url keeps the entry search, which is the one
	// carrying the region.
	let regionPageId = $derived(data.stable?.pageId ?? data.initialPageId ?? null);
	let regionSheet = $derived(pages.find((p) => p.id === regionPageId) ?? null);
	let region = $derived.by(() => {
		const raw = parsePaperRegion(routeState.url.search);
		if (!raw || !regionSheet) return null;
		const values = raw.split(',').map(Number);
		const size = regionSheet.size_points ?? regionSheet.size;
		if (values.length !== 4 || values.some((value) => !Number.isFinite(value))) return null;
		const width = size?.width || 595;
		const height = size?.height || 842;
		return {
			left: `${(values[0] / width) * 100}%`,
			top: `${(values[1] / height) * 100}%`,
			width: `${(values[2] / width) * 100}%`,
			height: `${(values[3] / height) * 100}%`
		};
	});

	// --- live ----------------------------------------------------------------

	// A page saved on the iPad arrives as a `paper` live event; a sync
	// pushing twenty pages is one refetch (the list's match is up top).
	$effect(() => list.listen());

	const isStale = (p: PaperPage) =>
		typeof p.revision === 'number' &&
		typeof p.render_revision === 'number' &&
		p.render_revision < p.revision;

	// A stale-revision save from a second device is parked as its own page
	// right after the one it forked from. Numbering stays positional, as on
	// the iPad and in OMNIA; the chip only says which page is the fork.
	function conflictLabel(p: PaperPage): string {
		const of = pages.findIndex((other) => other.id === p.conflict_of);
		const when = conflictStamp(p.id);
		return [
			of >= 0 ? `Conflict copy of page ${of + 1}` : 'Conflict copy',
			when?.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
		]
			.filter(Boolean)
			.join(' · ');
	}
</script>

<svelte:head><title>{title} — Paper</title></svelte:head>
<svelte:window onkeydown={onKeydown} />

<div class="notebook" class:split={transcriptOpen && !narrow}>
	<div class="pane">
		<header class="head">
			<div class="titles">
				<h1>{title}</h1>
				<p class="hint">
					Write in Paper on iPad.{#if pages.length} · Page {index + 1} of {pages.length}{/if}
				</p>
			</div>
			<button
				type="button"
				class="icon"
				class:on={transcriptOpen}
				title="Transcript"
				aria-label="Transcript"
				aria-pressed={transcriptOpen}
				onclick={() => (transcriptOpen = !transcriptOpen)}
			>
				<Glyph name="note" />
			</button>
		</header>

		{#if missing}
			<p class="state">This notebook is not in the vault.</p>
		{:else if list.failed && !book}
			<p class="state">Could not load this notebook — retrying on the next change.</p>
		{:else if pages.length === 0}
			<p class="state">No pages yet.</p>
		{:else}
			<div class="reader" bind:this={reader} onscroll={onScroll}>
				{#each pages as sheet, i (sheet.id)}
					<figure class="sheet" bind:this={sheets[i]}>
						<div class="render-wrap">
							{#if sheet.render_url}
								<img
									src={sheet.render_url}
									alt="Page {i + 1}"
									loading="lazy"
									decoding="async"
									style="aspect-ratio: {(sheet.size_points ?? sheet.size)?.width || 595} / {(sheet.size_points ?? sheet.size)?.height || 842}"
								/>
							{:else}
								<div
									class="render-missing"
									style="aspect-ratio: {(sheet.size_points ?? sheet.size)?.width || 595} / {(sheet.size_points ?? sheet.size)?.height || 842}"
								>
									Not rendered yet
								</div>
							{/if}
							{#if region && sheet.id === regionPageId}
								<span class="region-highlight" style:top={region.top} style:left={region.left} style:width={region.width} style:height={region.height} aria-label="Selected Paper region"></span>
							{/if}
						</div>
						<figcaption>
							<span>Page {i + 1}</span>
							{#if sheet.conflict_of}
								<span class="stale">{conflictLabel(sheet)}</span>
							{/if}
							{#if sheet.render_url && isStale(sheet)}
								<span
									class="stale"
									title="This picture is older than the ink — the iPad has not sent a new render yet."
									>Older render</span
								>
							{/if}
						</figcaption>
					</figure>
				{/each}
			</div>
		{/if}
	</div>

	{#if transcriptOpen && !narrow}
		<aside class="transcript" aria-label="Transcript">
			<h2>Transcript</h2>
			{#if textState === 'loading'}
				<p class="state">Reading…</p>
			{:else if textState === 'failed'}
				<p class="state">No transcript for this page.</p>
			{:else if transcriptBody}
				<p class="body">{transcriptBody}</p>
			{:else}
				<p class="state">Nothing recognised on this page yet.</p>
			{/if}
		</aside>
	{/if}
</div>

{#if transcriptOpen && narrow}
	<BottomSheet
		title={current ? `Page ${index + 1}` : title}
		label="Page transcript"
		onclose={() => (transcriptOpen = false)}
	>
		<div class="sheet-body">
			{#if textState === 'loading'}
				<p class="state">Reading…</p>
			{:else if textState === 'failed'}
				<p class="state">No transcript for this page.</p>
			{:else if transcriptBody}
				<p class="body">{transcriptBody}</p>
			{:else}
				<p class="state">Nothing recognised on this page yet.</p>
			{/if}
		</div>
	</BottomSheet>
{/if}

<style>
	.notebook {
		display: flex;
		flex: 1;
		min-height: 0;
		min-width: 0;
	}
	.pane {
		display: flex;
		flex: 1;
		flex-direction: column;
		min-width: 0;
		min-height: 0;
	}
	.head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		padding: 14px 28px 10px;
	}
	h1 {
		margin: 0;
		font-size: 16px;
		font-weight: 600;
	}
	.hint {
		margin: 3px 0 0;
		color: var(--text-muted);
		font-size: 12px;
	}
	/* Bare monochrome glyph: no box, no fill — colour carries the state. */
	.icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		flex: none;
		padding: 0;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		transition: color var(--motion-fast);
	}
	.icon:hover {
		color: var(--text);
	}
	.icon.on {
		color: var(--accent);
	}
	.icon :global(svg) {
		width: 17px;
		height: 17px;
	}
	.reader {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 0 28px 48px;
		scroll-behavior: auto;
	}
	.sheet {
		margin: 0 auto 24px;
		max-width: 900px;
	}
	.render-wrap {
		position: relative;
	}
	.sheet img,
	.render-missing {
		/* The aspect ratio (inline, from the page size) reserves the sheet's
		   height before the render arrives, so the hash jump on mount lands on
		   the right page instead of a zero-height stack that later expands. */
		display: block;
		width: 100%;
		border: 1px solid var(--border);
		border-radius: 4px;
		/* The paper itself is light in the native app; keep the sheet reading as
		   paper here rather than as a hole in a dark theme. */
		background: #fdfcfa;
	}
	.render-missing {
		display: flex;
		align-items: center;
		justify-content: center;
		/* The light theme's muted text: the sheet is paper in every theme. */
		color: #6e7681;
		font-size: 12px;
	}
	.region-highlight {
		position: absolute;
		z-index: 1;
		border: 2px solid var(--accent);
		border-radius: 3px;
		box-shadow: 0 0 0 9999px color-mix(in srgb, var(--accent) 8%, transparent);
		pointer-events: none;
	}
	figcaption {
		display: flex;
		align-items: center;
		gap: 10px;
		padding-top: 6px;
		color: var(--text-muted);
		font-size: 11px;
	}
	.stale {
		padding: 1px 6px;
		border: 1px solid var(--border);
		border-radius: 999px;
		font-size: 10px;
	}
	.transcript {
		width: 320px;
		flex: none;
		overflow-y: auto;
		padding: 14px 20px 48px;
		border-left: 1px solid var(--border);
	}
	.transcript h2 {
		margin: 0 0 10px;
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.body {
		margin: 0;
		font-size: 13px;
		line-height: 1.6;
		white-space: pre-wrap;
	}
	.state {
		padding: 22px 10px;
		color: var(--text-muted);
		font-size: 12px;
		text-align: center;
	}
	.sheet-body {
		padding: 4px 16px 20px;
	}
	@media (max-width: 760px) {
		.head {
			padding: 12px var(--m-gutter) 8px;
		}
		h1 {
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		.hint {
			font-size: var(--m-meta);
		}
		.icon {
			width: 44px;
			height: 44px;
		}
		.icon :global(svg) {
			width: var(--m-icon-bar);
			height: var(--m-icon-bar);
		}
		.reader {
			/* No bottom safe-area padding: MobileTabBar is in flow below and
			   already carries the inset. */
			padding: 0 var(--m-gutter) 16px;
		}
		.sheet {
			margin-bottom: 16px;
		}
	}
</style>
