<!--
	Edit a song, a folder or a playlist: names, its picture (from this computer
	or the Gallery), and for folders the colour, glyph and pin, for playlists
	the background with its blur and dim (MUSIC.md section 5).
-->
<script lang="ts">
	import { untrack } from 'svelte';
	import Glyph from '$lib/Glyph.svelte';
	import { modal } from '$lib/modal';
	import { notify } from '$lib/ui.svelte';
	import { musicApi } from './api';
	import { coverUrl, library } from './library.svelte';
	import PicturePicker from './PicturePicker.svelte';
	import { FOLDER_COLOURS, FOLDER_GLYPHS } from './types';

	let {
		kind,
		id,
		onclose
	}: { kind: 'song' | 'folder' | 'playlist'; id: string; onclose: () => void } = $props();

	// What it edits is fixed when it opens; the form holds the edits.
	const open = untrack(() => ({ kind, id }));
	const song = open.kind === 'song' ? library.songs[open.id] : undefined;
	const folder = open.kind === 'folder' ? library.folders[open.id] : undefined;
	const playlist = open.kind === 'playlist' ? library.playlists[open.id] : undefined;

	let title = $state(song?.title ?? '');
	let artist = $state(song?.artist ?? '');
	let album = $state(song?.album ?? '');
	let year = $state(song?.year ? String(song.year) : '');
	let genre = $state(song?.genre ?? '');
	let track = $state(song?.track ? String(song.track) : '');
	let name = $state(folder?.name ?? playlist?.name ?? '');
	let cover = $state<string | null>(song?.cover ?? folder?.cover ?? playlist?.cover ?? null);
	let colour = $state(folder?.colour ?? FOLDER_COLOURS[0]);
	let glyph = $state(folder?.glyph ?? '');
	let pinned = $state(folder?.pinned ?? false);
	let background = $state<string | null>(playlist?.background ?? null);
	let blur = $state(playlist?.blur ?? 24);
	let dim = $state(playlist?.dim ?? 45);
	let picking = $state<'cover' | 'background' | null>(null);
	let saving = $state(false);
	let first: HTMLInputElement | undefined = $state();

	const heading = open.kind === 'song' ? 'Edit song' : open.kind === 'folder' ? 'Edit folder' : 'Edit playlist';

	function changed<T>(now: T, before: T | null | undefined): boolean {
		return now !== (before ?? (typeof now === 'string' ? '' : null));
	}

	async function save(event: Event) {
		event.preventDefault();
		saving = true;
		try {
			if (song) {
				const fields: Record<string, unknown> = {};
				if (changed(title.trim(), song.title)) fields.title = title.trim();
				if (changed(artist.trim(), song.artist)) fields.artist = artist.trim();
				if (changed(album.trim(), song.album)) fields.album = album.trim();
				if (changed(genre.trim(), song.genre)) fields.genre = genre.trim();
				if (year.trim() !== (song.year ? String(song.year) : '')) fields.year = year.trim();
				if (track.trim() !== (song.track ? String(song.track) : '')) fields.track = track.trim();
				if (cover !== song.cover) fields.cover = cover;
				if (Object.keys(fields).length) await musicApi.editSong(id, fields);
			} else if (folder) {
				const fields: Record<string, unknown> = {};
				if (name.trim() !== folder.name) fields.name = name.trim();
				if (cover !== folder.cover) fields.cover = cover;
				if (colour !== folder.colour) fields.colour = colour;
				if ((glyph || null) !== folder.glyph) fields.glyph = glyph || null;
				if (pinned !== folder.pinned) fields.pinned = pinned;
				if (Object.keys(fields).length) await musicApi.editFolder(id, fields);
			} else if (playlist) {
				const fields: Record<string, unknown> = {};
				if (name.trim() !== playlist.name) fields.name = name.trim();
				if (cover !== playlist.cover) fields.cover = cover;
				if (background !== playlist.background) fields.background = background;
				if (blur !== playlist.blur) fields.blur = blur;
				if (dim !== playlist.dim) fields.dim = dim;
				if (Object.keys(fields).length) await musicApi.editPlaylist(id, fields);
			}
			await library.load();
			onclose();
		} catch (err) {
			notify(err instanceof Error ? err.message : String(err), { tone: 'error' });
		} finally {
			saving = false;
		}
	}
</script>

