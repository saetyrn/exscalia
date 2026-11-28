<!--
	Where a cover or a playlist background comes from: this computer, or the
	Gallery (MUSIC.md section 3). Hands back a vault attachment path.
-->
<script lang="ts">
	import { api } from '$lib/api';
	import type { PinCard } from '$lib/gallery/types';
	import { modal } from '$lib/modal';
	import { musicApi } from './api';

	let {
		title = 'Choose a picture',
		onpick,
		onclose
	}: { title?: string; onpick: (path: string) => void; onclose: () => void } = $props();

	let tab = $state<'computer' | 'gallery'>('computer');
	let pins = $state<PinCard[]>([]);
	let query = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let fileInput: HTMLInputElement | undefined = $state();

	async function loadGallery() {
		try {
			const page = await api.galleryPins({ q: query.trim() || undefined, limit: 60 });
			pins = page.pins;
			error = null;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		}
	}

	$effect(() => {
		if (tab === 'gallery') void loadGallery();
	});

	async function fromComputer(event: Event) {
		const file = (event.currentTarget as HTMLInputElement).files?.[0];
		if (!file) return;
		busy = true;
		try {
			const path = await musicApi.picture(file);
			onpick(path);
			onclose();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	function fromGallery(pin: PinCard) {
		onpick(pin.image.replace(/^\/api\//, ''));
		onclose();
	}
</script>

<div class="backdrop" role="presentation" onpointerdown={(e) => e.target === e.currentTarget && onclose()}>
	<div class="sheet" role="dialog" aria-label={title} use:modal={{ onEscape: onclose }}>
		<h2>{title}</h2>
		<div class="tabs" role="tablist">
			<button type="button" role="tab" aria-selected={tab === 'computer'} class:on={tab === 'computer'} onclick={() => (tab = 'computer')}>
				This computer
			</button>
			<button type="button" role="tab" aria-selected={tab === 'gallery'} class:on={tab === 'gallery'} onclick={() => (tab = 'gallery')}>
				Gallery
			</button>
		</div>
		{#if tab === 'computer'}
			<button type="button" class="choose" disabled={busy} onclick={() => fileInput?.click()}>
				{busy ? 'Saving…' : 'Choose a file'}
			</button>
			<input bind:this={fileInput} type="file" accept="image/png,image/jpeg,image/webp" hidden onchange={fromComputer} />
		{:else}
			<input
				class="search"
				bind:value={query}
				placeholder="Search the Gallery"
				aria-label="Search the Gallery"
				onkeydown={(e) => e.key === 'Enter' && loadGallery()}
			/>
			<div class="grid">
				{#each pins as pin (pin.path)}
					<button type="button" class="pin" title={pin.title || pin.board} onclick={() => fromGallery(pin)}>
						<img src={pin.thumb} alt={pin.title} loading="lazy" style:background={pin.color ?? undefined} />
					</button>
				{/each}
			</div>
		{/if}
		{#if error}<p class="error">{error}</p>{/if}
	</div>
</div>

<style>
	.backdrop {
		position: fixed;
		inset: 0;
		z-index: var(--z-dialog);
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.3);
	}
	.sheet {
		width: min(560px, calc(100vw - 32px));
		max-height: min(640px, calc(100dvh - 64px));
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 18px;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 14px;
		box-shadow: 0 20px 48px rgba(0, 0, 0, 0.22);
	}
	h2 {
		margin: 0;
		font-size: 16px;
	}
	.tabs {
		display: flex;
		gap: 6px;
	}
	.tabs button,
	.choose {
		padding: 8px 14px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		cursor: pointer;
	}
	.tabs button.on {
		color: var(--text);
		background: var(--bg-active);
	}
	.choose {
		align-self: flex-start;
		color: var(--text);
	}
	.search {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 16px;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
		gap: 8px;
		overflow-y: auto;
	}
	.pin {
		padding: 0;
		border: 2px solid transparent;
		border-radius: 8px;
		background: none;
		cursor: pointer;
		aspect-ratio: 1;
		overflow: hidden;
	}
	.pin:hover,
	.pin:focus-visible {
		border-color: var(--text);
		outline: none;
	}
	.pin img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.error {
		margin: 0;
		color: var(--danger);
		font-size: 13px;
	}
</style>
