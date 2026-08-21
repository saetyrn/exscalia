<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, encodePath, type HistoryVersion } from '$lib/api';
	import DiffView from '$lib/DiffView.svelte';
	import { refreshNotes } from '$lib/stores.svelte';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';

	let { data } = $props();

	let path = $derived(page.url.searchParams.get('path') ?? '');
	let versions: HistoryVersion[] = $state([]);
	let selected: HistoryVersion | null = $state(null);
	let diff = $state('');
	let content = $state('');
	let loading = $state(false);
	let error: string | null = $state(null);

	// The route load() fetches during navigation (including ?path= changes,
	// which don't remount this component) — sync local state from its result.
	$effect(() => {
		versions = data.versions ?? [];
		error = data.error;
		selected = null;
		diff = '';
		content = '';
	});

	let currentContent = $state('');

	async function inspect(version: HistoryVersion) {
		selected = version;
		try {
			const [diffResult, contentResult, note] = await Promise.all([
				api.historyDiff(path, version.sha, version.version_path),
				api.historyContent(path, version.sha, version.version_path),
				api.readNote(path).catch(() => null) // the note may be deleted
			]);
			diff = diffResult.diff || 'No textual changes from the current note.';
			content = contentResult.content;
			currentContent = note?.content ?? '';
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
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

	{#if loading}
		<p class="state">Loading versions…</p>
	{:else if error}
		<p class="state error">{error}</p>
	{:else if versions.length === 0}
		<p class="state">No committed versions are available yet.</p>
	{:else}
		<div class="history-layout">
			<aside aria-label="Versions">
				{#each versions as version (version.sha)}
					<button
						class:active={selected?.sha === version.sha}
						disabled={version.deleted}
						onclick={() => inspect(version)}
					>
						<strong>{new Date(version.committed_at).toLocaleString()}</strong>
						<span>{version.message}{version.deleted ? ' · note absent' : ''}</span>
						<small>{version.short_sha} · {version.author}</small>
					</button>
				{/each}
			</aside>
			<section>
				{#if selected}
					<div class="version-head">
						<div>
							<h2>{selected.message}</h2>
							<p>{selected.preview || 'Empty note'}</p>
						</div>
						<div class="actions">
							<button onclick={() => restore('copy')}>Restore as copy</button>
							<button class="primary" onclick={() => restore('replace')}>Restore this version</button>
						</div>
					</div>
					<details open>
						<summary>Changes from current</summary>
						{#if diff.startsWith('No textual changes')}
							<p class="no-diff">{diff}</p>
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
						<pre>{content}</pre>
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
			padding: 14px 10px 40px;
		}
		.history-layout {
			display: block;
		}
		aside {
			max-height: 230px;
			border-right: 0;
			border-bottom: 1px solid var(--border);
		}
		.version-head {
			flex-direction: column;
		}
		.actions {
			flex-wrap: wrap;
		}
	}
</style>
