<script lang="ts">
	// Edit one board: rename it, or delete it with every picture on it (to
	// the Trash, as one folder). `ondone` hears what happened.
	import { api } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { modal } from '$lib/modal';
	import { confirmDialog, notify } from '$lib/ui.svelte';
	import { pictureCount } from './format';

	let {
		name,
		count,
		onclose,
		ondone
	}: {
		name: string;
		count: number;
		onclose: () => void;
		ondone: (what: { renamed?: string; deleted?: boolean }) => void;
	} = $props();

	// svelte-ignore state_referenced_locally
	let value = $state(name);
	let busy = $state(false);
	let box: HTMLInputElement | undefined = $state();

	async function save(event: SubmitEvent) {
		event.preventDefault();
		const next = value.trim();
		if (!next || next === name) return onclose();
		busy = true;
		try {
			const { name: renamed } = await api.galleryRenameBoard(name, next);
			ondone({ renamed });
			onclose();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	async function remove() {
		const ok = await confirmDialog(
			count ? `${name} and its ${pictureCount(count)} go to the Trash.` : `${name} goes to the Trash.`,
			{ title: 'Delete board', confirmLabel: 'Delete', danger: true }
		);
		if (!ok) return;
		busy = true;
		try {
			await api.galleryDeleteBoard(name);
			ondone({ deleted: true });
			onclose();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}
</script>

<div
	class="overlay"
	role="presentation"
	onclick={(e) => {
		if (e.target === e.currentTarget) onclose();
	}}
>
	<div
		class="dialog"
		role="dialog"
		aria-modal="true"
		aria-label="Edit board"
		use:modal={{ initialFocus: () => box, onEscape: onclose }}
	>
	<form class="form" onsubmit={save}>
		<header>
			<h2>Edit board</h2>
			<button type="button" class="close" title="Close" aria-label="Close" onclick={onclose}>
				<Glyph name="x" />
			</button>
		</header>
		<label class="field">
			<span>Name</span>
			<input bind:this={box} type="text" bind:value maxlength="80" />
		</label>
		<footer>
			<button type="button" class="delete" disabled={busy} onclick={() => void remove()}>Delete board</button>
			<button type="submit" class="go" disabled={busy || !value.trim()}>Save</button>
		</footer>
	</form>
	</div>
</div>

<style>
	.overlay {
		position: fixed;
		inset: 0;
		z-index: var(--z-dialog);
		display: grid;
		place-items: start center;
		padding-top: 14vh;
		background: color-mix(in srgb, #000 45%, transparent);
	}
	.dialog {
		width: min(400px, calc(100vw - 32px));
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 16px;
		border: 1px solid var(--border);
		border-radius: 16px;
		background: var(--bg);
		color: var(--text);
		box-shadow: var(--lift-shadow);
	}
	.form {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	header,
	footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	h2 {
		margin: 0;
		font-size: 18px;
		font-weight: 700;
	}
	.close {
		width: 36px;
		height: 36px;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 50%;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.close:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.close :global(svg) {
		width: 18px;
		height: 18px;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 5px;
		font-size: 12px;
		color: var(--text-muted);
	}
	input {
		height: 40px;
		padding: 0 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 14px;
	}
	input:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: var(--focus-ring);
	}
	.delete,
	.go {
		height: 40px;
		padding: 0 16px;
		border-radius: 10px;
		font: inherit;
		font-size: 14px;
		font-weight: 600;
		cursor: pointer;
	}
	.delete {
		border: 1px solid var(--border);
		background: transparent;
		color: var(--danger);
	}
	.delete:hover {
		background: var(--bg-hover);
	}
	.go {
		border: none;
		background: var(--text);
		color: var(--bg);
	}
	button:disabled {
		opacity: 0.4;
		cursor: default;
	}
	@media (max-width: 760px) {
		input,
		.delete,
		.go {
			height: 44px;
			font-size: var(--m-row-text);
		}
		.close {
			width: 44px;
			height: 44px;
		}
	}
</style>
