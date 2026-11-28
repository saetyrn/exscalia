<!--
	The player bar: SoundCloud's layout in EXSCALIA's monochrome tokens
	(MUSIC.md section 4). It owns the app's one <audio> element and sits in the
	root layout outside the route {#key}, so a song keeps playing across every
	navigation. Shown while a song is loaded; closable once paused. Its height
	goes into --playerbar-h so the corner box and toasts sit above it.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import Glyph from '$lib/Glyph.svelte';
	import { coverIn, formatTime, library } from './library.svelte';
	import { player } from './player.svelte';
	import { musicApi } from './api';
	import { notify } from '$lib/ui.svelte';

	let audio: HTMLAudioElement | undefined = $state();
	let bar: HTMLElement | undefined = $state();
	let scrubbing = $state<number | null>(null);

	const song = $derived(player.current ? library.songs[player.current] : undefined);
	const visible = $derived(!!player.current && !player.dismissed);
	const cover = $derived(coverIn(song, player.context));
	const duration = $derived((player.isSpeaker && player.duration) || song?.duration || 0);
	const shown = $derived(scrubbing ?? player.shownPosition);
	const folderHref = $derived(
		player.context ? `/music?folder=${encodeURIComponent(player.context)}` : '/music'
	);

	onMount(() => {
		const stopListening = library.listen();
		const detach = audio ? player.attach(audio) : () => {};
		void library.ensure();
		return () => {
			detach();
			stopListening();
		};
	});

	// The bar's height, for the fixed things that must clear it.
	$effect(() => {
		const root = document.documentElement;
		if (!visible || !bar) {
			root.style.removeProperty('--playerbar-h');
			return;
		}
		const el = bar;
		const observer = new ResizeObserver(() => {
			root.style.setProperty('--playerbar-h', `${el.offsetHeight}px`);
		});
		observer.observe(el);
		return () => {
			observer.disconnect();
			root.style.removeProperty('--playerbar-h');
		};
	});

	async function toggleLike() {
		if (!song) return;
		const want = !song.liked;
		song.liked = want;
		try {
			await musicApi.like(song.id, want);
			void library.load();
		} catch (err) {
			song.liked = !want;
			notify(err instanceof Error ? err.message : String(err), { tone: 'error' });
		}
	}

	function onScrub(event: Event) {
		scrubbing = Number((event.currentTarget as HTMLInputElement).value);
	}
	function onScrubEnd(event: Event) {
		player.seekTo(Number((event.currentTarget as HTMLInputElement).value));
		scrubbing = null;
	}
</script>

<audio bind:this={audio} preload="metadata"></audio>

{#if visible}
	<div class="player-bar" bind:this={bar} role="region" aria-label="Player">
		<div class="controls">
			<button type="button" class="icon" title="Previous" aria-label="Previous" onclick={() => player.prev()}>
				<Glyph name="skip-back" />
			</button>
			<button
				type="button"
				class="icon play"
				title={player.playing ? 'Pause' : 'Play'}
				aria-label={player.playing ? 'Pause' : 'Play'}
				onclick={() => player.toggle()}
			>
				<Glyph name={player.playing ? 'pause' : 'play'} filled />
			</button>
			<button type="button" class="icon" title="Next" aria-label="Next" onclick={() => player.next()}>
				<Glyph name="skip-forward" />
			</button>
			<button
				type="button"
				class="icon toggle"
				class:on={player.shuffle}
				title={player.shuffle ? 'Shuffle on' : 'Shuffle'}
				aria-label="Shuffle"
				aria-pressed={player.shuffle}
				onclick={() => player.toggleShuffle()}
			>
				<Glyph name="shuffle" />
			</button>
			<button
				type="button"
				class="icon toggle"
				class:on={player.repeat !== 'off'}
				title={player.repeat === 'one' ? 'Repeat one' : player.repeat === 'all' ? 'Repeat all' : 'Repeat'}
				aria-label="Repeat"
				onclick={() => player.cycleRepeat()}
			>
				<Glyph name={player.repeat === 'one' ? 'repeat-one' : 'repeat'} />
			</button>
		</div>

		<div class="timeline">
			<span class="time">{formatTime(shown)}</span>
			<input
				class="scrub"
				type="range"
				min="0"
				max={duration || 1}
				step="0.1"
				value={shown}
				style:--fill={`${duration ? (shown / duration) * 100 : 0}%`}
				aria-label="Position"
				oninput={onScrub}
				onchange={onScrubEnd}
			/>
			<span class="time">{formatTime(duration)}</span>
		</div>

		<div class="volume">
			<button
				type="button"
				class="icon"
				title={player.muted ? 'Unmute' : 'Mute'}
				aria-label={player.muted ? 'Unmute' : 'Mute'}
				onclick={() => player.toggleMute()}
			>
				<Glyph name={player.muted || player.volume === 0 ? 'volume-x' : 'volume'} />
			</button>
			<input
				class="level"
				type="range"
				min="0"
				max="1"
				step="0.01"
				value={player.muted ? 0 : player.volume}
				style:--fill={`${(player.muted ? 0 : player.volume) * 100}%`}
				aria-label="Volume"
				oninput={(e) => player.setVolume(Number(e.currentTarget.value))}
			/>
		</div>

		<a class="now" href={folderHref} title="Go to folder">
			{#if cover}
				<img class="cover" src={cover} alt="" />
			{:else}
				<span class="cover blank"><Glyph name="disc" /></span>
			{/if}
			<span class="names">
				<span class="artist">{song?.artist ?? ''}</span>
				<span class="title">{song?.title ?? ''}</span>
				{#if player.remote && player.speakerName}
					<span class="where" title="Playing on {player.speakerName}">On {player.speakerName}</span>
				{/if}
			</span>
		</a>
		{#if player.error}
			<span class="error" role="status">{player.error}</span>
		{/if}

		<div class="end">
			<button
				type="button"
				class="icon toggle"
				class:on={song?.liked}
				title={song?.liked ? 'Unlike' : 'Like'}
				aria-label={song?.liked ? 'Unlike' : 'Like'}
				aria-pressed={!!song?.liked}
				onclick={toggleLike}
			>
				<Glyph name="heart" filled={!!song?.liked} />
			</button>
			<a class="icon" href={folderHref} title="Go to folder" aria-label="Go to folder">
				<Glyph name="folder" />
			</a>
			{#if !player.playing}
				<button type="button" class="icon" title="Close" aria-label="Close player" onclick={() => player.dismiss()}>
					<Glyph name="x" />
				</button>
			{/if}
		</div>
	</div>
{/if}

<style>
	.player-bar {
		flex: none;
		display: flex;
		align-items: center;
		gap: 20px;
		height: 60px;
		padding: 0 20px;
		box-sizing: border-box;
		background: var(--bg-panel);
		border-top: 1px solid var(--border);
		color: var(--text);
		min-width: 0;
	}
	.controls,
	.volume,
	.end {
		display: flex;
		align-items: center;
		gap: 4px;
		flex: none;
	}
	.icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		transition:
			background-color var(--motion-fast),
			color var(--motion-fast);
	}
	.icon :global(svg) {
		width: 20px;
		height: 20px;
	}
	.icon:hover,
	.icon:focus-visible {
		color: var(--text);
		background: var(--bg-hover);
	}
	.icon.play {
		width: 40px;
		height: 40px;
		color: var(--bg);
		background: var(--text);
	}
	.icon.play:hover {
		color: var(--bg);
		background: var(--text);
		opacity: 0.85;
	}
	.icon.toggle.on {
		color: var(--text);
	}
	.timeline {
		flex: 1;
		min-width: 120px;
		display: flex;
		align-items: center;
		gap: 12px;
	}
	.time {
		flex: none;
		min-width: 38px;
		font-size: 12px;
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
		text-align: center;
	}
	input[type='range'] {
		appearance: none;
		-webkit-appearance: none;
		height: 16px;
		margin: 0;
		background: transparent;
		cursor: pointer;
	}
	.scrub {
		flex: 1;
		min-width: 0;
	}
	.level {
		width: 84px;
	}
	input[type='range']::-webkit-slider-runnable-track {
		height: 2px;
		border-radius: 1px;
		background: linear-gradient(
			to right,
			var(--text) var(--fill),
			var(--border) var(--fill)
		);
	}
	input[type='range']::-moz-range-track {
		height: 2px;
		border-radius: 1px;
		background: linear-gradient(
			to right,
			var(--text) var(--fill),
			var(--border) var(--fill)
		);
	}
	input[type='range']::-webkit-slider-thumb {
		-webkit-appearance: none;
		width: 10px;
		height: 10px;
		margin-top: -4px;
		border-radius: 50%;
		background: var(--text);
		opacity: 0;
		transition: opacity var(--motion-fast);
	}
	input[type='range']::-moz-range-thumb {
		width: 10px;
		height: 10px;
		border: 0;
		border-radius: 50%;
		background: var(--text);
		opacity: 0;
	}
	input[type='range']:hover::-webkit-slider-thumb,
	input[type='range']:focus-visible::-webkit-slider-thumb {
		opacity: 1;
	}
	input[type='range']:hover::-moz-range-thumb,
	input[type='range']:focus-visible::-moz-range-thumb {
		opacity: 1;
	}
	.now {
		display: flex;
		align-items: center;
		gap: 12px;
		min-width: 0;
		width: 260px;
		flex: none;
		color: inherit;
		text-decoration: none;
	}
	.cover {
		width: 40px;
		height: 40px;
		flex: none;
		border-radius: 3px;
		object-fit: cover;
		background: var(--bg-hover);
	}
	.cover.blank {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		color: var(--text-muted);
	}
	.cover.blank :global(svg) {
		width: 20px;
		height: 20px;
	}
	.names {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.artist,
	.title {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.artist {
		font-size: 12px;
		color: var(--text-muted);
	}
	.title {
		font-size: 14px;
		font-weight: 600;
	}
	.where {
		font-size: 11px;
		color: var(--text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.error {
		font-size: 12px;
		color: var(--danger);
		flex: none;
	}

	@media (max-width: 1100px) {
		.volume .level,
		.controls .toggle {
			display: none;
		}
		.now {
			width: 200px;
		}
	}

	/* Phones: the mini bar. Cover and names, play, next; the scrubber becomes
	   a hairline along the top edge. */
	@media (max-width: 760px) {
		.player-bar {
			position: relative;
			height: 56px;
			gap: 8px;
			padding: 0 var(--m-gutter);
		}
		.timeline {
			position: absolute;
			left: 0;
			right: 0;
			top: -7px;
			min-width: 0;
		}
		.time,
		.volume,
		.end,
		.controls .icon:first-child {
			display: none;
		}
		.scrub {
			height: 14px;
		}
		.now {
			order: -1;
			flex: 1;
			width: auto;
		}
		.icon {
			width: 44px;
			height: 44px;
		}
		.icon.play {
			width: 44px;
			height: 44px;
		}
	}
</style>