<div class="backdrop" role="presentation" onpointerdown={(e) => e.target === e.currentTarget && onclose()}>
	<form class="sheet" aria-label={heading} use:modal={{ onEscape: () => !picking && onclose(), initialFocus: () => first }} onsubmit={save}>
		<h2>{heading}</h2>

		<div class="picture-row">
			<button type="button" class="picture" title="Change picture" onclick={() => (picking = 'cover')}>
				{#if coverUrl(cover)}
					<img src={coverUrl(cover)} alt="" />
				{:else}
					<span class="blank" style:background={folder ? `#${colour}` : undefined}><Glyph name={folder ? 'folder' : 'disc'} /></span>
				{/if}
			</button>
			<div class="picture-actions">
				<button type="button" class="link" onclick={() => (picking = 'cover')}>Change picture</button>
				{#if cover}<button type="button" class="link" onclick={() => (cover = null)}>Remove</button>{/if}
			</div>
		</div>

		{#if song}
			<label>Title <input bind:this={first} bind:value={title} required /></label>
			<label>Artist <input bind:value={artist} /></label>
			<label>Album <input bind:value={album} /></label>
			<div class="pair">
				<label>Year <input bind:value={year} inputmode="numeric" /></label>
				<label>Track <input bind:value={track} inputmode="numeric" /></label>
			</div>
			<label>Genre <input bind:value={genre} /></label>
		{:else}
			<label>Name <input bind:this={first} bind:value={name} required /></label>
		{/if}

		{#if folder}
			<fieldset>
				<legend>Colour</legend>
				<div class="swatches">
					{#each FOLDER_COLOURS as hex (hex)}
						<button
							type="button"
							class="swatch"
							class:on={colour === hex}
							style:background="#{hex}"
							aria-label="#{hex}"
							aria-pressed={colour === hex}
							onclick={() => (colour = hex)}
						></button>
					{/each}
					<input
						class="custom"
						type="color"
						value="#{colour}"
						aria-label="Custom colour"
						oninput={(e) => (colour = e.currentTarget.value.slice(1))}
					/>
				</div>
			</fieldset>
			<label>
				Glyph
				<select bind:value={glyph}>
					<option value="">None</option>
					{#each FOLDER_GLYPHS as g (g)}<option value={g}>{g}</option>{/each}
				</select>
			</label>
			<label class="check"><input type="checkbox" bind:checked={pinned} /> Pinned to the top</label>
		{/if}

		{#if playlist}
			<div class="picture-row">
				<button type="button" class="picture wide" title="Change background" onclick={() => (picking = 'background')}>
					{#if coverUrl(background)}
						<img src={coverUrl(background)} alt="" style:filter="blur({blur / 6}px) brightness({1 - dim / 100})" />
					{:else}
						<span class="blank">Background</span>
					{/if}
				</button>
				<div class="picture-actions">
					<button type="button" class="link" onclick={() => (picking = 'background')}>Change background</button>
					{#if background}<button type="button" class="link" onclick={() => (background = null)}>Remove</button>{/if}
				</div>
			</div>
			<label>Blur <input type="range" min="0" max="100" bind:value={blur} /></label>
			<label>Dim <input type="range" min="0" max="100" bind:value={dim} /></label>
		{/if}

		<div class="actions">
			<button type="button" class="secondary" onclick={onclose}>Cancel</button>
			<button type="submit" class="primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
		</div>
	</form>
</div>

{#if picking}
	<PicturePicker
		title={picking === 'cover' ? 'Choose a picture' : 'Choose a background'}
		onclose={() => (picking = null)}
		onpick={(path) => {
			if (picking === 'background') background = path;
			else cover = path;
		}}
	/>
{/if}

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
		width: min(460px, calc(100vw - 32px));
		max-height: calc(100dvh - 48px);
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 12px;
		padding: 20px;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 14px;
		box-shadow: 0 20px 48px rgba(0, 0, 0, 0.22);
		color: var(--text);
	}
	h2 {
		margin: 0;
		font-size: 17px;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 12px;
		color: var(--text-muted);
	}
	label.check {
		flex-direction: row;
		align-items: center;
		gap: 8px;
		font-size: 14px;
		color: var(--text);
	}
	input:not([type='checkbox']):not([type='range']):not([type='color']),
	select {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 16px;
	}
	.pair {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
	}
	fieldset {
		border: 0;
		padding: 0;
		margin: 0;
	}
	legend {
		font-size: 12px;
		color: var(--text-muted);
		margin-bottom: 6px;
	}
	.swatches {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: center;
	}
	.swatch {
		width: 28px;
		height: 28px;
		border-radius: 50%;
		border: 2px solid transparent;
		cursor: pointer;
		padding: 0;
	}
	.swatch.on {
		border-color: var(--text);
	}
	.custom {
		width: 32px;
		height: 32px;
		padding: 0;
		border: 0;
		background: none;
		cursor: pointer;
	}
	.picture-row {
		display: flex;
		align-items: center;
		gap: 14px;
	}
	.picture {
		width: 84px;
		height: 84px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 8px;
		overflow: hidden;
		background: var(--bg-hover);
		cursor: pointer;
		flex: none;
	}
	.picture.wide {
		width: 150px;
	}
	.picture img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.blank {
		width: 100%;
		height: 100%;
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--text-muted);
		font-size: 12px;
	}
	.blank :global(svg) {
		width: 30px;
		height: 30px;
	}
	.picture-actions {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 4px;
	}
	.link {
		padding: 4px 0;
		border: 0;
		background: none;
		color: var(--text);
		font: inherit;
		font-size: 14px;
		cursor: pointer;
		text-decoration: underline;
		text-underline-offset: 3px;
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 4px;
	}
	.actions button {
		padding: 9px 16px;
		border-radius: 8px;
		font: inherit;
		cursor: pointer;
	}
	.secondary {
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text);
	}
	.primary {
		border: 1px solid var(--text);
		background: var(--text);
		color: var(--bg);
	}
	@media (max-width: 760px) {
		.actions button,
		.link {
			min-height: 44px;
		}
	}
</style>
