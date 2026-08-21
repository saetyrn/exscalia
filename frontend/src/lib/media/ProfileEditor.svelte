<script lang="ts">
	import { untrack } from 'svelte';
	import { api, uploadAttachment } from '$lib/api';
	import type { MediaProfile } from '$lib/api';
	import FramedImage from '$lib/media/FramedImage.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import { notify } from '$lib/ui.svelte';

	// Steam-style customizer: pick/upload an avatar and banner, reframe each
	// (drag to reposition, slider to zoom), and reuse ones you've had before.
	let {
		profile,
		initialTab = 'avatar',
		onclose,
		onsaved
	}: {
		profile: MediaProfile;
		initialTab?: 'avatar' | 'banner';
		onclose: () => void;
		onsaved: (profile: MediaProfile) => void;
	} = $props();

	type Frame = { url: string; zoom: number; x: number; y: number };

	let tab: 'avatar' | 'banner' = $state(untrack(() => initialTab));
	let busy = $state(false);

	// Seed the drafts once — the modal is remounted each time it opens.
	let av: Frame = $state(
		untrack(() => ({
			url: profile.avatar,
			zoom: profile.avatar_zoom,
			x: profile.avatar_x,
			y: profile.avatar_y
		}))
	);
	let bn: Frame = $state(
		untrack(() => ({
			url: profile.banner,
			zoom: profile.banner_zoom,
			x: profile.banner_x,
			y: profile.banner_y
		}))
	);
	let avatars: string[] = $state(untrack(() => [...profile.avatars]));
	let banners: string[] = $state(untrack(() => [...profile.banners]));

	let current = $derived(tab === 'avatar' ? av : bn);
	let gallery = $derived(tab === 'avatar' ? avatars : banners);

	let fileInput: HTMLInputElement | undefined = $state();

	function reposition(nx: number, ny: number) {
		if (tab === 'avatar') av = { ...av, x: nx, y: ny };
		else bn = { ...bn, x: nx, y: ny };
	}

	function select(url: string) {
		if (tab === 'avatar') {
			av = url === av.url ? av : { url, zoom: 1, x: 50, y: 50 };
		} else {
			bn = url === bn.url ? bn : { url, zoom: 1, x: 50, y: 50 };
		}
	}

	function setZoom(value: number) {
		if (tab === 'avatar') av = { ...av, zoom: value };
		else bn = { ...bn, zoom: value };
	}

	async function upload(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file || busy) return;
		busy = true;
		try {
			const uploaded = await uploadAttachment(file);
			if (tab === 'avatar') {
				av = { url: uploaded.url, zoom: 1, x: 50, y: 50 };
				if (!avatars.includes(uploaded.url)) avatars = [uploaded.url, ...avatars];
			} else {
				bn = { url: uploaded.url, zoom: 1, x: 50, y: 50 };
				if (!banners.includes(uploaded.url)) banners = [uploaded.url, ...banners];
			}
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	async function save() {
		if (busy) return;
		busy = true;
		try {
			const result = await api.mediaSaveProfile({
				avatar: av.url,
				banner: bn.url,
				avatar_zoom: av.zoom,
				avatar_x: av.x,
				avatar_y: av.y,
				banner_zoom: bn.zoom,
				banner_x: bn.x,
				banner_y: bn.y
			});
			onsaved(result.profile);
			onclose();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}
</script>

<svelte:window
	onkeydown={(e) => {
		if (e.key === 'Escape') onclose();
	}}
/>

<div
	class="scrim"
	role="button"
	tabindex="-1"
	aria-label="Close"
	onclick={(e) => {
		if (e.target === e.currentTarget) onclose();
	}}
	onkeydown={() => {}}
>
	<div class="panel" role="dialog" aria-modal="true" aria-label="Customize profile">
		<header>
			<h2>Customize profile</h2>
			<button type="button" class="icon" title="Close" aria-label="Close" onclick={onclose}>
				<Glyph name="x" />
			</button>
		</header>

		<div class="tabs" role="tablist">
			<button
				type="button"
				role="tab"
				aria-selected={tab === 'avatar'}
				class:active={tab === 'avatar'}
				onclick={() => (tab = 'avatar')}>Avatar</button
			>
			<button
				type="button"
				role="tab"
				aria-selected={tab === 'banner'}
				class:active={tab === 'banner'}
				onclick={() => (tab = 'banner')}>Banner</button
			>
		</div>

		<div class="stage">
			<div class="preview {tab}" class:empty={!current.url}>
				{#if current.url}
					<FramedImage
						src={current.url}
						zoom={current.zoom}
						x={current.x}
						y={current.y}
						editable
						onreposition={reposition}
					/>
				{:else}
					<span class="ph"><Glyph name={tab === 'avatar' ? 'user' : 'image'} /></span>
				{/if}
			</div>
			{#if current.url}
				<label class="zoom">
					<span>Zoom</span>
					<input
						type="range"
						min="1"
						max="3"
						step="0.02"
						value={current.zoom}
						oninput={(e) => setZoom(Number((e.currentTarget as HTMLInputElement).value))}
					/>
				</label>
				<p class="hint">Drag the image to reposition it.</p>
			{/if}
		</div>

		<div class="picker">
			<div class="picker-head">
				<span>{tab === 'avatar' ? 'Your avatars' : 'Your banners'}</span>
				<button type="button" class="upload" disabled={busy} onclick={() => fileInput?.click()}>
					Upload {tab}
				</button>
			</div>
			<input
				bind:this={fileInput}
				type="file"
				accept="image/png,image/jpeg,image/webp,image/gif"
				class="sr-only"
				onchange={upload}
			/>
			{#if gallery.length === 0}
				<p class="none">Nothing yet — upload one above.</p>
			{:else}
				<div class="grid {tab}">
					{#each gallery as url (url)}
						<button
							type="button"
							class="thumb {tab}"
							class:selected={current.url === url}
							aria-label="Use this {tab}"
							onclick={() => select(url)}
						>
							<img src={url} alt="" loading="lazy" />
						</button>
					{/each}
				</div>
			{/if}
		</div>

		<footer>
			<button type="button" class="ghost" onclick={onclose}>Cancel</button>
			<button type="button" class="primary" disabled={busy} onclick={save}>Save</button>
		</footer>
	</div>
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		z-index: 50;
		display: grid;
		place-items: center;
		padding: 20px;
		background: color-mix(in srgb, #000 62%, transparent);
	}
	.panel {
		width: min(560px, 100%);
		max-height: 90vh;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 14px;
		color: var(--text);
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 16px 18px;
		border-bottom: 1px solid var(--border);
	}
	header h2 {
		margin: 0;
		font-size: 15px;
		font-weight: 600;
	}
	.icon {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		padding: 0;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.icon:hover {
		color: var(--text);
	}
	.icon :global(svg) {
		width: 18px;
		height: 18px;
	}
	.tabs {
		display: flex;
		gap: 4px;
		padding: 12px 18px 0;
	}
	.tabs button {
		padding: 7px 14px;
		font-family: inherit;
		font-size: 13px;
		border: none;
		background: none;
		color: var(--text-muted);
		border-radius: 8px 8px 0 0;
		border-bottom: 2px solid transparent;
		cursor: pointer;
	}
	.tabs button.active {
		color: var(--text);
		border-bottom-color: var(--accent);
	}

	.stage {
		padding: 18px;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 12px;
	}
	.preview {
		position: relative;
		overflow: hidden;
		background: var(--bg);
		border: 1px solid var(--border);
	}
	.preview.avatar {
		width: 184px;
		height: 184px;
	}
	.preview.banner {
		width: 100%;
		aspect-ratio: 32 / 9;
	}
	.ph {
		position: absolute;
		inset: 0;
		display: grid;
		place-items: center;
		color: var(--text-muted);
	}
	.ph :global(svg) {
		width: 34px;
		height: 34px;
	}
	.zoom {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		max-width: 320px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.zoom input {
		flex: 1;
		accent-color: var(--accent);
	}
	.hint {
		margin: 0;
		font-size: 11px;
		color: var(--text-muted);
	}

	.picker {
		padding: 0 18px 8px;
	}
	.picker-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 10px;
	}
	.picker-head span {
		font-size: 11px;
		letter-spacing: 0.6px;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.upload {
		padding: 6px 12px;
		font-family: inherit;
		font-size: 12px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
		cursor: pointer;
		text-transform: capitalize;
	}
	.upload:hover {
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.none {
		margin: 0;
		padding: 10px 0;
		font-size: 12px;
		color: var(--text-muted);
	}
	.grid {
		display: grid;
		gap: 8px;
	}
	.grid.avatar {
		grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
	}
	.grid.banner {
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
	}
	.thumb {
		padding: 0;
		overflow: hidden;
		border: 2px solid transparent;
		border-radius: 8px;
		background: var(--bg);
		cursor: pointer;
	}
	.thumb.avatar {
		aspect-ratio: 1;
	}
	.thumb.banner {
		aspect-ratio: 32 / 9;
	}
	.thumb img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.thumb:hover {
		border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
	}
	.thumb.selected {
		border-color: var(--accent);
	}

	footer {
		display: flex;
		justify-content: flex-end;
		gap: 10px;
		padding: 14px 18px;
		border-top: 1px solid var(--border);
	}
	.ghost,
	.primary {
		padding: 8px 18px;
		font-family: inherit;
		font-size: 13px;
		border-radius: 8px;
		cursor: pointer;
	}
	.ghost {
		color: var(--text-muted);
		background: none;
		border: 1px solid var(--border);
	}
	.primary {
		color: #fff;
		background: var(--accent);
		border: 1px solid var(--accent);
	}
	.primary:disabled,
	.upload:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}
</style>
