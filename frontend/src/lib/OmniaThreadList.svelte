<script lang="ts">
	// The mobile face of /omnia: a conversation list, not a chat. Opening the
	// tab lands here and a conversation is one tap away, the way a phone's
	// message list works — the drawer this replaced hid every earlier thread
	// behind a hamburger while the chat took the whole screen.
	import type { ThreadSummary } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { threadTimeLabel } from '$lib/stores.svelte';

	let {
		threads,
		oncompose,
		onopen
	}: {
		threads: ThreadSummary[];
		/** Start a fresh conversation. */
		oncompose: () => void;
		onopen: (id: string) => void;
	} = $props();

	let search = $state('');

	// The server already sorts newest-first; sorting again keeps the order
	// right when the list is filled from somewhere else (the preloader) or a
	// rename lands out of band.
	let rows = $derived.by(() => {
		const needle = search.trim().toLowerCase();
		const sorted = [...threads].sort((a, b) =>
			String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? ''))
		);
		if (!needle) return sorted;
		return sorted.filter((thread) =>
			`${thread.title} ${thread.preview}`.toLowerCase().includes(needle)
		);
	});
</script>

<div class="thread-index">
	<header>
		<h1>OMNIA</h1>
		<button type="button" class="compose" title="New conversation" aria-label="New conversation" onclick={oncompose}>
			<Glyph name="compose" />
		</button>
	</header>

	<div class="search">
		<Glyph name="search" />
		<!-- 16px or iOS zooms the page the moment the field takes focus. -->
		<input type="search" bind:value={search} placeholder="Search" aria-label="Search conversations" />
	</div>

	{#if rows.length === 0}
		<p class="empty">No conversations.</p>
	{:else}
		<ul>
			{#each rows as thread (thread.id)}
				<li>
					<button type="button" onclick={() => onopen(thread.id)}>
						<span class="avatar">
							<Glyph name={thread.kind === 'daily' ? 'calendar' : 'chat'} />
						</span>
						<span class="body">
							<span class="line">
								<span class="title">{thread.title}</span>
								<span class="meta">
									{threadTimeLabel(thread.updated_at)}
									<Glyph name="chevron" />
								</span>
							</span>
							{#if thread.preview}
								<span class="preview">{thread.preview}</span>
							{/if}
						</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.thread-index {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 6px var(--m-gutter) 8px;
	}
	h1 {
		margin: 0;
		font-size: var(--m-title);
		font-weight: 700;
		letter-spacing: -0.2px;
	}
	.compose {
		flex: 0 0 auto;
		display: grid;
		place-items: center;
		width: 44px;
		height: 44px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: none;
		color: var(--text);
	}
	.compose:active {
		background: var(--bg-hover);
	}
	.compose :global(svg) {
		display: block;
		width: var(--m-icon-bar);
		height: var(--m-icon-bar);
	}

	.search {
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		gap: 8px;
		height: 44px;
		margin: 0 var(--m-gutter) 6px;
		padding: 0 14px;
		border-radius: 999px;
		background: var(--bg-panel);
		color: var(--text-muted);
	}
	.search :global(svg) {
		flex: 0 0 auto;
		display: block;
		width: 17px;
		height: 17px;
	}
	.search input {
		flex: 1;
		min-width: 0;
		padding: 0;
		border: 0;
		background: none;
		color: var(--text);
		font-size: var(--m-row-text);
	}
	.search input:focus {
		outline: none;
		box-shadow: none;
	}
	/* Safari draws its own clear button inside a search field; the pill has
	   its own shape and does not want it. */
	.search input::-webkit-search-decoration,
	.search input::-webkit-search-cancel-button {
		appearance: none;
	}

	ul {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		margin: 0;
		padding: 0;
		list-style: none;
		-webkit-overflow-scrolling: touch;
	}
	li {
		position: relative;
	}
	/* iOS insets the hairline to where the text starts, so the avatars run as
	   one uninterrupted column: gutter + avatar + the gap after it. */
	li + li::before {
		content: '';
		position: absolute;
		top: 0;
		right: 0;
		left: calc(var(--m-gutter) + 44px + 12px);
		border-top: 1px solid var(--border);
	}
	li button {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		width: 100%;
		min-height: 72px;
		padding: 10px var(--m-gutter);
		border: 0;
		border-radius: 0;
		background: none;
		text-align: left;
	}
	li button:active {
		background: var(--bg-hover);
	}
	.avatar {
		flex: 0 0 auto;
		display: grid;
		place-items: center;
		width: 44px;
		height: 44px;
		border-radius: 50%;
		background: var(--bg-panel);
		color: var(--text-muted);
	}
	.avatar :global(svg) {
		display: block;
		width: 20px;
		height: 20px;
	}
	.body {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		/* Optically centres the two lines against the 44px avatar. */
		padding-top: 2px;
	}
	.line {
		display: flex;
		align-items: baseline;
		gap: 8px;
	}
	.title {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		font-size: var(--m-row-text);
		font-weight: 600;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.meta {
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		gap: 3px;
		color: var(--text-muted);
		font-size: var(--m-meta);
	}
	.meta :global(svg) {
		display: block;
		width: 13px;
		height: 13px;
	}
	.preview {
		display: -webkit-box;
		overflow: hidden;
		margin-top: 4px;
		color: var(--text-muted);
		font-size: 14px;
		line-height: 1.35;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
	}
	.empty {
		margin: 20px var(--m-gutter);
		color: var(--text-muted);
		font-size: var(--m-meta);
	}
</style>
