<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, encodePath, type HistoryVersion } from '$lib/api';
	import DiffView from '$lib/DiffView.svelte';
	import { appendOlder, dayBreak, sizeDelta } from '$lib/historylist';
	import { refreshNotes } from '$lib/stores.svelte';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';

	let { data } = $props();

	let path = $derived(page.url.searchParams.get('path') ?? '');
	let versions: HistoryVersion[] = $state([]);
	let selected: HistoryVersion | null = $state(null);
	let content = $state('');
	let currentContent = $state('');
	let previewLoading = $state(false);
	// The listing carries no content: the one-line preview comes from the
	// version's content once inspect() has it, 'Empty note' for a blank one.
	let preview = $derived.by(() =>
		selected && !previewLoading
			? content.split(/\r\n|\r|\n/).join(' ').slice(0, 180) || 'Empty note'
			: ''
	);
	let error: string | null = $state(null);
	let hasMore = $state(false);
	let loadingOlder = $state(false);
	// Bumped per inspect() and on route change: only the latest preview
	// fetch may write the panes.
	let inspectGen = 0;
	// Bumped on route change: an older page only lands on the list it was
	// asked for.
	let listGen = 0;
	// Rows the server has handed out, repeats included: the next page's skip.
	// A save between fetches slides the window, so this runs ahead of
	// versions.length by the repeats appendOlder dropped.
	let fetched = 0;

	// The route load() fetches during navigation (including ?path= changes,
	// which don't remount this component) — sync local state from its result.
	$effect(() => {
		const first = data.versions ?? [];
		versions = first;
		hasMore = data.hasMore;
		// From the loader's array, not `versions`: reading that here would
		// rerun this reset whenever loadOlder() appends.
		fetched = first.length;
		loadingOlder = false;
		listGen++;
		error = data.error;
		selected = null;
		content = '';
		currentContent = '';
		previewLoading = false;
		inspectGen++;
	});

	async function inspect(version: HistoryVersion) {
		const gen = ++inspectGen;
		selected = version;
		content = '';
		currentContent = '';
		previewLoading = true;
		try {
			const [contentResult, note] = await Promise.all([
				api.historyContent(path, version.sha, version.version_path),
				api.readNote(path).catch(() => null) // the note may be deleted
			]);
			if (gen !== inspectGen) return;
			content = contentResult.content;
			currentContent = note?.content ?? '';
		} catch (cause) {
			if (gen !== inspectGen) return;
			// Nothing loaded: don't leave a header and Restore over empty panes.
			selected = null;
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			if (gen === inspectGen) previewLoading = false;
		}
	}

	async function loadOlder() {
		const gen = listGen;
		loadingOlder = true;
		try {
			const older = await api.history(path, { skip: fetched });
			if (gen !== listGen) return;
			fetched += older.versions.length;
			versions = appendOlder(versions, older.versions);
			hasMore = older.has_more;
		} catch (cause) {
			if (gen !== listGen) return;
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			if (gen === listGen) loadingOlder = false;
		}
	}

	async function restore(mode: 'replace' | 'copy') {
		if (!selected) return;
		let target: string | undefined;
		if (mode === 'replace') {
			const confirmed = await confirmDialog(
				`Restore ${selected.short_sha} as the current version of “${path}”? The current note will be snapshotted first.`,
				{ title: 'Restore version', confirmLabel: 'Restore' }
			);
			if (!confirmed) return;
		} else {
			target =
				(await promptDialog('Choose a path for the restored copy:', {
					title: 'Restore as copy',
					value: path.replace(/\.md$/, ' (restored).md'),
					confirmLabel: 'Create copy'
				})) ?? undefined;
			if (!target) return;
			if (!target.endsWith('.md')) target += '.md';
		}
		try {
			const note = await api.restoreHistory({
				path,
				sha: selected.sha,
				version_path: selected.version_path,
				mode,
				target_path: target
			});
			await refreshNotes();
			notify(mode === 'replace' ? 'Version restored.' : 'Restored copy created.', {
				tone: 'success'
			});
			await goto(`/note/${encodePath(note.path)}`);
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}
</script>

<div class="history-page">
	<header>
		<div>
			<a href="/note/{encodePath(path)}">← Back to note</a>
			<h1>Version history</h1>
			<p>{path}</p>
		</div>
	</header>

	{#if error}
		<p class="state error">{error}</p>
	{:else if versions.length === 0}
		<p class="state">No committed versions are available yet.</p>
	{:else}
		<div class="history-layout">
			<aside aria-label="Versions">
				{#each versions as version, i (version.sha)}
					{@const day = dayBreak(versions, i)}
					{@const delta = sizeDelta(versions, i)}
					{#if day}
						<h3 class="day">{day}</h3>
					{/if}
					<button
						class:active={selected?.sha === version.sha}
						disabled={version.deleted}
						onclick={() => inspect(version)}
					>
						<strong>{new Date(version.committed_at).toLocaleTimeString()}</strong>
						<span>{version.message}{version.deleted ? ' · note absent' : ''}</span>
						<small>{version.short_sha} · {version.author}{delta ? ` · ${delta}` : ''}</small>
					</button>
				{/each}
				{#if hasMore}
					<button class="older" disabled={loadingOlder} onclick={loadOlder}>Older</button>
				{/if}
			</aside>
			<section>
				{#if selected}
					<div class="version-head">
						<div>
							<h2>{selected.message}</h2>
							<p>{preview}</p>
						</div>
						<div class="actions">
							<button disabled={previewLoading} onclick={() => restore('copy')}>Restore as copy</button>
							<button class="primary" disabled={previewLoading} onclick={() => restore('replace')}>Restore this version</button>
						</div>
					</div>
					<details open>
						<summary>Changes from current</summary>
						{#if previewLoading}
							<p class="no-diff">Loading version…</p>
						{:else if content === currentContent}
							<p class="no-diff">No textual changes from the current note.</p>
						{:else}
							<!-- The same side-by-side the review tray uses: this
							     version on the left, the note as it is now on
							     the right. -->
							<div class="diff-frame">
								<DiffView oldContent={content} newContent={currentContent} />
							</div>
						{/if}
					</details>
					<details>
						<summary>Full version</summary>
						{#if !previewLoading}
							<pre>{content}</pre>
						{/if}
					</details>
				{:else}
					<p class="state">Select a version to preview its changes.</p>
				{/if}
			</section>
		</div>
	{/if}
</div>

<style>
	.history-page {
		flex: 1;
		overflow: auto;
		padding: 24px 28px 60px;
	}
	.history-page > header {
		max-width: 1180px;
		margin: 0 auto 18px;
	}
	header a {
		color: var(--text-muted);
		font-size: 12px;
		text-decoration: none;
	}
	h1 {
		margin: 8px 0 2px;
		font-size: 21px;
	}
	header p,
	.version-head p {
		margin: 0;
		color: var(--text-muted);
		font: 12px var(--font-mono);
	}
	.history-layout {
		display: grid;
		grid-template-columns: 290px minmax(0, 1fr);
		max-width: 1180px;
		min-height: 560px;
		margin: 0 auto;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg-panel);
	}
	aside {
		overflow-y: auto;
		border-right: 1px solid var(--border);
	}
	aside button {
		width: 100%;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 3px;
		padding: 11px 13px;
		border: 0;
		border-bottom: 1px solid var(--border);
		border-radius: 0;
		text-align: left;
	}
	aside button.active {
		background: var(--bg-active);
		color: var(--accent);
	}
	aside button.older {
		align-items: center;
		color: var(--text-muted);
	}
	.day {
		position: sticky;
		top: 0;
		z-index: 1;
		margin: 0;
		padding: 12px 13px 5px;
		border-bottom: 1px solid var(--border);
		background: var(--bg-panel);
		color: var(--text-muted);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.5px;
		text-transform: uppercase;
	}
	aside span {
		color: var(--text-muted);
	}
	aside small {
		font-family: var(--font-mono);
		color: var(--text-muted);
	}
	section {
		min-width: 0;
		padding: 18px;
	}
	.version-head {
		display: flex;
		justify-content: space-between;
		gap: 16px;
		margin-bottom: 16px;
	}
	h2 {
		margin: 0 0 4px;
		font-size: 16px;
	}
	.actions {
		display: flex;
		gap: 7px;
		align-items: flex-start;
	}
	details {
		margin-top: 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	summary {
		padding: 9px 11px;
		cursor: pointer;
		font-weight: 700;
		font-size: 12px;
	}
	pre {
		max-height: 480px;
		margin: 0;
		overflow: auto;
		padding: 13px;
		border-top: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font: 12px/1.55 var(--font-mono);
		white-space: pre-wrap;
	}
	.no-diff {
		color: var(--text-muted);
		font-size: 13px;
		margin: 8px 0 0;
	}
	.diff-frame {
		margin-top: 8px;
		border: 1px solid var(--border);
		border-radius: 8px;
		overflow: hidden;
	}
	.state {
		color: var(--text-muted);
		text-align: center;
		padding: 40px;
	}
	.state.error {
		color: var(--danger);
	}
	@media (max-width: 760px) {
		.history-page {
			padding: 16px var(--m-gutter) 40px;
		}
		h1 {
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		.history-layout {
			display: block;
		}
		aside {
			max-height: 230px;
			border-right: 0;
			border-bottom: 1px solid var(--border);
		}
		.day {
			font-size: var(--m-label);
			font-weight: 600;
		}
		.version-head {
			flex-direction: column;
		}
		.actions {
			flex-wrap: wrap;
		}
	}
</style>
