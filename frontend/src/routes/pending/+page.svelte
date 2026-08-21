<script lang="ts">
	import { cubicOut } from 'svelte/easing';
	import { fade, fly, slide } from 'svelte/transition';
	import DiffView from '$lib/DiffView.svelte';
	import { api, encodePath, type PendingChange } from '$lib/api';
	import { refreshNotes, refreshPendingCount, vault } from '$lib/stores.svelte';
	import { confirmDialog } from '$lib/ui.svelte';

	let changes: PendingChange[] = $state([]);
	let loaded = $state(false);
	let expanded: Record<number, boolean> = $state({});
	let error: string | null = $state(null);
	let busy = $state(false);

	$effect(() => {
		void load();
	});

	async function load() {
		changes = await api.listPending();
		// Expand single changes by default; collapse long lists.
		if (changes.length === 1) expanded[changes[0].id] = true;
		loaded = true;
		await refreshPendingCount();
	}

	const opLabel: Record<PendingChange['op'], string> = {
		create: 'added',
		edit: 'modified',
		delete: 'deleted',
		move: 'moved'
	};

	let sessions = $derived(new Set(changes.map((c) => c.session_id)).size);

	async function act(fn: () => Promise<unknown>) {
		if (busy) return;
		busy = true;
		error = null;
		try {
			await fn();
			await load();
			await refreshNotes();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	const approve = (c: PendingChange, force = false) => act(() => api.approvePending(c.id, force));
	const discard = (c: PendingChange) => act(() => api.discardPending(c.id));
	/** Approving all used to dead-end on a single stale change: the backend
	 * requires `force`, and no caller ever passed it, so the user saw a raw
	 * error naming internal database ids and a flag with no button. */
	const approveAll = () =>
		act(async () => {
			const stale = changes.filter((c) => c.stale);
			if (stale.length) {
				const proceed = await confirmDialog(
					`${stale.length} of ${changes.length} change(s) were proposed before you edited those notes. Approving will overwrite your edits.`,
					{
						title: 'Some changes are stale',
						confirmLabel: `Approve all ${changes.length}`,
						cancelLabel: 'Review individually',
						danger: true
					}
				);
				if (!proceed) return;
				await api.approveAllPending(true);
				return;
			}
			await api.approveAllPending();
		});
	const discardAll = () =>
		act(async () => {
			if (
				await confirmDialog(`Discard all ${changes.length} pending change(s)?`, {
					title: 'Discard OMNIA changes',
					confirmLabel: 'Discard all',
					danger: true
				})
			) {
				await api.discardAllPending();
			}
		});
</script>

<div class="tray">
	<header>
		<div>
			<h1>Pending changes</h1>
			<p class="sub">
				{#if changes.length === 0}
					Everything OMNIA proposes lands here for your review before it touches the vault.
				{:else}
					{changes.length} staged change{changes.length === 1 ? '' : 's'}
					{sessions > 1 ? `across ${sessions} sessions` : ''} — approve to commit, discard to
					drop. Approved deletes stay recoverable from git history.
				{/if}
			</p>
		</div>
		{#if changes.length > 0}
			<div class="bulk">
				<button class="primary" onclick={approveAll} disabled={busy}>Approve all</button>
				<button onclick={discardAll} disabled={busy}>Discard all</button>
			</div>
		{/if}
	</header>

	{#if error}
		<div class="error" transition:fly={{ y: -5, duration: 170, easing: cubicOut }}>{error}</div>
	{/if}

	{#if loaded && changes.length === 0}
		<div class="empty" in:fade={{ duration: 180 }}>
			<p>No pending changes. The vault is exactly as you left it.</p>
			<p><a href="/omnia">Ask OMNIA to do something →</a></p>
		</div>
	{/if}

	{#each changes as c (c.id)}
		<section
			class="change"
			class:stale={c.stale}
			in:fly={{ y: 6, duration: 180, easing: cubicOut }}
			out:fade={{ duration: 100 }}
		>
			<button
				type="button"
				class="row"
				onclick={() => (expanded[c.id] = !expanded[c.id])}
				aria-expanded={expanded[c.id] ?? false}
			>
				<span class="badge {c.op}">{opLabel[c.op]}</span>
				<span class="path">
					{c.path}{#if c.new_path}&nbsp;→ {c.new_path}{/if}
				</span>
				{#if c.stale}
					<span class="stale-flag" title="This file was edited after the change was proposed">
						stale
					</span>
				{/if}
				<span class="chev" class:open={expanded[c.id] ?? false}>›</span>
			</button>
			{#if c.rationale}
				<p class="rationale">{c.rationale}</p>
			{/if}
			{#if expanded[c.id]}
				<div class="detail" transition:slide={{ duration: 180, easing: cubicOut }}>
					{#if c.op === 'move'}
						<p class="movenote">
							Rename <code>{c.path}</code> to <code>{c.new_path}</code>. Content unchanged.
						</p>
					{:else}
						<DiffView oldContent={c.old_content ?? ''} newContent={c.new_content ?? ''} />
					{/if}
					<div class="actions">
						{#if c.stale}
							<span class="stale-note">
								The note changed since this was proposed — recheck the diff.
							</span>
							<button class="primary" onclick={() => approve(c, true)} disabled={busy}>
								Approve anyway
							</button>
						{:else}
							<button class="primary" onclick={() => approve(c)} disabled={busy}>Approve</button>
						{/if}
						<button onclick={() => discard(c)} disabled={busy}>Discard</button>
						{#if c.op !== 'create'}
							<a class="open" href="/note/{encodePath(c.path)}">open current note</a>
						{/if}
					</div>
				</div>
			{/if}
		</section>
	{/each}
</div>

<style>
	.tray {
		flex: 1;
		overflow-y: auto;
		max-width: 980px;
		width: 100%;
		margin: 0 auto;
		padding: 24px 28px 60px;
	}
	header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		margin-bottom: 18px;
	}
	h1 {
		font-size: 19px;
		margin: 0 0 4px;
	}
	.sub {
		color: var(--text-muted);
		font-size: 13px;
		margin: 0;
		max-width: 560px;
	}
	.bulk {
		display: flex;
		gap: 8px;
		flex-shrink: 0;
	}
	.empty {
		text-align: center;
		color: var(--text-muted);
		margin-top: 60px;
	}
	.empty a {
		color: var(--accent);
	}
	.error {
		background: color-mix(in srgb, var(--danger) 12%, transparent);
		color: var(--danger);
		border-radius: var(--radius);
		padding: 8px 12px;
		font-size: 13px;
		margin-bottom: 12px;
	}
	.change {
		border: 1px solid var(--border);
		border-radius: var(--radius);
		margin-bottom: 10px;
		background: var(--bg-panel);
		overflow: hidden;
		transition:
			border-color var(--motion-base) var(--ease-out),
			box-shadow var(--motion-base) var(--ease-out),
			background-color var(--motion-base) var(--ease-out);
	}
	.change:hover {
		border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
		box-shadow: 0 5px 18px color-mix(in srgb, var(--text) 6%, transparent);
	}
	.change.stale {
		border-color: color-mix(in srgb, var(--danger) 50%, var(--border));
	}
	.row {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		text-align: left;
		padding: 10px 14px;
		font-size: 13.5px;
		border: none;
		background: none;
	}
	.row:not(:disabled):hover {
		transform: none;
		background: color-mix(in srgb, var(--bg-hover) 68%, transparent);
	}
	.row:not(:disabled):active {
		transform: none;
		background: var(--bg-hover);
	}
	.badge {
		font-size: 11px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		border-radius: 4px;
		padding: 2px 7px;
		flex-shrink: 0;
	}
	.badge.create {
		background: color-mix(in srgb, var(--added) 18%, transparent);
		color: var(--added);
	}
	.badge.edit {
		background: var(--accent-soft);
		color: var(--accent);
	}
	.badge.delete {
		background: color-mix(in srgb, var(--removed) 15%, transparent);
		color: var(--removed);
	}
	.badge.move {
		background: var(--bg-hover);
		color: var(--text-muted);
	}
	.path {
		font-family: var(--font-mono);
		font-size: 12.5px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.stale-flag {
		color: var(--danger);
		font-size: 12px;
		flex-shrink: 0;
	}
	.chev {
		margin-left: auto;
		color: var(--text-muted);
		font-size: 17px;
		line-height: 1;
		transform: rotate(0deg);
		transition: transform var(--motion-base) var(--ease-snap);
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.rationale {
		color: var(--text-muted);
		font-size: 12.5px;
		font-style: italic;
		margin: -4px 0 8px;
		padding: 0 14px 0 14px;
	}
	.detail {
		padding: 10px 14px 12px;
		border-top: 1px solid color-mix(in srgb, var(--border) 75%, transparent);
		transform-origin: top;
	}
	.movenote {
		font-size: 13px;
		color: var(--text-muted);
	}
	.movenote code {
		font-family: var(--font-mono);
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 10px;
	}
	.stale-note {
		color: var(--danger);
		font-size: 12.5px;
	}
	.open {
		margin-left: auto;
		font-size: 12.5px;
		color: var(--text-muted);
	}
	.open:hover {
		color: var(--accent);
	}

	@media (max-width: 760px) {
		.tray {
			padding: 18px 12px 24px;
		}
		header {
			flex-direction: column;
			gap: 12px;
			margin-bottom: 14px;
		}
		.bulk {
			width: 100%;
		}
		.bulk button {
			flex: 1;
		}
		.empty {
			margin: 36px 16px 0;
		}
		.row {
			min-height: 52px;
			gap: 8px;
			padding: 9px 10px;
		}
		.path {
			min-width: 0;
			font-size: 12px;
		}
		.rationale {
			padding-inline: 10px;
		}
		.detail {
			padding: 10px;
		}
		.actions {
			flex-wrap: wrap;
		}
		.actions button {
			flex: 1;
		}
		.stale-note {
			width: 100%;
		}
		.open {
			width: 100%;
			margin: 2px 0 0;
			padding: 8px 2px;
		}
	}
</style>
