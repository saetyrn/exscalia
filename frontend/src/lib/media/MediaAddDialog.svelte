<script lang="ts">
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, encodePath } from '$lib/api';
	import type { MediaCandidate, MediaItem, MediaStatus, MediaType } from '$lib/api';
	import { closeMediaAdd, mediaAdd } from '$lib/media/adddialog.svelte';
	import { TYPE_WORD } from '$lib/media/helpers';
	import Glyph from '$lib/Glyph.svelte';
	import StarRating from '$lib/media/StarRating.svelte';
	import StarDisplay from '$lib/media/StarDisplay.svelte';
	import { notify } from '$lib/ui.svelte';

	// One collective search across every provider — results stream in as each
	// answers, so fast types (books) show before slow ones (games).
	const TYPES: MediaType[] = ['movie', 'tv', 'book', 'game'];

	type Mode = 'search' | 'add' | 'rewatch';
	let mode: Mode = $state('search');
	let query = $state('');
	let results = $state<MediaCandidate[]>([]);
	let searching = $state(0);
	let searchError = $state('');
	let busy = $state(false);
	let searchBox: HTMLInputElement | undefined = $state();

	// Add popup (a new item).
	let selected: MediaCandidate | null = $state(null);
	let rating: number | null = $state(null);
	let review = $state('');
	let page = $state('');
	let bookFinished = $state(false);
	let addError = $state('');

	// Rewatch popup (an already-tracked item).
	let match: { path?: string; item?: MediaItem | null; plays?: number } | null = $state(null);
	let rewatchRating: number | null = $state(null);

	let seq = 0;
	let debounce: ReturnType<typeof setTimeout> | undefined;

	// Runs only when the dialog opens — everything the body reads/writes is
	// untracked so typing (which changes `query`) doesn't re-trigger it and
	// reset the dialog out from under a selection.
	$effect(() => {
		if (!mediaAdd.open) return;
		untrack(() => {
			mode = 'search';
			query = mediaAdd.query || '';
			results = [];
			searchError = '';
			selected = null;
			match = null;
			addError = '';
			searching = 0;
			searchBox?.focus();
			if (query.trim()) runSearch(query);
		});
	});

	function runSearch(q: string) {
		const mine = ++seq;
		results = [];
		searchError = '';
		if (!q.trim()) {
			searching = 0;
			return;
		}
		searching = TYPES.length;
		for (const type of TYPES) {
			api
				.mediaLookup(type, q.trim())
				.then((r) => {
					if (mine !== seq) return; // a newer keystroke superseded this
					results = [...results, ...(r.candidates || []).map((c) => ({ ...c, media_type: type }))];
				})
				.catch(() => {})
				.finally(() => {
					if (mine === seq) searching = Math.max(0, searching - 1);
				});
		}
	}

	function onInput() {
		clearTimeout(debounce);
		const q = query;
		debounce = setTimeout(() => runSearch(q), 250);
	}

	function done(path: string, title: string, verb = 'Added') {
		closeMediaAdd();
		notify(`${verb} “${title}”.`, {
			actionLabel: 'View',
			action: () => goto('/media/' + encodePath(path))
		});
	}

	async function pick(candidate: MediaCandidate) {
		if (busy) return;
		const title = candidate.title?.trim();
		if (!title) return;
		// Watchlist adds are a plain "planned" create — no rating/log.
		if (mediaAdd.status === 'planned') {
			await create(candidate, 'planned');
			return;
		}
		busy = true;
		try {
			const m = await api.mediaMatch(title);
			if (m.tracked && m.item) {
				match = { path: m.path, item: m.item, plays: m.plays };
				rewatchRating = m.item.rating ?? null;
				mode = 'rewatch';
			} else {
				selected = candidate;
				rating = null;
				review = '';
				page = '';
				bookFinished = false;
				addError = '';
				mode = 'add';
			}
		} catch (cause) {
			searchError = cause instanceof Error ? cause.message : 'Could not check that title';
		} finally {
			busy = false;
		}
	}

	async function create(candidate: MediaCandidate, status: MediaStatus) {
		const title = candidate.title?.trim() || query.trim();
		const result = await api.mediaCreate({
			title,
			media_type: candidate.media_type || 'book',
			creator: candidate.creator ?? undefined,
			year: candidate.year ?? undefined,
			total: candidate.total ?? undefined,
			cover_url: candidate.cover_url ?? undefined,
			source: candidate.source ?? undefined,
			status,
			rating: rating ?? undefined,
			review: review.trim() || undefined
		});
		return result.path;
	}

	async function confirmAdd() {
		if (!selected || busy) return;
		busy = true;
		addError = '';
		try {
			const isBook = selected.media_type === 'book';
			// A log means you finished it — except a book, which is logged at its
			// current page unless you tick Finished.
			const status: MediaStatus = !isBook || bookFinished ? 'finished' : 'active';
			const path = await create(selected, status);
			if (isBook && !bookFinished && page.trim()) {
				try {
					await api.mediaLog(path, { kind: 'progress', value: `page ${page.trim()}` });
				} catch {
					/* progress is a nicety; the item is already created */
				}
			}
			done(path, selected.title?.trim() || query.trim());
		} catch (cause) {
			addError = cause instanceof Error ? cause.message : 'Could not add it';
		} finally {
			busy = false;
		}
	}

	function rewatchVerb(type?: MediaType | null): string {
		return type === 'book' ? 'reread' : type === 'game' ? 'replay' : 'rewatch';
	}

	async function logRewatch() {
		if (!match?.path || busy) return;
		busy = true;
		try {
			await api.mediaLog(match.path, { kind: 'finished', rating: rewatchRating ?? undefined });
			done(match.path, match.item?.title || '', 'Logged a new sitting for');
		} catch (cause) {
			addError = cause instanceof Error ? cause.message : 'Could not log it';
		} finally {
			busy = false;
		}
	}

	function editReview() {
		const path = match?.path;
		closeMediaAdd();
		if (path) void goto('/media/' + encodePath(path));
	}
