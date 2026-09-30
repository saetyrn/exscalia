<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { tick } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from '$lib/motion';
	import { api, encodePath, type SearchResult } from '$lib/api';
	import { vault } from '$lib/stores.svelte';

	let q = $derived(page.url.searchParams.get('q') ?? '');
	let input = $state('');
	let folder = $state('');
	let tag = $state('');
	let resultType: 'all' | 'note' | 'task' = $state('all');
	let createdFrom = $state('');
	let modifiedFrom = $state('');
	let results: SearchResult[] = $state([]);
	let searching = $state(false);
	let error: string | null = $state(null);
	let semanticAvailable = $state(false);
	let filtersOpen = $state(false);
	let selected = $state(0);
	let resultList: HTMLUListElement | undefined = $state();
	let queryInput: HTMLInputElement | undefined = $state();
	let searchTimer: ReturnType<typeof setTimeout> | null = null;
	let searchGeneration = 0;

	$effect(() => {
		input = q;
		folder = page.url.searchParams.get('folder') ?? '';
		tag = page.url.searchParams.get('tag') ?? '';
		resultType = (page.url.searchParams.get('result_type') as typeof resultType) || 'all';
		createdFrom = page.url.searchParams.get('created_from') ?? '';
		modifiedFrom = page.url.searchParams.get('modified_from') ?? '';
	});

	$effect(() => {
		const query = input;
		folder;
		tag;
		resultType;
		createdFrom;
		modifiedFrom;
		if (searchTimer) clearTimeout(searchTimer);
		searchTimer = setTimeout(() => void run(query), 240);
		return () => {
			if (searchTimer) clearTimeout(searchTimer);
		};
	});

	// While the server's model loads, a search is keyword hits only. A cold
	// load can take tens of seconds, so ask again with backoff (1.5 s, 3 s,
	// 6 s, 12 s) until the semantic pass is in or off; a new query starts over.
	const WARMING_RETRIES = 4;

	async function run(value = input, refresh = false, attempt = 0) {
		const query = value.trim();
		const generation = ++searchGeneration;
		if (!query) {
			results = [];
			error = null;
			searching = false;
			return;
		}
		// A refresh swaps the list in place instead of flashing the spinner.
		if (!refresh) {
			searching = true;
			error = null;
		}
		try {
			const response = await api.unifiedSearch(query, {
				folder: folder || undefined,
				tag: tag || undefined,
				result_type: resultType,
				created_from: createdFrom || undefined,
				modified_from: modifiedFrom || undefined,
				include_paper: true
			});
			if (generation !== searchGeneration) return;
			results = response.results;
			semanticAvailable = response.semantic_available;
			selected = refresh ? Math.min(selected, Math.max(0, results.length - 1)) : 0;
			// The server answered before its embedding model finished loading,
			// so these are keyword hits only: ask again for the rest.
			if (response.semantic_state === 'warming' && attempt < WARMING_RETRIES) {
				searchTimer = setTimeout(() => {
					if (generation === searchGeneration) void run(query, true, attempt + 1);
				}, 1500 * 2 ** attempt);
			}
		} catch (cause) {
			if (generation !== searchGeneration) return;
			if (refresh) return; // keep the keyword hits already shown
			error = (cause as Error).message;
			results = [];
		} finally {
			if (generation === searchGeneration) searching = false;
		}
	}

	function submit(event?: Event) {
		event?.preventDefault();
		const query = input.trim();
		if (!query) return;
		const params = new URLSearchParams({ q: query });
		if (folder) params.set('folder', folder);
		if (tag) params.set('tag', tag);
		if (resultType !== 'all') params.set('result_type', resultType);
		if (createdFrom) params.set('created_from', createdFrom);
		if (modifiedFrom) params.set('modified_from', modifiedFrom);
		goto(`/search?${params}`);
	}

	function clearFilters() {
		folder = '';
		tag = '';
		resultType = 'all';
		createdFrom = '';
		modifiedFrom = '';
		submit();
	}

	// Arrowing past the fold must not leave the highlight off-screen. Arrow
	// keys only: an effect on `selected` would also fire on hover and every
	// time a search lands (the list remounts), scrolling the page away from
	// the query box mid-typing.
	async function revealSelected() {
		await tick();
		resultList?.querySelector('li.selected')?.scrollIntoView({ block: 'nearest' });
	}

	// The result link and the Enter shortcut must open the same place.
	function resultHref(result: SearchResult) {
		return result.durable_url ?? result.url ?? `/note/${encodePath(result.path)}`;
	}

	function keyboard(event: KeyboardEvent) {
		const target = event.target instanceof HTMLElement ? event.target : null;
		const typing =
			!!target && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable);

		// Enter opens the selected result only from the query box or the page
		// itself; a focused button, link or filter keeps its own Enter.
		if (event.key === 'Enter') {
			if (target?.closest('button, a, select, summary, [contenteditable]')) return;
			if (typing && target !== queryInput) return;
			if (results[selected]) {
				event.preventDefault();
				goto(resultHref(results[selected]));
			}
			return;
		}

		// Arrow keys must move the caret while you're editing the query or a
		// filter; only steer the result list from outside a form field.
		if (typing) return;

		if (event.key === 'ArrowDown') {
			event.preventDefault();
			selected = Math.min(results.length - 1, selected + 1);
			void revealSelected();
		}
		if (event.key === 'ArrowUp') {
			event.preventDefault();
			selected = Math.max(0, selected - 1);
			void revealSelected();
		}
	}

	let folders = $derived([...new Set(vault.folders.filter((value) => !value.startsWith('.')))]);
