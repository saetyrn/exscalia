<!--
	The stage on its own, for tuning its look and motion before it goes into
	the Music page (MUSIC.md, Deploy 4). Reached at /music/lab; nothing links
	to it.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { coverUrl, library } from '$lib/music/library.svelte';
	import Stage from '$lib/music/stage/Stage.svelte';
	import type { StageObject } from '$lib/music/stage/scene';
	import { FOLDER_COLOURS, FOLDER_GLYPHS } from '$lib/music/types';

	onMount(() => {
		void library.ensure();
	});

	const KINDS = ['blank', 'cd', 'vinyl', 'case', 'folder', 'sleeve', 'heart', 'stack', 'clock'] as const;
	type Kind = (typeof KINDS)[number];

	let kind = $state<Kind>('cd');
	let playing = $state(false);
	let colour = $state<string>(FOLDER_COLOURS[0]);
	let glyph = $state<string>('note');
	let pinned = $state(true);
	let withCover = $state(true);

	const covers = $derived(
		[...new Set(Object.values(library.songs).map((s) => s.cover).filter((c): c is string => !!c))]
	);
	let coverIndex = $state(0);
	const cover = $derived(withCover && covers.length ? coverUrl(covers[coverIndex % covers.length]) : null);

	const object = $derived.by((): StageObject => {
		switch (kind) {
			case 'blank':
				return { kind: 'blank' };
			case 'cd':
			case 'vinyl':
			case 'case':
			case 'sleeve':
				return { kind, cover };
			case 'folder':
				return { kind: 'folder', colour, glyph: glyph || null, pinned };
			default:
				return { kind: 'glass', shape: kind };
		}
	});
</script>

<div class="lab">
	<div class="stage"><Stage {object} {playing} /></div>
	<div class="controls">
		<div class="row">
			{#each KINDS as k (k)}
				<button type="button" class:on={kind === k} onclick={() => (kind = k)}>{k}</button>
			{/each}
		</div>
		<div class="row">
			<button type="button" class:on={playing} onclick={() => (playing = !playing)}>{playing ? 'Pause' : 'Play'}</button>
			<label><input type="checkbox" bind:checked={withCover} /> cover</label>
			<button type="button" onclick={() => coverIndex++} disabled={!covers.length}>next cover</button>
		</div>
		{#if kind === 'folder'}
			<div class="row">
				{#each FOLDER_COLOURS as c (c)}
					<button type="button" class="swatch" class:on={colour === c} style:background="#{c}" aria-label={c} onclick={() => (colour = c)}></button>
				{/each}
				<select bind:value={glyph}>
					<option value="">none</option>
					{#each FOLDER_GLYPHS as g (g)}<option value={g}>{g}</option>{/each}
				</select>
				<label><input type="checkbox" bind:checked={pinned} /> pinned</label>
			</div>
		{/if}
	</div>
</div>

<style>
	.lab {
		height: 100%;
		display: flex;
		flex-direction: column;
		background: linear-gradient(#e7e9f0, #dfe1ea);
	}
	:global(html[data-theme='dark']) .lab,
	:global(html[data-theme='black']) .lab {
		background: linear-gradient(#101018, #0a0a0f);
	}
	.stage {
		flex: 1;
		min-height: 0;
		--stage-backdrop: #e3e5ed;
	}
	:global(html[data-theme='dark']) .stage,
	:global(html[data-theme='black']) .stage {
		--stage-backdrop: #0d0d14;
	}
	.controls {
		padding: 12px 20px 20px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.row {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
	}
	button {
		padding: 6px 12px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 13px;
		cursor: pointer;
	}
	button.on {
		background: var(--text);
		color: var(--bg);
	}
	.swatch {
		width: 26px;
		height: 26px;
		padding: 0;
	}
	.swatch.on {
		outline: 2px solid var(--text);
	}
	label {
		font-size: 13px;
	}
</style>
