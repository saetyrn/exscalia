<script lang="ts">
	import { untrack } from 'svelte';
	import { api, encodePath } from '$lib/api';
	import type { MediaHub, MediaItem } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import MediaCard from '$lib/media/MediaCard.svelte';
	import FramedImage from '$lib/media/FramedImage.svelte';
	import ProfileEditor from '$lib/media/ProfileEditor.svelte';
	import { completionVerb, dayOf, mergeDayBookends, starText } from '$lib/media/helpers';
	import { loadHub, mediaHub, watchMediaLive } from '$lib/mediahub.svelte';
	import { notify } from '$lib/ui.svelte';

	let { data } = $props();

	let hub: MediaHub | null = $state(untrack(() => data.hub));
	let busy = $state(false);

	// Stat tiles default to this year, with a toggle for lifetime totals.
	let period: 'year' | 'lifetime' = $state('year');
	let periodStats = $derived(
		period === 'year' ? hub?.stats.this_year : hub?.stats.lifetime
	);

	// Click-to-edit name/location, matching the item page's review pattern.
	let editingField: 'name' | 'location' | null = $state(null);
	let fieldDraft = $state('');
	let fieldBox: HTMLInputElement | undefined = $state();

	// Favorites picker: click library entries to fill up to four slots in order.
	let editingFavs = $state(false);
	let favDraft: MediaItem[] = $state([]);
	let favSearch = $state('');
	let library: MediaItem[] | null = $state(null);

	// Profile customizer modal: null = closed, else which tab opens first.
	let editorTab: 'avatar' | 'banner' | null = $state(null);

	async function load() {
		try {
			hub = await loadHub();
		} catch {
			hub = null;
		}
	}

	$effect(() => {
		if (hub === null) void load();
	});

	// Adopt whatever the shared cache is holding. `load()` above covers the
	// first-ever visit; this covers the revalidation the loader kicked off
	// behind the cached render, and the refresh a media note change triggers —
	// either way the payload swaps in place, with nothing blanking on the way.
	$effect(() => {
		const fresh = mediaHub.hub;
		if (fresh && fresh !== untrack(() => hub)) hub = fresh;
	});

	// One app-wide subscription refetches the cache when a media note changes;
	// asking for it here covers the case where the preloader never ran.
	$effect(() => {
		watchMediaLive();
	});

	// Letterboxd-style feed phrasing: "You finished <Title>, rated ★★★★".
	// `stars` is kept apart so only the stars take the gold star color.
	function activityParts(line: {
		kind: string;
		value: string | null;
		media_type?: string;
		bookend?: unknown;
	}): {
		before: string;
		connector: string;
		stars: string;
	} {
		if (line.kind === 'finished' && line.bookend)
			return {
				before: `You ${completionVerb(line.media_type).toLowerCase()} `,
				connector: line.value ? ', rated ' : '',
				stars: line.value ? starText(line.value) : ''
			};
		if (line.kind === 'finished')
			return {
				before: 'You finished ',
				connector: line.value ? ', rated ' : '',
				stars: line.value ? starText(line.value) : ''
			};
		if (line.kind === 'watched') return { before: 'You watched ', connector: '', stars: '' };
		if (line.kind === 'started') return { before: 'You started ', connector: '', stars: '' };
		if (line.kind === 'rated')
			return { before: 'You rated ', connector: ' ', stars: starText(line.value) };
		if (line.kind === 'progress')
			return { before: `You reached ${line.value ?? ''} in `, connector: '', stars: '' };
		return { before: 'You logged a note on ', connector: '', stars: '' };
	}

	function ageOf(at: string): string {
		const days = Math.max(
			0,
			Math.floor((Date.now() - new Date(`${at}T00:00:00`).getTime()) / 86_400_000)
		);
		return days === 0 ? 'today' : `${days}d`;
	}

	function startFieldEdit(field: 'name' | 'location') {
		if (!hub) return;
		editingField = field;
		fieldDraft = field === 'name' ? hub.profile.name : hub.profile.location;
	}

	$effect(() => {
		if (editingField && fieldBox) {
			fieldBox.focus();
			fieldBox.select();
		}
	});

	async function saveField() {
		if (!editingField || busy) return;
		const field = editingField;
		busy = true;
		try {
			const result = await api.mediaSaveProfile({ [field]: fieldDraft });
			if (hub) hub.profile = result.profile;
			editingField = null;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	async function openFavEditor() {
		if (!hub) return;
		favDraft = [...hub.favorites];
		favSearch = '';
		editingFavs = true;
		if (library === null) {
			try {
				library = await api.mediaList({ sort: 'title' });
			} catch {
				library = [];
			}
		}
	}

	let favChoices = $derived.by(() => {
		const chosen = new Set(favDraft.map((f) => f.path));
		const needle = favSearch.trim().toLowerCase();
		return (library ?? []).filter(
			(item) =>
				!chosen.has(item.path) &&
				(!needle || `${item.title} ${item.creator ?? ''}`.toLowerCase().includes(needle))
		);
	});

	async function saveFavorites() {
		if (busy) return;
		busy = true;
		try {
			const result = await api.mediaSaveProfile({
				favorites: favDraft.map((f) => f.path)
			});
			if (hub) hub.favorites = result.favorites;
			editingFavs = false;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}
</script>

<div class="media-page">
	<h1 class="sr-only">Media profile</h1>

	{#if hub === null}
		<p class="state">Loading…</p>
	{:else}
		<header class="profile" class:has-banner={hub.profile.banner}>
			{#if hub.profile.banner}
				<div class="banner" aria-hidden="true">
					<FramedImage
						src={hub.profile.banner}
						zoom={hub.profile.banner_zoom}
						x={hub.profile.banner_x}
						y={hub.profile.banner_y}
					/>
				</div>
			{/if}
			<button
				type="button"
				class="banner-edit"
				title="Customize profile"
				aria-label="Customize profile picture and banner"
				onclick={() => (editorTab = 'avatar')}
			>
				<Glyph name="dots" />
			</button>
			<button
				type="button"
				class="avatar"
				title="Change profile picture"
				aria-label="Change profile picture"
				onclick={() => (editorTab = 'avatar')}
			>
				{#if hub.profile.avatar}
					<FramedImage
						src={hub.profile.avatar}
						zoom={hub.profile.avatar_zoom}
						x={hub.profile.avatar_x}
						y={hub.profile.avatar_y}
					/>
				{:else}
					<Glyph name="user" />
				{/if}
			</button>
			<div class="ident">
				{#if editingField === 'name'}
					<input
						bind:this={fieldBox}
						class="name-edit"
						bind:value={fieldDraft}
						disabled={busy}
						onblur={() => void saveField()}
						onkeydown={(e) => {
							if (e.key === 'Enter') void saveField();
							if (e.key === 'Escape') editingField = null;
						}}
					/>
				{:else}
					<button type="button" class="name" title="Click to edit" onclick={() => startFieldEdit('name')}>
						{hub.profile.name}
					</button>
				{/if}
				{#if editingField === 'location'}
					<input
						bind:this={fieldBox}
						class="location-edit"
						bind:value={fieldDraft}
						placeholder="Location or bio…"
						disabled={busy}
						onblur={() => void saveField()}
						onkeydown={(e) => {
							if (e.key === 'Enter') void saveField();
							if (e.key === 'Escape') editingField = null;
						}}
					/>
				{:else}
					<button
						type="button"
						class="location"
						class:empty={!hub.profile.location}
						title="Click to edit"
						onclick={() => startFieldEdit('location')}
					>
						{hub.profile.location || 'Add a line…'}
					</button>
				{/if}
			</div>
			<div class="stats-wrap">
				<div class="period" role="group" aria-label="Stats period">
					<button
						type="button"
						class:active={period === 'year'}
						onclick={() => (period = 'year')}
					>
						This year
					</button>
					<button
						type="button"
						class:active={period === 'lifetime'}
						onclick={() => (period = 'lifetime')}
					>
						Lifetime
					</button>
				</div>
				<div class="stats-body">
					<div class="stats" role="group" aria-label="Library stats">
						<a class="stat" href="/media/library?type=book">
							<span class="stat-n">{periodStats?.pages ?? 0}</span>
							<span class="stat-l">Pages read</span>
						</a>
						<a class="stat" href="/media/library?type=movie">
							<span class="stat-n">{periodStats?.movies ?? 0}</span>
							<span class="stat-l">Movies watched</span>
						</a>
						<a class="stat" href="/media/library?type=tv">
							<span class="stat-n">{periodStats?.shows ?? 0}</span>
							<span class="stat-l">Shows watched</span>
						</a>
						<a class="stat" href="/media/library?type=game">
							<span class="stat-n">{periodStats?.games ?? 0}</span>
							<span class="stat-l">Games played</span>
						</a>
					</div>
					<!-- Watchlist is a live count, not a period total — its own tile
					     beside the toggled stack. -->
					<a class="tile" href="/media/watchlist">
						<span class="tile-n">{hub.stats.watchlist}</span>
						<span class="tile-l">Watchlist</span>
					</a>
				</div>
			</div>
		</header>

		{#if editorTab}
			<ProfileEditor
				profile={hub.profile}
				initialTab={editorTab}
				onclose={() => (editorTab = null)}
				onsaved={(p) => {
					if (hub) hub.profile = p;
				}}
			/>
		{/if}

		<div class="columns">
			<div class="main">
				<section>
					<div class="sect-head">
						<h2>Favorites</h2>
						<button class="sect-action" onclick={() => void openFavEditor()} disabled={editingFavs}>
							Edit
						</button>
					</div>
					{#if editingFavs}
						<div class="fav-editor">
							<div class="fav-row">
								{#each favDraft as fav (fav.path)}
									<div class="fav-slot">
										<MediaCard item={fav} compact />
										<button
											class="fav-x"
											title="Remove from favorites"
											aria-label="Remove {fav.title} from favorites"
											onclick={() => (favDraft = favDraft.filter((f) => f.path !== fav.path))}
										>
											<Glyph name="x" />
										</button>
									</div>
								{/each}
								{#each Array.from({ length: 4 - favDraft.length }) as _, i (i)}
									<div class="fav-empty"></div>
								{/each}
							</div>
							<input
								type="search"
								class="fav-search"
								placeholder="Find something to add…"
								aria-label="Search your library"
								bind:value={favSearch}
							/>
							{#if favDraft.length < 4 && favChoices.length}
								<ul class="fav-choices">
									{#each favChoices.slice(0, 8) as choice (choice.path)}
										<li>
											<button
												type="button"
												onclick={() => {
													if (favDraft.length < 4) favDraft = [...favDraft, choice];
												}}
											>
												{choice.title}
												{#if choice.year}<span>{choice.year}</span>{/if}
											</button>
										</li>
									{/each}
								</ul>
							{/if}
							<div class="fav-actions">
								<button class="sect-action" onclick={() => void saveFavorites()} disabled={busy}>
									{busy ? 'Saving…' : 'Save'}
								</button>
								<button class="sect-action quiet" onclick={() => (editingFavs = false)} disabled={busy}>
									Cancel
								</button>
							</div>
						</div>
					{:else if hub.favorites.length === 0}
						<p class="none">Nothing here yet — press Edit to pick up to four.</p>
					{:else}
						<div class="fav-row">
							{#each hub.favorites as fav (fav.path)}
								<MediaCard item={fav} compact />
							{/each}
						</div>
					{/if}
				</section>

				<section>
					<div class="sect-head">
						<h2>Recent activity</h2>
						<a class="sect-action" href="/media/activity">All</a>
					</div>
					{#if hub.recent.length === 0}
						<p class="none">No logged activity yet.</p>
					{:else}
						<div class="recent-row">
							{#each hub.recent as entry (entry.item.path)}
								<MediaCard item={entry.item} compact />
							{/each}
						</div>
					{/if}
				</section>
			</div>

			<aside class="rail">
				<section>
					<div class="sect-head">
						<h2><a href="/media/watchlist">Watchlist</a></h2>
						<span class="sect-count">{hub.stats.watchlist}</span>
					</div>
					{#if hub.watchlist.length === 0}
						<p class="none">Empty — plan something.</p>
					{:else}
						<div class="watch-strip">
							{#each hub.watchlist.slice(0, 5) as item (item.path)}
								<a
									class="watch-poster"
									href={'/media/' + encodePath(item.path)}
									title={item.title}
								>
									{#if item.cover}
										<img src={item.cover} alt="" loading="lazy" />
									{:else}
										<span>{item.title.slice(0, 12)}</span>
									{/if}
								</a>
							{/each}
						</div>
					{/if}
				</section>

				<section>
					<div class="sect-head">
						<h2>Ratings</h2>
						<span class="sect-count">{hub.ratings.total}</span>
					</div>
					{#if hub.ratings.total === 0}
						<p class="none">Nothing rated yet.</p>
					{:else}
						{@const peak = Math.max(...hub.ratings.counts, 1)}
						<div class="histogram" role="img" aria-label="Rating distribution, half a star to five stars">
							<span class="histo-end">★</span>
							<div class="histo-bars">
								{#each hub.ratings.counts as count, i (i)}
									<div
										class="histo-bar"
										style:height="{count === 0 ? 4 : Math.max(10, (count / peak) * 100)}%"
										title="{(i + 1) / 2}★ — {count}"
									></div>
								{/each}
							</div>
							<span class="histo-end">★★★★★</span>
						</div>
					{/if}
				</section>

				<section>
					<div class="sect-head">
						<h2><a href="/media/activity">Activity</a></h2>
					</div>
					{#if hub.diary.length === 0}
						<p class="none">No entries yet.</p>
					{:else}
						<ul class="feed">
							{#each mergeDayBookends(hub.diary) as line, index (index)}
								{@const parts = activityParts(line)}
								<li>
									<a href={'/media/' + encodePath(line.path)}>
										<span class="feed-text">
											{parts.before}
											<strong>{line.title}</strong>{parts.connector}{#if parts.stars}<span
													class="rated-stars">{parts.stars}</span
												>{/if}
											<span class="feed-when">on {dayOf(line.at)}</span>
										</span>
										<span class="feed-age">{ageOf(line.at)}</span>
									</a>
								</li>
							{/each}
						</ul>
					{/if}
				</section>
			</aside>
		</div>
	{/if}
</div>

<style>
	.media-page {
		padding: 26px 28px 48px;
		max-width: 1180px;
		margin: 0 auto;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}
	.state,
	.none {
		padding: 18px 4px;
		color: var(--text-muted);
		font-size: 12px;
	}
	.state {
		text-align: center;
		padding: 48px 10px;
	}

	/* -- profile header ---------------------------------------------------- */
	.profile {
		position: relative;
		display: flex;
		align-items: center;
		gap: 18px;
		margin-bottom: 22px;
		padding: 20px 16px;
		overflow: hidden;
	}
	.profile.has-banner {
		min-height: 210px;
	}
	/* Everything but the banner layer sits above it. */
	.profile > :not(.banner):not(.banner-edit) {
		position: relative;
		z-index: 1;
	}
	/* Full-width banner behind the header, fading into the page toward the
	   right (and bottom) so it dissolves before it reaches the stats. */
	.banner {
		position: absolute;
		inset: 0;
		z-index: 0;
	}
	.banner::after {
		content: '';
		position: absolute;
		inset: 0;
		background:
			linear-gradient(to right, transparent 16%, var(--bg) 82%),
			linear-gradient(to bottom, transparent 25%, var(--bg)),
			linear-gradient(to right, color-mix(in srgb, #000 55%, transparent), transparent 52%);
	}
	.banner-edit {
		position: absolute;
		top: 10px;
		right: 10px;
		z-index: 2;
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		padding: 0;
		border: 1px solid color-mix(in srgb, #fff 12%, transparent);
		border-radius: 8px;
		background: color-mix(in srgb, #000 50%, transparent);
		color: #fff;
		cursor: pointer;
		transition: background var(--motion-fast);
	}
	.banner-edit:hover {
		background: color-mix(in srgb, #000 68%, transparent);
	}
	.banner-edit :global(svg) {
		width: 18px;
		height: 18px;
	}
	.avatar {
		width: 112px;
		height: 112px;
		flex: none;
		display: grid;
		place-items: center;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 0;
		background: var(--bg-panel);
		color: var(--text-muted);
		overflow: hidden;
		cursor: pointer;
		box-shadow: 0 4px 18px color-mix(in srgb, #000 45%, transparent);
		transition: border-color var(--motion-fast);
	}
	.avatar:hover {
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
	}
	.avatar :global(svg) {
		width: 40px;
		height: 40px;
	}
	.ident {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.name,
	.location {
		padding: 0;
		border: none;
		background: none;
		font-family: inherit;
		text-align: left;
		color: var(--text);
		cursor: text;
	}
	.name {
		font-size: 24px;
		font-weight: 700;
		letter-spacing: 0.2px;
	}
	.location {
		font-size: 12px;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.6px;
	}
	.location.empty {
		opacity: 0;
		transition: opacity var(--motion-fast);
	}
	.profile:hover .location.empty,
	.location.empty:focus-visible {
		opacity: 0.7;
	}
	/* No hover to reveal it on touch screens. */
	@media (hover: none) {
		.location.empty {
			opacity: 0.7;
		}
	}
	.name-edit,
	.location-edit {
		font-family: inherit;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 7px;
		padding: 2px 8px;
	}
	.name-edit {
		font-size: 22px;
		font-weight: 700;
		width: min(340px, 60vw);
	}
	.location-edit {
		font-size: 12px;
		width: min(340px, 60vw);
	}
	.stats-wrap {
		margin-left: auto;
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 6px;
		min-width: 190px;
	}
	.period {
		align-self: flex-end;
		display: inline-flex;
		gap: 2px;
		padding: 2px;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 999px;
	}
	.period button {
		padding: 4px 12px;
		font-family: inherit;
		font-size: 11px;
		letter-spacing: 0.4px;
		border: none;
		background: none;
		color: var(--text-muted);
		border-radius: 999px;
		cursor: pointer;
	}
	.period button.active {
		background: var(--accent-soft);
		color: var(--text);
	}
	/* The toggled stack sits left; the watchlist tile sits to its right. */
	.stats-body {
		display: flex;
		align-items: center;
		gap: 28px;
	}
	/* Vertical stack: one stat per row, numbers column-aligned on the left. */
	.stats {
		display: flex;
		flex-direction: column;
	}
	.stat {
		display: flex;
		align-items: baseline;
		gap: 9px;
		padding: 1px 0;
		text-decoration: none;
	}
	.stat-n {
		flex: 0 0 auto;
		min-width: 34px;
		text-align: right;
		font-size: 14px;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--text);
	}
	.stat-l {
		font-size: 10px;
		letter-spacing: 0.6px;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.stat:hover .stat-l {
		color: var(--text);
	}
	/* Watchlist tile — the original centered number-over-label format. */
	.tile {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		text-decoration: none;
	}
	.tile-n {
		font-size: 22px;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--text);
	}
	.tile-l {
		font-size: 10px;
		letter-spacing: 0.8px;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.tile:hover .tile-l {
		color: var(--text);
	}

	/* -- sections ----------------------------------------------------------- */
	.columns {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 300px;
		gap: 40px;
	}
	section {
		margin-bottom: 28px;
	}
	.sect-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 10px;
		border-bottom: 1px solid var(--border);
		padding-bottom: 6px;
		margin-bottom: 12px;
	}
	.sect-head h2 {
		margin: 0;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 1px;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.sect-head h2 a {
		color: inherit;
		text-decoration: none;
	}
	.sect-head h2 a:hover {
		color: var(--text);
	}
	.sect-count {
		font-size: 11px;
		color: var(--text-muted);
	}
	.sect-action {
		padding: 0;
		border: none;
		background: none;
		font-family: inherit;
		font-size: 11px;
		letter-spacing: 0.6px;
		text-transform: uppercase;
		color: var(--text-muted);
		text-decoration: none;
		cursor: pointer;
		transition: color var(--motion-fast);
	}
	.sect-action:hover {
		color: var(--text);
	}
	.sect-action:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.fav-row,
	.recent-row {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 14px;
	}
	.fav-slot {
		position: relative;
	}
	.fav-x {
		position: absolute;
		top: 6px;
		right: 6px;
		width: 24px;
		height: 24px;
		display: grid;
		place-items: center;
		padding: 0;
		border: none;
		border-radius: 6px;
		background: color-mix(in srgb, var(--bg) 72%, transparent);
		color: var(--text);
		cursor: pointer;
	}
	.fav-x :global(svg) {
		width: 13px;
		height: 13px;
	}
	.fav-empty {
		aspect-ratio: 2 / 3;
		border: 1px dashed var(--border);
		border-radius: 8px;
	}
	.fav-editor {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.fav-search {
		height: 32px;
		padding: 0 12px;
		font-family: inherit;
		font-size: 13px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.fav-search:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.fav-choices {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-height: 240px;
		overflow-y: auto;
	}
	.fav-choices button {
		width: 100%;
		display: flex;
		justify-content: space-between;
		gap: 8px;
		padding: 6px 9px;
		font-family: inherit;
		font-size: 13px;
		text-align: left;
		color: var(--text);
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		cursor: pointer;
	}
	.fav-choices button:hover {
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
	}
	.fav-choices button span {
		color: var(--text-muted);
		font-size: 12px;
	}
	.fav-actions {
		display: flex;
		gap: 14px;
	}
	.quiet {
		opacity: 0.75;
	}

	/* -- right rail --------------------------------------------------------- */
	.watch-strip {
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		gap: 6px;
	}
	.watch-poster {
		aspect-ratio: 2 / 3;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 5px;
		background: var(--bg-panel);
		text-decoration: none;
		transition: border-color var(--motion-fast);
	}
	.watch-poster:hover {
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
	}
	.watch-poster img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.watch-poster span {
		font-size: 9px;
		color: var(--text-muted);
		text-align: center;
		padding: 2px;
	}
	/* Letterboxd-style rating histogram: ten buckets, 1 on the left. */
	.histogram {
		display: flex;
		align-items: flex-end;
		gap: 8px;
	}
	.histo-end {
		font-size: 10px;
		color: var(--text-muted);
		padding-bottom: 1px;
	}
	.histo-bars {
		flex: 1;
		display: flex;
		align-items: flex-end;
		gap: 2px;
		height: 64px;
	}
	.histo-bar {
		flex: 1;
		min-height: 2px;
		border-radius: 2px 2px 0 0;
		background: color-mix(in srgb, var(--text-muted) 42%, var(--bg-panel));
	}
	.histo-bar:hover {
		background: var(--accent);
	}

	/* The dotted activity feed. */
	.feed {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.feed li {
		position: relative;
		padding-left: 18px;
	}
	/* The connecting line… */
	.feed li::before {
		content: '';
		position: absolute;
		left: 4px;
		top: 0;
		bottom: 0;
		width: 1px;
		background: var(--border);
	}
	.feed li:first-child::before {
		top: 12px;
	}
	.feed li:last-child::before {
		bottom: calc(100% - 12px);
	}
	/* …and the dot on it. */
	.feed li::after {
		content: '';
		position: absolute;
		left: 1px;
		top: 9px;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: color-mix(in srgb, var(--text-muted) 55%, var(--bg-panel));
	}
	.feed a {
		display: flex;
		align-items: baseline;
		gap: 8px;
		padding: 4px 6px;
		border-radius: var(--radius);
		text-decoration: none;
		color: var(--text-muted);
		font-size: 12px;
		line-height: 1.45;
	}
	.feed a:hover {
		background: var(--bg-hover);
	}
	.feed-text {
		min-width: 0;
	}
	.feed-text strong {
		color: var(--text);
		font-weight: 600;
	}
	.rated-stars {
		color: var(--star);
		white-space: nowrap;
	}
	.feed-when {
		white-space: nowrap;
	}
	.feed-age {
		margin-left: auto;
		flex: none;
		font-size: 11px;
		color: color-mix(in srgb, var(--text-muted) 70%, transparent);
	}

	@media (max-width: 900px) {
		.columns {
			grid-template-columns: 1fr;
			gap: 0;
		}
		.profile {
			flex-wrap: wrap;
		}
		.stats-wrap {
			margin-left: 0;
			align-items: flex-start;
		}
		.period {
			align-self: flex-start;
		}
	}
	@media (max-width: 760px) {
		.media-page {
			padding: 20px var(--m-gutter) 32px;
		}
		/* Section-label sizing; the 1px tracking drops to the shared 0.5px so
		   these read the same as every other section head in the app. Colors
		   stay the media look's. */
		.sect-head h2 {
			font-size: var(--m-label);
			letter-spacing: 0.5px;
		}
		.feed a {
			font-size: var(--m-meta);
		}
		/* 16px or iOS zooms the page in on focus; the height goes with it, both
		   so the taller text fits and so the control is a real tap target. */
		.fav-search {
			height: 44px;
			font-size: 16px;
		}
		.location-edit {
			font-size: var(--m-row-text);
		}
	}
</style>
