<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { cubicOut } from 'svelte/easing';
	import { fly } from 'svelte/transition';
	import { api } from '$lib/api';
	import { refreshNotes, refreshPendingCount, vault } from '$lib/stores.svelte';
	import { confirmDialog } from '$lib/ui.svelte';

	let busy = $state(false);
	let error: string | null = $state(null);

	let visible = $derived(vault.pendingCount > 0 && page.url.pathname !== '/pending');

	async function act(fn: () => Promise<unknown>) {
		if (busy) return;
		busy = true;
		error = null;
		try {
			await fn();
			await refreshPendingCount();
			await refreshNotes();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	// The corner box has no room to resolve staleness; send them to the full
	// review page rather than surfacing a raw backend error here.
	const approveAll = () =>
		act(async () => {
			try {
				await api.approveAllPending();
			} catch (cause) {
				if (String((cause as Error).message).includes('stale')) {
					await goto('/pending');
					return;
				}
				throw cause;
			}
		});
	const discardAll = () =>
		act(async () => {
			if (
				await confirmDialog(`Discard all ${vault.pendingCount} staged change(s)?`, {
					title: 'Discard OMNIA changes',
					confirmLabel: 'Discard all',
					danger: true
				})
			) {
				await api.discardAllPending();
			}
		});
</script>

{#if visible}
	<div
		class="pending-box"
		role="status"
		transition:fly={{ y: 12, duration: 210, easing: cubicOut }}
	>
		<div class="head">
			<span class="pulse"></span>
			<strong>
				OMNIA staged {vault.pendingCount} change{vault.pendingCount === 1 ? '' : 's'}
			</strong>
		</div>
		<p class="hint">Nothing is applied until you approve it.</p>
		{#if error}<p class="err">{error}</p>{/if}
		<div class="actions">
			<button class="primary" onclick={() => goto('/pending')}>Review</button>
			<button onclick={approveAll} disabled={busy}>Approve all</button>
			<button onclick={discardAll} disabled={busy}>Discard all</button>
		</div>
	</div>
{/if}

<style>
	.pending-box {
		position: fixed;
		right: 18px;
		bottom: 18px;
		z-index: 50;
		width: 280px;
		background: var(--bg-panel);
		border: 1px solid var(--accent);
		border-radius: 12px;
		box-shadow: 0 8px 28px rgba(0, 0, 0, 0.18);
		padding: 12px 14px;
		font-size: 13px;
		transition:
			background-color var(--motion-slow) var(--ease-out),
			border-color var(--motion-base) var(--ease-out),
			box-shadow var(--motion-base) var(--ease-out);
	}
	.head {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.pulse {
		width: 9px;
		height: 9px;
		border-radius: 50%;
		background: var(--accent);
		flex-shrink: 0;
		animation: pulse 1.6s ease-in-out infinite;
	}
	@keyframes pulse {
		0%,
		100% {
			box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 45%, transparent);
		}
		50% {
			box-shadow: 0 0 0 6px transparent;
		}
	}
	.hint {
		color: var(--text-muted);
		margin: 4px 0 8px;
		font-size: 12px;
	}
	.err {
		color: var(--danger);
		font-size: 12px;
		margin: 0 0 6px;
	}
	.actions {
		display: flex;
		gap: 6px;
	}
	.actions button {
		font-size: 12px;
		border: 1px solid var(--border);
	}
	.actions button.primary {
		border-color: var(--accent);
	}

	@media (max-width: 760px) {
		.pending-box {
			right: 10px;
			/* Clear of the chat composer, which is bottom-anchored — the box used
			   to land on top of it exactly when OMNIA staged something. The
			   composer no longer reaches the bottom of the screen, so the same
			   74px of clearance is now measured from the top of the tab bar;
			   --tabbar-h carries the safe-area inset the old value spelled out. */
			bottom: calc(74px + var(--tabbar-h));
			left: 10px;
			width: auto;
			padding: 12px;
		}
		.actions {
			flex-wrap: wrap;
		}
		.actions button {
			flex: 1;
			min-width: 100px;
		}
		.actions button.primary {
			flex-basis: 100%;
		}
	}
</style>
