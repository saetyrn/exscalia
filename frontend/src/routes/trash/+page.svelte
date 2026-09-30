<script lang="ts">
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { ApiError, api, encodePath, type TrashItem } from '$lib/api';
	import { refreshNotes } from '$lib/stores.svelte';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';

	let { data } = $props();

	// Initial-only capture is deliberate: this route remounts per navigation.
	const initialItems = untrack(() => data.items);
	let items: TrashItem[] = $state(initialItems ?? []);
	let selected: (TrashItem & { content: string | null; paths: string[] }) | null = $state(null);
	let loading = $state(initialItems === null);
	// The route load() covers the initial fetch; recover here only if it failed.
	if (initialItems === null) void load();

	async function load() {
		// Only an empty list may blank to "Loading Trash…" — a refresh after an
		// undo or purge must keep the rows (and the scroll position) on screen.
		if (items.length === 0) loading = true;
		try {
			items = (await api.listTrash()).items;
			if (selected && !items.some((item) => item.id === selected?.id)) selected = null;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			loading = false;
		}
	}

	async function inspect(item: TrashItem) {
		try {
			selected = await api.previewTrash(item.id);
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	const isPaper = (item: TrashItem) => item.kind === 'notebook' || item.kind === 'page';

	async function restore(item: TrashItem, target?: string) {
		try {
			const result = await api.restoreTrash(item.id, target);
			await refreshNotes();
			await load();
			notify('Restored from Trash.', { tone: 'success' });
			if (result.kind === 'note') await goto(`/note/${encodePath(result.path)}`);
		} catch (cause) {
			// Paper notebooks and pages only go back where they were.
			if (cause instanceof ApiError && cause.status === 409 && !target && !isPaper(item)) {
				const next = await promptDialog('That location is occupied. Choose a new restore path:', {
					title: 'Restore with a new name',
					value:
						item.kind === 'note'
							? item.original_path.replace(/\.md$/, ' (restored).md')
							: `${item.original_path} (restored)`,
					confirmLabel: 'Restore'
				});
				if (next) await restore(item, next);
				return;
			}
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function purge(item: TrashItem) {
		const confirmed = await confirmDialog(
			`Permanently remove “${item.original_path}” from Trash? Existing Git history may still retain it.`,
			{ title: 'Remove from Trash', confirmLabel: 'Remove', danger: true }
		);
		if (!confirmed) return;
		try {
			await api.purgeTrash(item.id);
			await load();
			notify('Removed from Trash.');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function empty() {
		const confirmed = await confirmDialog(
			`Remove all ${items.length} Trash item${items.length === 1 ? '' : 's'}? Git history may still retain them.`,
			{ title: 'Empty Trash', confirmLabel: 'Empty Trash', danger: true }
		);
		if (!confirmed) return;
		try {
			await api.emptyTrash();
			await load();
			notify('Trash emptied.');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}
</script>

<div class="trash-page">
	<header>
		<div>
			<h1>Trash</h1>
			<p>Items are removed automatically after 30 days. This is not secure erasure from Git history.</p>
		</div>
		{#if items.length}<button class="danger" onclick={empty}>Empty Trash</button>{/if}
	</header>

	{#if loading}
		<p class="state">Loading Trash…</p>
	{:else if items.length === 0}
		<div class="empty">
			<h2>Trash is empty</h2>
			<p>Deleted notes and folders will remain recoverable here for 30 days.</p>
		</div>
	{:else}
		<div class="trash-layout">
			<div class="items">
				{#each items as item (item.id)}
					<button class:active={selected?.id === item.id} onclick={() => inspect(item)}>
						<strong>{item.original_path}</strong>
						{#if isPaper(item)}
							<span>{item.kind}</span>
						{:else}
							<span>{item.kind} · {item.note_count} note{item.note_count === 1 ? '' : 's'}</span>
						{/if}
						<small>Deletes {new Date(item.expires_at).toLocaleDateString()}</small>
					</button>
				{/each}
			</div>
			<section>
				{#if selected}
					<div class="preview-head">
						<div>
							<h2>{selected.original_path}</h2>
							<p>Deleted {new Date(selected.deleted_at).toLocaleString()}</p>
						</div>
						<div class="actions">
							<button class="danger" onclick={() => purge(selected!)}>Remove</button>
							<button class="primary" onclick={() => restore(selected!)}>Restore</button>
						</div>
					</div>
					{#if selected.content !== null}
						<pre>{selected.content}</pre>
					{:else}
						<ul>
							{#each selected.paths as path (path)}<li>{path}</li>{/each}
						</ul>
					{/if}
				{:else}
					<p class="state">Select an item to preview or restore it.</p>
				{/if}
			</section>
		</div>
	{/if}
</div>

<style>
	.trash-page {
		flex: 1;
		overflow: auto;
		padding: 24px 28px 60px;
	}
	.trash-page > header {
		display: flex;
		justify-content: space-between;
		gap: 16px;
		max-width: 1080px;
		margin: 0 auto 18px;
	}
	h1 {
		margin: 0 0 4px;
		font-size: 21px;
	}
	header p,
	.preview-head p,
	.empty p {
		margin: 0;
		color: var(--text-muted);
		font-size: 13px;
	}
	.trash-layout {
		display: grid;
		grid-template-columns: 320px minmax(0, 1fr);
		max-width: 1080px;
		min-height: 500px;
		margin: 0 auto;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg-panel);
	}
	.items {
		border-right: 1px solid var(--border);
	}
	.items button {
		width: 100%;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 3px;
		padding: 12px;
		border: 0;
		border-bottom: 1px solid var(--border);
		border-radius: 0;
		text-align: left;
	}
	.items button.active {
		background: var(--bg-active);
		color: var(--accent);
	}
	.items span,
	.items small {
		color: var(--text-muted);
	}
	section {
		min-width: 0;
		padding: 18px;
	}
	.preview-head {
		display: flex;
		justify-content: space-between;
		gap: 14px;
		margin-bottom: 14px;
	}
	h2 {
		margin: 0 0 4px;
		font-size: 16px;
	}
	.actions {
		display: flex;
		gap: 7px;
	}
	pre {
		max-height: 520px;
		overflow: auto;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
		color: var(--text);
		font: 12px/1.55 var(--font-mono);
		white-space: pre-wrap;
	}
	.empty,
	.state {
		max-width: 560px;
		margin: 80px auto;
		text-align: center;
	}
	.empty h2 {
		font-size: 18px;
	}
	@media (max-width: 760px) {
		.trash-page {
			padding: 16px var(--m-gutter) 40px;
		}
		h1 {
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		.trash-page > header {
			align-items: flex-start;
		}
		.trash-layout {
			display: block;
		}
		.items {
			max-height: 240px;
			overflow-y: auto;
			border-right: 0;
			border-bottom: 1px solid var(--border);
		}
		.preview-head {
			flex-direction: column;
		}
	}
</style>
