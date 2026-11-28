<script lang="ts">
	// Add pictures: pick or drop files, or paste a link to an image or to a
	// page whose main picture is taken, onto a board chosen here (a new one is
	// made on the spot). Files go up through uploads.svelte.ts, so the page's
	// progress line follows them after the dialog closes.
	import { api } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { modal } from '$lib/modal';
	import { notify } from '$lib/ui.svelte';
	import { imageFiles } from './format';
	import { sendPictures } from './uploads.svelte';
	import type { Pin } from './types';

	const NEW = '\u0000new';

	let {
		boards,
		board = '',
		onclose,
		onsaved
	}: {
		boards: string[];
		/** The board to preselect: the one on screen. */
		board?: string;
		onclose: () => void;
		onsaved: (pin: Pin, created: boolean) => void;
	} = $props();

	// The dialog is created open, so its first board is the prop's value then.
	// svelte-ignore state_referenced_locally
	let choice = $state(board);
	let newName = $state('');
	let link = $state('');
	let busy = $state(false);
	let over = $state(false);
	let picker: HTMLInputElement | undefined = $state();
	let linkBox: HTMLInputElement | undefined = $state();

	async function target(): Promise<string | null> {
		if (choice !== NEW) return choice;
		const name = newName.trim();
		if (!name) {
			notify('Name the new board', { tone: 'error' });
			return null;
		}
		try {
			return (await api.galleryCreateBoard(name)).name;
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
			return null;
		}
	}

	async function send(files: File[]) {
		const pictures = imageFiles(files);
		if (!pictures.length) {
			notify('PNG, JPEG, GIF or WebP only', { tone: 'error' });
			return;
		}
		const where = await target();
		if (where === null) return;
		onclose();
		const errors = await sendPictures(pictures, where, onsaved);
		if (errors.length) notify(errors.slice(0, 3).join('\n'), { tone: 'error' });
	}

	async function saveLink(event: SubmitEvent) {
		event.preventDefault();
		if (!link.trim() || busy) return;
		const where = await target();
		if (where === null) return;
		busy = true;
		try {
			const { pin, created } = await api.galleryLink(link.trim(), where);
			onsaved(pin, created);
			onclose();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	function drop(event: DragEvent) {
		event.preventDefault();
		over = false;
		void send(Array.from(event.dataTransfer?.files ?? []));
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
		aria-label="Add pictures"
		use:modal={{ initialFocus: () => linkBox, onEscape: onclose }}
	>
		<header>
			<h2>Add pictures</h2>
			<button type="button" class="close" title="Close" aria-label="Close" onclick={onclose}>
				<Glyph name="x" />
			</button>
		</header>

		<label class="field">
			<span>Board</span>
			<select bind:value={choice}>
				<option value="">No board</option>
				{#each boards as name (name)}
					<option value={name}>{name}</option>
				{/each}
				<option value={NEW}>New board</option>
			</select>
		</label>
		{#if choice === NEW}
			<input class="text" type="text" placeholder="Board name" bind:value={newName} maxlength="80" />
		{/if}

		<button
			type="button"
			class="drop"
			class:over
			onclick={() => picker?.click()}
			ondragover={(e) => {
				e.preventDefault();
				over = true;
			}}
			ondragleave={() => (over = false)}
			ondrop={drop}
		>
			<Glyph name="image" />
			<span>Choose or drop pictures</span>
		</button>
		<input
			bind:this={picker}
			class="hidden"
			type="file"
			accept="image/png,image/jpeg,image/gif,image/webp"
			multiple
			onchange={(e) => void send(Array.from((e.currentTarget as HTMLInputElement).files ?? []))}
		/>

		<form class="link" onsubmit={saveLink}>
			<input
				bind:this={linkBox}
				class="text"
				type="url"
				inputmode="url"
				placeholder="Image or page link"
				bind:value={link}
			/>
			<button type="submit" class="go" disabled={busy || !link.trim()}>{busy ? '…' : 'Save'}</button>
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
		padding-top: 12vh;
		background: color-mix(in srgb, #000 45%, transparent);
	}
	.dialog {
		width: min(440px, calc(100vw - 32px));
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 16px;
		border: 1px solid var(--border);
		border-radius: 16px;
		background: var(--bg);
		color: var(--text);
		box-shadow: var(--lift-shadow);
	}
	header {
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
	select,
	.text {
		height: 40px;
		padding: 0 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 14px;
	}
	select:focus-visible,
	.text:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: var(--focus-ring);
	}
	.drop {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		min-height: 140px;
		border: 1.5px dashed var(--border);
		border-radius: 14px;
		background: var(--bg-panel);
		color: var(--text-muted);
		font: inherit;
		font-size: 14px;
		cursor: pointer;
		transition: background var(--motion-fast) var(--ease-out), border-color var(--motion-fast) var(--ease-out);
	}
	.drop :global(svg) {
		width: 26px;
		height: 26px;
	}
	.drop:hover,
	.drop.over {
		background: var(--bg-hover);
		border-color: var(--text-muted);
		color: var(--text);
	}
	.drop:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
	}
	.hidden {
		display: none;
	}
	.link {
		display: flex;
		gap: 8px;
	}
	.link .text {
		flex: 1;
		min-width: 0;
	}
	.go {
		height: 40px;
		padding: 0 16px;
		border: none;
		border-radius: 10px;
		background: var(--text);
		color: var(--bg);
		font: inherit;
		font-size: 14px;
		font-weight: 600;
		cursor: pointer;
	}
	.go:disabled {
		opacity: 0.4;
		cursor: default;
	}
	@media (max-width: 760px) {
		.overlay {
			padding-top: 6vh;
		}
		select,
		.text,
		.go {
			height: 44px;
			font-size: var(--m-row-text);
		}
		.close {
			width: 44px;
			height: 44px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.drop {
			transition: none;
		}
	}
</style>
