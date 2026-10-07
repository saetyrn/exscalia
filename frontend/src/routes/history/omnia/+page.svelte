<script lang="ts">
	import { untrack } from 'svelte';
	import { api, type OmniaActionRecord } from '$lib/api';
	import { noteHref } from '$lib/recipes/links';
	import { refreshNotes } from '$lib/stores.svelte';
	import { confirmDialog, notify } from '$lib/ui.svelte';

	let { data } = $props();

	// Initial-only capture is deliberate: this route remounts per navigation.
	const initial = untrack(() => data.actions);
	let actions = $state<OmniaActionRecord[]>(initial ?? []);
	let loading = $state(initial === null);
	let undoing = $state<string | null>(null);

	// The route load() covers the first fetch; recover only if it failed.
	if (initial === null) void load();

	async function load() {
		// Only an empty list may blank to "Loading…" — a refresh after an undo
		// must keep the entries (and the scroll position) on screen.
		if (actions.length === 0) loading = true;
		try {
			actions = (await api.listOmniaActions()).actions;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			loading = false;
		}
	}

	async function undo(action: OmniaActionRecord) {
		const confirmed = await confirmDialog(`Undo “${action.summary}”?`, {
			title: 'Undo OMNIA action',
			confirmLabel: 'Undo'
		});
		if (!confirmed) return;
		undoing = action.id;
		try {
			// Undo is bound to the originating thread, which is why the record
			// carries thread_id.
			await api.undoOmniaAction(action.id, action.thread_id);
			await refreshNotes();
			await load();
			notify('Reverted.', { tone: 'success' });
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			undoing = null;
		}
	}

	function when(value: string) {
		const at = new Date(value);
		return at.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit'
		});
	}
</script>

<div class="log-page">
	<header>
		<div>
			<h1>OMNIA history</h1>
			<p>Everything OMNIA changed, newest first. Each entry can be reverted.</p>
		</div>
		<button onclick={load} disabled={loading}>Refresh</button>
	</header>

	{#if loading}
		<p class="state">Loading…</p>
	{:else if actions.length === 0}
		<div class="empty">
			<h2>Nothing yet</h2>
			<p>Changes OMNIA makes to your vault will be listed here.</p>
		</div>
	{:else}
		<ul class="entries">
			{#each actions as action (action.id)}
				<li class:undone={!!action.undone_at}>
					<div class="line">
						<span class="summary">{action.summary}</span>
						<span class="when">{when(action.created_at)}</span>
					</div>
					<div class="meta">
						<code>{action.tool}</code>
						{#each action.paths.slice(0, 3) as path (path)}
							<a href={noteHref(path)}>{path}</a>
						{/each}
						{#if action.paths.length > 3}
							<span class="more">+{action.paths.length - 3} more</span>
						{/if}
					</div>
					<div class="row-actions">
						{#if action.undone_at}
							<span class="tag">Undone</span>
						{:else if action.undoable}
							<button disabled={undoing === action.id} onclick={() => undo(action)}>
								{undoing === action.id ? 'Undoing…' : 'Undo'}
							</button>
						{:else}
							<span class="tag muted">Not undoable</span>
						{/if}
					</div>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.log-page {
		flex: 1;
		overflow: auto;
		padding: 24px 28px 60px;
	}
	header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		max-width: 860px;
		margin: 0 auto 18px;
	}
	h1 {
		margin: 0 0 4px;
		font-size: 21px;
	}
	header p,
	.empty p {
		margin: 0;
		color: var(--text-muted);
		font-size: 13px;
	}
	.entries {
		max-width: 860px;
		margin: 0 auto;
		padding: 0;
		list-style: none;
	}
	.entries li {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 2px 12px;
		padding: 11px 14px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg-panel);
		margin-bottom: 8px;
	}
	.entries li.undone {
		opacity: 0.55;
	}
	.line {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 10px;
		min-width: 0;
	}
	.summary {
		overflow: hidden;
		font-size: 13.5px;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.when {
		flex-shrink: 0;
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		min-width: 0;
	}
	.meta code {
		color: var(--text-muted);
		font-family: var(--font-mono);
		font-size: 11px;
	}
	.meta a {
		overflow: hidden;
		color: var(--accent);
		font-size: 11.5px;
		text-decoration: none;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.meta a:hover {
		text-decoration: underline;
	}
	.more,
	.tag {
		color: var(--text-muted);
		font-size: 11px;
	}
	.row-actions {
		grid-row: 1 / span 2;
		grid-column: 2;
		align-self: center;
	}
	.row-actions button {
		font-size: 12px;
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
		.log-page {
			padding: 16px var(--m-gutter) 40px;
		}
		h1 {
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		.entries li {
			grid-template-columns: minmax(0, 1fr);
		}
		.row-actions {
			grid-row: auto;
			grid-column: 1;
			margin-top: 6px;
		}
	}
</style>