</script>

{#if mediaAdd.open}
	<div
		class="overlay"
		role="presentation"
		onclick={(e) => {
			if (e.target === e.currentTarget) closeMediaAdd();
		}}
	>
		<div class="dialog" role="dialog" aria-modal="true" aria-label="Add media">
			<header>
				<h2>
					{#if mode === 'add'}Log “{selected?.title}”
					{:else if mode === 'rewatch'}Already tracked
					{:else}{mediaAdd.status === 'planned' ? 'Add to watchlist' : 'Add media'}{/if}
				</h2>
				<button type="button" class="close" title="Close" aria-label="Close" onclick={closeMediaAdd}>
					✕
				</button>
			</header>

			{#if mode === 'search'}
				<input
					bind:this={searchBox}
					class="search"
					type="search"
					placeholder="Search books, films, shows, games…"
					aria-label="Search all media"
					bind:value={query}
					oninput={onInput}
					onkeydown={(e) => e.key === 'Escape' && closeMediaAdd()}
				/>
				{#if searchError}
					<p class="note">{searchError}</p>
				{/if}
				<div class="results">
					{#each results as candidate, index (candidate.source ?? index)}
						<button type="button" class="cand" disabled={busy} onclick={() => void pick(candidate)}>
							<span class="cover" class:empty={!candidate.cover_url}>
								{#if candidate.cover_url}
									<img src={candidate.cover_url} alt="" loading="lazy" />
								{:else}
									<Glyph name={candidate.media_type ?? 'book'} />
								{/if}
							</span>
							<span class="meta">
								<span class="title">{candidate.title}</span>
								<span class="sub">
									{[TYPE_WORD[candidate.media_type ?? 'book'], candidate.creator, candidate.year]
										.filter(Boolean)
										.join(' · ')}
								</span>
							</span>
						</button>
					{/each}
					{#if searching > 0}
						<p class="note searching">Searching…</p>
					{:else if query.trim() && results.length === 0}
						<p class="note">No matches found.</p>
					{/if}
				</div>

			{:else if mode === 'add'}
				<div class="detail">
					<span class="cover big" class:empty={!selected?.cover_url}>
						{#if selected?.cover_url}
							<img src={selected.cover_url} alt="" />
						{:else}
							<Glyph name={selected?.media_type ?? 'book'} />
						{/if}
					</span>
					<div class="fields">
						<p class="sub">
							{[TYPE_WORD[selected?.media_type ?? 'book'], selected?.creator, selected?.year]
								.filter(Boolean)
								.join(' · ')}
						</p>
						<label class="field">
							<span>Rating</span>
							<StarRating value={rating} onrate={(r) => (rating = rating === r ? null : r)} />
						</label>
						{#if selected?.media_type === 'book'}
							<label class="field">
								<span>Current page</span>
								<input
									class="page"
									type="text"
									inputmode="numeric"
									placeholder="e.g. 120"
									bind:value={page}
									disabled={bookFinished}
								/>
							</label>
							<label class="check">
								<input type="checkbox" bind:checked={bookFinished} />
								<span>Finished it</span>
							</label>
						{/if}
					</div>
				</div>
				<textarea class="review" placeholder="Review (optional)…" rows="3" bind:value={review}
				></textarea>
				{#if addError}<p class="note error">{addError}</p>{/if}
				<div class="actions">
					<button type="button" class="ghost" onclick={() => (mode = 'search')}>Back</button>
					<button type="button" class="go" disabled={busy} onclick={() => void confirmAdd()}>
						{busy ? '…' : 'Add'}
					</button>
				</div>

			{:else if mode === 'rewatch'}
				<div class="detail">
					<span class="cover big" class:empty={!match?.item?.cover}>
						{#if match?.item?.cover}
							<img src={match.item.cover} alt="" />
						{:else}
							<Glyph name={match?.item?.media_type ?? 'book'} />
						{/if}
					</span>
					<div class="fields">
						<p class="already">
							You've already logged this{match?.plays ? ` — ${match.plays} time${match.plays === 1 ? '' : 's'}` : ''}.
						</p>
						{#if match?.item?.rating}
							<div class="field"><span>Your rating</span><StarDisplay value={match.item.rating} /></div>
						{/if}
						<label class="field">
							<span>Rating this time</span>
							<StarRating
								value={rewatchRating}
								onrate={(r) => (rewatchRating = rewatchRating === r ? null : r)}
							/>
						</label>
					</div>
				</div>
				{#if addError}<p class="note error">{addError}</p>{/if}
				<div class="actions">
					<button type="button" class="ghost" onclick={() => (mode = 'search')}>Back</button>
					<button type="button" class="ghost" onclick={editReview}>Edit review</button>
					<button type="button" class="go" disabled={busy} onclick={() => void logRewatch()}>
						{busy ? '…' : `Log ${rewatchVerb(match?.item?.media_type)}`}
					</button>
				</div>
			{/if}
		</div>
	</div>
{/if}

<svelte:window onkeydown={(e) => e.key === 'Escape' && mediaAdd.open && closeMediaAdd()} />

<style>
	.overlay {
		position: fixed;
		inset: 0;
		z-index: 90;
		display: grid;
		place-items: start center;
		padding-top: 12vh;
		background: color-mix(in srgb, #000 45%, transparent);
	}
	.dialog {
		position: relative;
		width: min(460px, calc(100vw - 32px));
		max-height: 74vh;
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg);
		box-shadow: var(--lift-shadow);
		overflow: hidden;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	h2 {
		margin: 0;
		font-size: 14px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.close {
		padding: 2px 6px;
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 13px;
		cursor: pointer;
		flex: none;
	}
	.close:hover {
		color: var(--text);
	}
	.search {
		height: 34px;
		padding: 0 12px;
		font-family: inherit;
		font-size: 13px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.search:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.results {
		display: flex;
		flex-direction: column;
		gap: 4px;
		overflow-y: auto;
	}
	.cand {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 6px;
		font-family: inherit;
		text-align: left;
		color: var(--text);
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		cursor: pointer;
	}
	.cand:hover:not(:disabled) {
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
	}
	.cand:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.cover {
		flex: 0 0 34px;
		width: 34px;
		aspect-ratio: 2 / 3;
		border-radius: 4px;
		overflow: hidden;
		background: var(--bg);
		display: grid;
		place-items: center;
		color: var(--text-muted);
	}
	.cover img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.cover.empty :global(svg) {
		width: 16px;
		height: 16px;
	}
	.cover.big {
		flex: 0 0 84px;
		width: 84px;
		border-radius: 8px;
	}
	.meta {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}
	.title {
		font-size: 13px;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.sub {
		font-size: 12px;
		color: var(--text-muted);
	}
	.note {
		margin: 2px 0;
		font-size: 12px;
		color: var(--text-muted);
	}
	.note.error {
		color: var(--danger, #e5484d);
	}
	.searching {
		font-style: italic;
	}
	.detail {
		display: flex;
		gap: 12px;
	}
	.fields {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.already {
		margin: 0;
		font-size: 13px;
		color: var(--text);
	}
	.field {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		font-size: 12px;
		color: var(--text-muted);
		--star-size: 18px;
	}
	.check {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--text-muted);
		cursor: pointer;
	}
	.page {
		width: 90px;
		height: 28px;
		padding: 0 8px;
		font-family: inherit;
		font-size: 13px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
	}
	.review {
		width: 100%;
		resize: vertical;
		padding: 8px 10px;
		font-family: inherit;
		font-size: 13px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.review:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
	.go,
	.ghost {
		font-family: inherit;
		font-size: 13px;
		padding: 6px 14px;
		border-radius: 8px;
		cursor: pointer;
		border: 1px solid var(--border);
	}
	.ghost {
		background: transparent;
		color: var(--text-muted);
	}
	.ghost:hover {
		color: var(--text);
	}
	.go {
		background: var(--accent);
		border-color: var(--accent);
		color: #fff;
		font-weight: 600;
	}
	.go:disabled {
		opacity: 0.6;
		cursor: default;
	}
	/* The dialog is mounted outside <main>, so it never inherits the media
	   token scope that paints every media route black — on a phone, where the
	   media look owns the entire screen including the chrome, that landed a
	   white card on a black page. Re-declare the same tokens here (keep in
	   step with .media-shell). Desktop keeps the look it has today. */
	@media (max-width: 760px) {
		.overlay {
			padding-top: 8vh;
		}
		.dialog {
			--bg: #000000;
			--bg-panel: #17171a;
			--border: #2a2a2e;
			--text: #ffffff;
			--text-muted: #9c9ca3;
			--bg-hover: color-mix(in srgb, #ffffff 7%, transparent);
			max-height: calc(100dvh - 8vh - var(--tabbar-h, 0px) - 16px);
			background: var(--bg);
			color: var(--text);
		}
		.close {
			width: 44px;
			height: 44px;
			margin: -8px -8px -8px 0;
			font-size: 16px;
		}
	}
</style>
