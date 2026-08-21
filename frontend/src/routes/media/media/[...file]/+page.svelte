<script lang="ts">
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { api } from '$lib/api';
	import type { MediaDetail, MediaStatus } from '$lib/api';
	import { removeDraft } from '$lib/drafts';
	import { loadHub } from '$lib/mediahub.svelte';
	import MediaChatPopover from '$lib/MediaChatPopover.svelte';
	import MediaActionBar from '$lib/media/MediaActionBar.svelte';
	import StarRating from '$lib/media/StarRating.svelte';
	import { eventPhrase, mergeDayBookends, starText } from '$lib/media/helpers';
	import type { LogRequest } from '$lib/media/mediaactions';
	import { renderMarkdown } from '$lib/render';
	import { forgetNoteThread, refreshNotes } from '$lib/stores.svelte';
	import { confirmDialog, notify } from '$lib/ui.svelte';

	let { data } = $props();

	let detail: MediaDetail | null = $state(untrack(() => data.detail));
	let path: string = $state(untrack(() => data.path));
	let busy = $state(false);
	let logValue = $state('');
	let editingReview = $state(false);
	let reviewDraft = $state('');

	async function reload() {
		try {
			detail = await api.mediaGet(path);
		} catch {
			/* keep what we have; the next live event retries */
		}
	}

	$effect(() => {
		if (detail === null) void reload();
	});

	// The enrichment job finishes out-of-band; its note event repaints the
	// page with the backdrop and palette.
	let liveRefresh: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const onLive = (e: Event) => {
			const d = (e as CustomEvent).detail as { type?: string; path?: string };
			if (d?.type !== 'note' || d.path !== path) return;
			if (liveRefresh) clearTimeout(liveRefresh);
			liveRefresh = setTimeout(() => {
				liveRefresh = null;
				void reload();
			}, 400);
		};
		window.addEventListener('exscalia:live', onLive);
		return () => {
			window.removeEventListener('exscalia:live', onLive);
			if (liveRefresh) clearTimeout(liveRefresh);
		};
	});

	const str = (v: unknown) => (v == null ? null : String(v));
	let item = $derived(detail?.item ?? null);
	let title = $derived(item?.title ?? str(detail?.meta?.title) ?? path.split('/').pop()?.replace(/\.md$/, '') ?? 'Media');
	let mediaType = $derived(item?.media_type ?? 'book');

	const CREATOR_LABELS: Record<string, string> = {
		book: 'By',
		movie: 'Directed by',
		tv: 'Created by',
		game: 'By'
	};

	function statusLabel(status: MediaStatus): string {
		// Planned IS the watchlist — one concept, one word, everywhere.
		if (status === 'planned') return 'Watchlist';
		if (status === 'active') {
			if (mediaType === 'book') return 'Reading';
			if (mediaType === 'game') return 'Playing';
			return 'Watching';
		}
		return status.charAt(0).toUpperCase() + status.slice(1);
	}

	let backdrop = $derived.by(() => {
		const value = str(detail?.meta?.backdrop);
		return value?.startsWith('/api/attachments/') ? value : null;
	});
	let cover = $derived(item?.cover ?? null);

	// Every finish or watch entry counts as one viewing (movies).
	let watchCount = $derived(
		detail?.events.filter((e) => e.kind === 'watched' || e.kind === 'finished').length ?? 0
	);

	// The note body is the review; the ## Log section renders separately.
	let reviewRaw = $derived.by(() => {
		const content = detail?.content ?? '';
		const body = content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n\r?\n?/, '');
		return body.split(/^##\s+log\s*$/im)[0].trim();
	});
	let reviewHtml = $derived(reviewRaw ? renderMarkdown(reviewRaw) : '');

	let reviewBox: HTMLTextAreaElement | undefined = $state();

	// The display name is the alias when set; the real title becomes a subtitle.
	let displayName = $derived(item?.alias?.trim() || title);
	let editingAlias = $state(false);
	let aliasDraft = $state('');
	let aliasBox: HTMLInputElement | undefined = $state();

	function startAliasEdit() {
		aliasDraft = item?.alias ?? '';
		editingAlias = true;
	}

	$effect(() => {
		if (editingAlias && aliasBox) {
			aliasBox.focus();
			aliasBox.select();
		}
	});

	async function saveAlias() {
		if (busy) return;
		busy = true;
		try {
			await api.mediaPatch(path, { alias: aliasDraft.trim(), base_revision: detail?.revision });
			editingAlias = false;
			await reload();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	function startReviewEdit(event?: Event) {
		// A link inside the rendered review is still a link.
		if (event?.target instanceof Element && event.target.closest('a')) return;
		reviewDraft = reviewRaw;
		editingReview = true;
	}

	$effect(() => {
		if (editingReview && reviewBox) {
			reviewBox.focus();
			const end = reviewBox.value.length;
			reviewBox.setSelectionRange(end, end);
		}
	});

	async function saveReview() {
		if (busy) return;
		busy = true;
		try {
			await api.mediaSetReview(path, {
				review: reviewDraft,
				base_revision: detail?.revision
			});
			editingReview = false;
			await reload();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	function dayOf(at: string): string {
		return new Date(`${at}T00:00:00`).toLocaleDateString(undefined, {
			month: 'short',
			day: 'numeric',
			year: 'numeric'
		});
	}

	// The hub payload is cached across navigations (lib/mediahub), so after a
	// mutation the watchlist row and the counters would go on showing the
	// stale answer until a live event happened to land. Nudge it every time.
	function refreshHub() {
		void loadHub().catch(() => {
			/* the last good payload stays on screen */
		});
	}

	async function patch(body: { rating?: number }) {
		if (busy) return;
		busy = true;
		try {
			await api.mediaPatch(path, body);
			await reload();
			refreshHub();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	/** One shape for every action-bar call: run it, repaint from the server,
	 * refresh the hub — and answer whether it worked, because a sheet stays
	 * open on failure so the typed draft survives. */
	async function runAction(work: () => Promise<unknown>): Promise<boolean> {
		if (busy) return false;
		busy = true;
		try {
			await work();
			await reload();
			refreshHub();
			return true;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
			return false;
		} finally {
			busy = false;
		}
	}

	const barLog = (body: LogRequest) =>
		runAction(async () => notify((await api.mediaLog(path, body)).summary + '.'));

	const barRate = (rating: number) => runAction(() => api.mediaPatch(path, { rating }));

	const barStatus = (status: MediaStatus) =>
		runAction(async () => notify((await api.mediaPatch(path, { status })).summary + '.'));

	async function log(event: SubmitEvent) {
		event.preventDefault();
		if (busy) return;
		const value = logValue.trim();
		let body: Parameters<typeof api.mediaLog>[1];
		if (mediaType === 'movie') {
			body = { kind: 'watched' };
		} else if (mediaType === 'game') {
			if (!value) {
				notify('Write a short note about the session.', { tone: 'error' });
				return;
			}
			body = { kind: 'note', text: value };
		} else if (mediaType === 'book') {
			if (!/^\d+$/.test(value) || Number(value) < 1) {
				notify('Enter your current page.', { tone: 'error' });
				return;
			}
			body = { kind: 'progress', value };
		} else {
			if (!/^s?\d{1,2}\s*e\d{1,3}$/i.test(value) && !/^\d+$/.test(value)) {
				notify('Enter an episode like S02E05 (or a number).', { tone: 'error' });
				return;
			}
			body = { kind: 'progress', value };
		}
		busy = true;
		try {
			const result = await api.mediaLog(path, body);
			logValue = '';
			notify(result.summary + '.');
			await reload();
			refreshHub();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	async function deleteLog(event: { at: string; text: string; bookend?: { at: string; text: string } }) {
		if (busy) return;
		busy = true;
		try {
			const first = await api.mediaDeleteLog(path, {
				at: event.at,
				text: event.text,
				base_revision: detail?.revision
			});
			// A merged "Watched" entry stands for two lines; remove its hidden
			// started mate too, rebased on the revision the first delete left.
			if (event.bookend) {
				await api.mediaDeleteLog(path, {
					at: event.bookend.at,
					text: event.bookend.text,
					base_revision: first.revision
				});
			}
			await reload();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	async function remove() {
		if (busy || detail === null) return;
		if (
			!(await confirmDialog(`Move “${title}” to Trash for 30 days?`, {
				title: 'Delete media',
				confirmLabel: 'Delete',
				danger: true
			}))
		)
			return;

		busy = true;
		try {
			await api.mediaDelete(path);
			await removeDraft(path);
			forgetNoteThread(path);
			await refreshNotes();
			notify('Media moved to Trash.', {
				tone: 'success',
				actionLabel: 'View Trash',
				action: () => goto('/trash')
			});
			await goto('/media');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
			busy = false;
		}
	}
</script>

<div class="detail">
	<div class="page-nav" role="group" aria-label="Page history">
		<button
			type="button"
			title="Back"
			aria-label="Go back to the previously visited page"
			onclick={() => window.history.back()}
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
		</button>
		<button
			type="button"
			title="Forward"
			aria-label="Go forward to the next visited page"
			onclick={() => window.history.forward()}
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
		</button>
		<a href="/media" title="Media home" aria-label="Go to the media hub">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10.5 12 3l9 7.5M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5" /></svg>
		</a>
	</div>
	{#if detail === null}
		<p class="state">Loading…</p>
	{:else}
		<MediaChatPopover
			contextPath={path}
			placeholder="Ask about this item…"
			starters={['Log my progress', 'Pick a new backdrop']}
		/>
		<button
			class="delete-media"
			type="button"
			title="Delete media"
			aria-label="Delete media"
			disabled={busy}
			onclick={() => void remove()}
		>
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
		</button>
		<div class="hero" aria-hidden="true">
			{#if backdrop}
				<img class="hero-edge" src={backdrop} alt="" />
				<img class="hero-still" src={backdrop} alt="" />
			{/if}
			<div class="hero-fade"></div>
		</div>

		<div class="sheet">
			<header class="head">
				<div class="poster">
					{#if cover}
						<img src={cover} alt="" />
					{:else}
						<span>{mediaType === 'tv' ? 'TV' : mediaType.charAt(0).toUpperCase() + mediaType.slice(1)}</span>
					{/if}
				</div>
				<div class="ident">
					{#if editingAlias}
						<input
							class="alias-edit"
							bind:this={aliasBox}
							bind:value={aliasDraft}
							placeholder={title}
							disabled={busy}
							onblur={() => void saveAlias()}
							onkeydown={(e) => {
								if (e.key === 'Enter') void saveAlias();
								if (e.key === 'Escape') editingAlias = false;
							}}
						/>
					{:else}
						<h1>
							<button
								type="button"
								class="title-click"
								title="Click to rename (sets a display alias)"
								onclick={startAliasEdit}
							>
								{displayName}
							</button>
						</h1>
					{/if}
					{#if item?.alias?.trim()}
						<p class="orig-title" title="Original title">{title}</p>
					{/if}
					<p class="byline">
						{#if item?.year}<span class="year">{item.year}</span>{/if}
						{#if item?.creator}
							<span class="creator">{CREATOR_LABELS[mediaType]} {item.creator}</span>
						{/if}
					</p>
					{#if item}
						<p class="standing">
							<span>{statusLabel(item.status)}</span>
							{#if item.rating != null}<span class="rated">{starText(item.rating)}</span>{/if}
							{#if item.total}
								<span class="total">
									{mediaType === 'book' ? `${item.total} pages` : mediaType === 'tv' ? `${item.total} episodes` : item.total}
								</span>
							{/if}
						</p>
					{/if}
				</div>
			</header>

			<div class="cols">
				<main>
					<section>
						<h2 class="label">Review</h2>
						{#if editingReview}
							<textarea
								class="review-edit"
								bind:this={reviewBox}
								bind:value={reviewDraft}
								rows={Math.max(6, reviewDraft.split('\n').length + 1)}
								placeholder="Your thoughts — markdown works."
								disabled={busy}
								onkeydown={(e) => e.key === 'Escape' && (editingReview = false)}
							></textarea>
							<div class="edit-actions">
								<button class="label-action" onclick={() => void saveReview()} disabled={busy}>
									{busy ? 'Saving…' : 'Save'}
								</button>
								<button
									class="label-action quiet"
									onclick={() => (editingReview = false)}
									disabled={busy}
								>
									Cancel
								</button>
							</div>
						{:else}
							<div
								class="review-click"
								role="button"
								tabindex="0"
								aria-label="Edit review"
								title="Click to edit"
								onclick={startReviewEdit}
								onkeydown={(e) =>
									(e.key === 'Enter' || e.key === ' ') &&
									(e.preventDefault(), startReviewEdit())}
							>
								{#if reviewHtml}
									<!-- eslint-disable-next-line svelte/no-at-html-tags — renderMarkdown sanitizes -->
									<div class="review">{@html reviewHtml}</div>
								{:else}
									<!-- No placeholder copy: an empty review shows nothing. The
									     region stays clickable (tooltip carries the affordance). -->
									<div class="review-blank" aria-hidden="true"></div>
								{/if}
							</div>
						{/if}
					</section>

				</main>

				<aside class="rail">
					<div class="rail-card rate-card">
						<h2 class="label">{item?.rating != null ? 'Rated' : 'Rate'}</h2>
						<StarRating
							value={item?.rating ?? null}
							disabled={busy}
							onrate={(rating) => void patch({ rating })}
						/>
					</div>

					<div class="rail-card">
						{#if mediaType !== 'movie'}
							<!-- On a laptop the heading labels the quick-log form below
							     it. On a phone that form has moved to the action bar, so
							     with nothing logged yet the heading would label thin
							     air. -->
							<h2 class="label" class:empty-log={detail.events.length === 0}>Log</h2>
						{/if}
						<form class="quick-log" onsubmit={log}>
							{#if mediaType !== 'movie'}
								<input
									bind:value={logValue}
									placeholder={mediaType === 'book' ? 'Current page' : mediaType === 'tv' ? 'S02E05' : 'Session note…'}
									aria-label={mediaType === 'book' ? 'Current page' : mediaType === 'tv' ? 'Episode reached' : 'Session note'}
									inputmode={mediaType === 'book' ? 'numeric' : undefined}
									disabled={busy}
								/>
							{/if}
							<button type="submit" disabled={busy}>
								{mediaType === 'movie'
									? watchCount > 0
										? `Watched: ${watchCount}`
										: 'Log watch'
									: 'Log'}
							</button>
						</form>
						{#if detail.events.length > 0}
							<ul class="log">
								{#each mergeDayBookends(detail.events) as event, index (index)}
									<li>
										<span class="log-day">{dayOf(event.at)}</span>
										<span class="log-phrase">{eventPhrase(event, mediaType)}</span>
										<button
											type="button"
											class="log-del"
											title="Delete this entry"
											aria-label="Delete this log entry"
											disabled={busy}
											onclick={() => void deleteLog(event)}
										>
											<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
										</button>
									</li>
								{/each}
							</ul>
						{/if}
					</div>

					<!-- Touch has no hover, so the corner button below reveals
					     itself to nobody on a phone — and it is pinned exactly
					     where the tab bar now sits. Deleting gets a plain row in
					     the rail instead, beside the item's other actions, where
					     it is reachable and still nowhere near a thumb resting
					     on the poster. Desktop keeps the corner button. -->
					<button
						class="rail-delete"
						type="button"
						disabled={busy}
						onclick={() => void remove()}
					>
						Delete this item
					</button>
				</aside>
			</div>
		</div>

		<!-- Mobile only (the component hides itself above 760px): the four
		     verbs the rail carries on a laptop, parked in the thumb arc. -->
		<MediaActionBar
			title={displayName}
			{mediaType}
			{item}
			events={detail.events}
			{busy}
			onlog={barLog}
			onrate={barRate}
			onstatus={barStatus}
		/>
	{/if}
</div>

<style>
	.detail {
		/* Media pages wear one standard look regardless of app theme: black
		   surface, white type. Only --accent follows the app, for interactive
		   highlights. Scoped custom props re-skin every descendant rule. */
		--bg: #000000;
		--bg-panel: #17171a;
		--border: #2a2a2e;
		--text: #ffffff;
		--text-muted: #9c9ca3;
		--accent-soft: color-mix(in srgb, var(--accent) 16%, transparent);
		--bg-hover: color-mix(in srgb, #ffffff 7%, transparent);
		position: relative;
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		background: var(--bg);
		color: var(--text);
	}
	/* Bare glyphs over the hero; the drop shadow keeps them readable on any
	   backdrop without giving them a box. */
	.page-nav {
		position: absolute;
		top: 10px;
		left: 12px;
		z-index: 2;
		display: flex;
		gap: 2px;
	}
	.page-nav button,
	.page-nav a {
		width: 30px;
		height: 30px;
		display: grid;
		place-items: center;
		padding: 0;
		border: none;
		background: none;
		color: var(--text);
		text-decoration: none;
		cursor: pointer;
		opacity: 0.8;
		filter: drop-shadow(0 1px 4px color-mix(in srgb, var(--bg) 85%, transparent));
		transition: opacity var(--motion-fast);
	}
	.page-nav button:hover,
	.page-nav a:hover {
		opacity: 1;
	}
	.page-nav svg {
		width: 17px;
		height: 17px;
	}
	.delete-media {
		/* Destructive, so it hides in the bottom corner and only appears when
		   the pointer finds it (or it receives keyboard focus). */
		position: fixed;
		bottom: 16px;
		right: 18px;
		z-index: 3;
		width: 34px;
		height: 34px;
		display: grid;
		place-items: center;
		padding: 0;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		opacity: 0;
		filter: drop-shadow(0 1px 4px color-mix(in srgb, var(--bg) 85%, transparent));
		transition: opacity var(--motion-fast), color var(--motion-fast);
	}
	.delete-media:hover,
	.delete-media:focus-visible {
		color: var(--danger, #e06c75);
		opacity: 1;
	}
	.delete-media:focus-visible {
		outline: 1px solid currentColor;
		outline-offset: 1px;
	}
	.delete-media:disabled {
		opacity: 0.45;
		cursor: default;
	}
	.delete-media svg {
		width: 17px;
		height: 17px;
	}
	.state {
		padding: 48px 10px;
		color: var(--text-muted);
		font-size: 12px;
		text-align: center;
	}
	.hero {
		position: relative;
		height: clamp(220px, 40vh, 420px);
		overflow: hidden;
		background:
			radial-gradient(ellipse at 50% 12%, color-mix(in srgb, var(--bg-panel) 78%, transparent), transparent 68%),
			var(--bg);
	}
	.hero img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: center 25%;
	}
	/* The still stays crisp through the center. A second copy sits beneath it
	   and is revealed only by the feathered side masks, producing Letterboxd-
	   style soft edges without turning the image itself into a blur. */
	.hero-edge {
		filter: blur(32px) saturate(0.9) brightness(0.72);
		transform: scale(1.1);
		opacity: 0.82;
	}
	.hero-still {
		-webkit-mask-image: linear-gradient(
			to right,
			transparent 0%,
			rgba(0, 0, 0, 0.28) 4%,
			#000 13%,
			#000 87%,
			rgba(0, 0, 0, 0.28) 96%,
			transparent 100%
		);
		mask-image: linear-gradient(
			to right,
			transparent 0%,
			rgba(0, 0, 0, 0.28) 4%,
			#000 13%,
			#000 87%,
			rgba(0, 0, 0, 0.28) 96%,
			transparent 100%
		);
	}
	.hero-fade {
		position: absolute;
		inset: 0;
		background:
			linear-gradient(to right, var(--bg) 0%, transparent 10%, transparent 90%, var(--bg) 100%),
			linear-gradient(to bottom, color-mix(in srgb, var(--bg) 18%, transparent), transparent 32%),
			linear-gradient(to bottom, transparent 24%, var(--bg) 98%);
	}
	.sheet {
		position: relative;
		max-width: 940px;
		margin: clamp(-150px, -18vh, -96px) auto 0;
		padding: 0 22px 64px;
	}
	.head {
		display: flex;
		align-items: flex-end;
		gap: 26px;
	}
	.poster {
		flex: 0 0 152px;
		aspect-ratio: 2 / 3;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		border-radius: 9px;
		border: 1px solid color-mix(in srgb, var(--text) 18%, transparent);
		background: var(--bg-panel);
		box-shadow: 0 14px 38px color-mix(in srgb, #000 45%, transparent);
	}
	.poster img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.poster span {
		font-size: 12px;
		color: var(--text-muted);
	}
	.ident {
		min-width: 0;
		padding-bottom: 4px;
	}
	h1 {
		margin: 0;
		font-size: clamp(24px, 4vw, 34px);
		line-height: 1.12;
		letter-spacing: -0.015em;
		text-shadow: 0 1px 14px color-mix(in srgb, var(--bg) 70%, transparent);
	}
	.title-click {
		font: inherit;
		color: inherit;
		letter-spacing: inherit;
		text-align: left;
		margin: 0;
		padding: 0 4px;
		margin-left: -4px;
		border: none;
		background: none;
		cursor: text;
		border-radius: 6px;
	}
	.title-click:focus-visible {
		outline: none;
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 60%, transparent);
	}
	.alias-edit {
		width: min(100%, 640px);
		margin: 0;
		padding: 2px 8px;
		font-family: inherit;
		font-size: clamp(24px, 4vw, 34px);
		font-weight: 700;
		line-height: 1.12;
		letter-spacing: -0.015em;
		color: var(--text);
		background: color-mix(in srgb, #000 35%, transparent);
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.alias-edit:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.orig-title {
		margin: 4px 0 0;
		font-size: 13px;
		color: var(--text-muted);
	}
	.byline {
		margin: 7px 0 0;
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		font-size: 14px;
		color: var(--text-muted);
	}
	.byline .creator {
		color: var(--text);
	}
	.standing {
		margin: 9px 0 0;
		display: flex;
		gap: 12px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.standing .rated {
		color: var(--star);
		font-weight: 600;
	}
	.cols {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 236px;
		gap: 34px;
		margin-top: 34px;
	}
	.label {
		margin: 0 0 10px;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.13em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.review-click {
		margin: -6px -8px;
		padding: 6px 8px;
		border-radius: 8px;
		cursor: text;
		transition: background-color var(--motion-fast);
	}
	.review-blank {
		min-height: 34px;
	}
	.review-click:hover,
	.review-click:focus-visible {
		background: color-mix(in srgb, var(--bg-panel) 70%, transparent);
		outline: none;
	}
	.label-action {
		padding: 2px 9px;
		font-family: inherit;
		font-size: 11px;
		color: var(--text-muted);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 999px;
		cursor: pointer;
		transition: border-color var(--motion-fast), color var(--motion-fast);
	}
	.label-action:hover {
		color: var(--text);
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.label-action:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.label-action.quiet {
		border-color: transparent;
	}
	.review-edit {
		width: 100%;
		font-family: inherit;
		font-size: 14px;
		line-height: 1.6;
		color: var(--text);
		background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
		border: 1px solid var(--border);
		border-radius: 9px;
		padding: 10px 12px;
		resize: vertical;
	}
	.review-edit:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.edit-actions {
		display: flex;
		gap: 8px;
		margin-top: 8px;
	}
	.review {
		font-size: 14.5px;
		line-height: 1.65;
	}
	.review :global(p) {
		margin: 0 0 0.9em;
	}
	.review :global(h1),
	.review :global(h2),
	.review :global(h3) {
		font-size: 15px;
		margin: 1.2em 0 0.5em;
	}
	.review :global(a) {
		color: var(--accent);
	}
	.review :global(blockquote) {
		margin: 0 0 0.9em;
		padding-left: 12px;
		border-left: 2px solid var(--accent);
		color: var(--text-muted);
	}
	/* The history sits in the rail card under the log button. */
	.log {
		list-style: none;
		margin: 10px 0 0;
		padding: 8px 0 0;
		border-top: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
	}
	.log li {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 4px 0;
		font-size: 12.5px;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
	}
	.log li:last-child {
		border-bottom: none;
	}
	.log-day {
		flex: 0 0 74px;
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.log-phrase {
		min-width: 0;
	}
	.log-del {
		margin-left: auto;
		flex: none;
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		padding: 0;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		opacity: 0;
		transition: opacity var(--motion-fast), color var(--motion-fast);
	}
	.log li:hover .log-del,
	.log-del:focus-visible {
		opacity: 1;
	}
	.log-del:hover {
		color: var(--danger);
	}
	.log-del:disabled {
		cursor: default;
	}
	.log-del svg {
		width: 13px;
		height: 13px;
	}
	.rail {
		display: flex;
		flex-direction: column;
		gap: 20px;
	}
	/* Phone-only twin of .delete-media — see the comment on the markup. */
	.rail-delete {
		display: none;
	}
	.quick-log {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 6px;
		margin-top: 8px;
	}
	.quick-log input {
		min-width: 0;
		height: 32px;
		font-family: inherit;
		font-size: 13px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0 9px;
	}
	.quick-log button {
		height: 32px;
		padding: 0 11px;
		font-family: inherit;
		font-size: 12px;
		color: var(--text);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		cursor: pointer;
		transition: border-color var(--motion-fast), background-color var(--motion-fast);
	}
	.quick-log button:hover {
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.quick-log button:disabled {
		opacity: 0.55;
		cursor: default;
	}
	@media (max-width: 760px) {
		.sheet {
			margin-top: -84px;
			/* The action bar is fixed over the foot of the page; the extra
			   bottom padding is what keeps the last log line reachable
			   instead of sitting permanently underneath it. */
			padding: 0 14px 104px;
		}
		.head {
			gap: 16px;
		}
		.poster {
			flex-basis: 108px;
		}
		.cols {
			grid-template-columns: 1fr;
			gap: 26px;
		}
		/* Natural order on mobile: the review reads first (it's the point of
		   the page), then the rating and log controls below it. */

		/* These scroll away with the hero, which is fine — what was not fine
		   is a 30px target for a back button. The shadow is already doing the
		   work of reading over artwork; it just needed a bigger box. */
		.page-nav {
			top: 4px;
			left: 4px;
			gap: 0;
		}
		.page-nav button,
		.page-nav a {
			width: 44px;
			height: 44px;
			opacity: 1;
			filter: drop-shadow(0 1px 5px color-mix(in srgb, var(--bg) 92%, transparent))
				drop-shadow(0 0 2px color-mix(in srgb, var(--bg) 80%, transparent));
		}
		.page-nav svg {
			width: 21px;
			height: 21px;
		}
		/* Hover-revealed and pinned over the tab bar: unusable here. */
		.delete-media {
			display: none;
		}
		.rail-delete {
			display: block;
			width: 100%;
			min-height: 44px;
			padding: 0 12px;
			font-family: inherit;
			font-size: 13px;
			color: var(--text-muted);
			background: transparent;
			border: 1px solid var(--border);
			border-radius: var(--radius);
			cursor: pointer;
		}
		.rail-delete:active {
			color: var(--danger, #e06c75);
			background: var(--bg-hover);
		}
		.rail-delete:disabled {
			opacity: 0.55;
			cursor: default;
		}
		/* The action bar owns rating and logging down here, so the rail's own
		   controls would be the same two verbs a second time, in a smaller
		   shape, further from the thumb. The log HISTORY stays — that is the
		   part of the card worth scrolling to. */
		.rate-card,
		.quick-log,
		.label.empty-log {
			display: none;
		}
		.log {
			margin-top: 0;
			border-top: none;
		}
		/* Hover-revealed on a laptop; on touch there is no hover, so the entry
		   you just mis-logged was undeletable. */
		.log li {
			padding: 2px 0;
		}
		.log-del {
			width: 44px;
			height: 44px;
			opacity: 1;
		}
		.log-del svg {
			width: 15px;
			height: 15px;
		}
	}

</style>