</script>

<svelte:window onkeydown={keyboard} />

<div class="search-page">
	<form onsubmit={submit}>
		<input type="search" placeholder="Search your vault…" bind:value={input} bind:this={queryInput} />
		<button class="primary" type="submit">Search</button>
	</form>

	<div class="search-meta">
		<span>{semanticAvailable ? 'Blending exact and meaning-based matches' : 'Exact search · semantic index unavailable'}</span>
		<button onclick={() => (filtersOpen = !filtersOpen)}>
			Filters{folder || tag || resultType !== 'all' || createdFrom || modifiedFrom ? ' •' : ''}
		</button>
	</div>

	{#if filtersOpen}
		<div class="filters" transition:fly={{ y: -5, duration: 150 }}>
			<label>
				<span>Type</span>
				<select bind:value={resultType}>
					<option value="all">Notes and tasks</option>
					<option value="note">Notes only</option>
					<option value="task">Tasks only</option>
				</select>
			</label>
			<label>
				<span>Folder</span>
				<select bind:value={folder}>
					<option value="">Every folder</option>
					{#each folders as value (value)}<option value={value}>{value}</option>{/each}
				</select>
			</label>
			<label>
				<span>Tag</span>
				<input bind:value={tag} placeholder="#tag" />
			</label>
			<label>
				<span>Created after</span>
				<input type="date" bind:value={createdFrom} />
			</label>
			<label>
				<span>Modified after</span>
				<input type="date" bind:value={modifiedFrom} />
			</label>
			<div class="filter-actions">
				<button onclick={clearFilters}>Clear</button>
				<button class="primary" onclick={submit}>Apply</button>
			</div>
		</div>
	{/if}

	{#if searching}
		<p class="muted" transition:fade={{ duration: 130 }}>Searching…</p>
	{:else if error}
		<p class="error" transition:fly={{ y: -4, duration: 160, easing: cubicOut }}>{error}</p>
	{:else if input.trim() && results.length === 0}
		<p class="muted" transition:fade={{ duration: 150 }}>No results for “{input.trim()}”. Try fewer filters or a related phrase.</p>
	{:else}
		<ul class="results" bind:this={resultList}>
			{#each results as result, index ((result.id ?? result.path) + index)}
				<li
					class:selected={selected === index}
					onmouseenter={() => (selected = index)}
					in:fly={{ y: 5, delay: Math.min(index * 18, 130), duration: 170, easing: cubicOut }}
				>
					<div class="result-head">
						<span class="type"
							>{result.type === 'task' ? 'Task' : result.type === 'paper' ? 'Paper' : 'Note'}</span
						>
						<!-- A handwriting hit is a page inside a notebook, which the
						     /note/ convention cannot address — the server sends the
						     link for those and everything else keeps the default. -->
							<a href={resultHref(result)}
							>{result.title || result.path}</a
						>
						{#if result.status}<span class="status">{result.status.replace('_', ' ')}</span>{/if}
					</div>
					<span class="path">{result.path}</span>
					{#if result.highlights?.length}
						<p class="snippet">
							{#each result.highlights as segment, segmentIndex (segmentIndex)}
								{#if segment.highlight}<mark>{segment.text}</mark>{:else}{segment.text}{/if}
							{/each}
						</p>
					{:else if result.snippet}
						<p class="snippet">{result.snippet}</p>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.search-page {
		max-width: 820px;
		width: 100%;
		margin: 0 auto;
		padding: 28px 32px 60px;
		overflow-y: auto;
	}
	form {
		display: flex;
		gap: 8px;
	}
	form input {
		flex: 1;
	}
	.search-meta {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin: 9px 0 16px;
		color: var(--text-muted);
		font-size: 11px;
	}
	.search-meta button {
		padding: 4px 7px;
		color: var(--accent);
	}
	.filters {
		display: grid;
		grid-template-columns: repeat(3, minmax(130px, 1fr));
		gap: 9px;
		margin-bottom: 18px;
		padding: 12px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--bg-panel);
	}
	.filters label {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.filters label > span {
		color: var(--text-muted);
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
	}
	.filter-actions {
		display: flex;
		align-items: flex-end;
		justify-content: flex-end;
		gap: 6px;
	}
	.results {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.results li {
		padding: 11px 9px;
		border-bottom: 1px solid var(--border);
		border-radius: 7px;
		transition: background-color var(--motion-base) var(--ease-out);
	}
	.results li:hover,
	.results li.selected {
		background: color-mix(in srgb, var(--bg-panel) 78%, transparent);
	}
	.result-head {
		display: flex;
		align-items: center;
		gap: 7px;
	}
	.result-head a {
		color: var(--accent);
		font-weight: 650;
		text-decoration: none;
	}
	.type,
	.status {
		padding: 2px 5px;
		border-radius: 4px;
		background: var(--bg-hover);
		color: var(--text-muted);
		font-size: 9px;
		font-weight: 700;
		text-transform: uppercase;
	}
	.status {
		margin-left: auto;
		color: var(--accent);
	}
	.path {
		display: block;
		margin-top: 2px;
		color: var(--text-muted);
		font: 11px var(--font-mono);
	}
	.snippet {
		color: var(--text-muted);
		font-size: 13px;
		margin: 5px 0 0;
	}
	mark {
		padding: 0 1px;
		border-radius: 2px;
		background: var(--accent-soft);
		color: var(--accent);
	}
	.muted {
		color: var(--text-muted);
	}
	.error {
		color: var(--danger);
	}
	@media (max-width: 760px) {
		.search-page {
			padding: 16px var(--m-gutter) 40px;
		}
		.filters {
			grid-template-columns: 1fr 1fr;
		}
		/* Section-label sizing without the uppercase — these read as field
		   names above their control, not as headings over a group. */
		.filters label > span {
			font-size: var(--m-label);
			font-weight: 600;
			text-transform: none;
		}
		.filter-actions {
			grid-column: 1 / -1;
		}
		.results li {
			min-height: var(--m-row-h);
			padding: 12px 10px;
			border-radius: 8px;
		}
		.type,
		.status {
			font-size: 10px;
		}
	}
</style>
