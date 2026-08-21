<script lang="ts">
	// The media item page's four verbs, on a phone.
	//
	// On a laptop these live in the right rail, where a pointer reaches them
	// for free. On a phone the rail becomes the bottom of a single column,
	// below a hero, a poster and a review — so the things you actually opened
	// the page to do were a scroll away. They sit in a fixed bar above the tab
	// bar instead: Log, Rate, Status, and the watchlist bookmark, all inside
	// the thumb arc, all ≥44px.
	//
	// The bar is mobile-only (CSS), and the sheets it opens are the app's
	// shared BottomSheet, which closes itself if the window ever grows past
	// the breakpoint. Desktop never sees any of it.
	import { onMount } from 'svelte';
	import BottomSheet from '$lib/BottomSheet.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import { softKeyboard, watchSoftKeyboard } from '$lib/keyboard.svelte';
	import type { MediaItem, MediaStatus, MediaType } from '$lib/api';
	import MediaLogSheet from '$lib/media/MediaLogSheet.svelte';
	import StarRating from '$lib/media/StarRating.svelte';
	import { statusLabel } from '$lib/media/helpers';
	import {
		STATUS_ORDER,
		unwatchlistStatus,
		type LogEventLite,
		type LogRequest
	} from '$lib/media/mediaactions';

	let {
		title,
		mediaType,
		item,
		events,
		busy,
		onlog,
		onrate,
		onstatus
	}: {
		title: string;
		mediaType: MediaType;
		item: MediaItem | null;
		events: LogEventLite[];
		busy: boolean;
		/** Each resolves true on success. False keeps the sheet open — the
		 * caller has already shown the error, and the draft should survive. */
		onlog: (body: LogRequest) => Promise<boolean>;
		onrate: (rating: number) => Promise<boolean>;
		onstatus: (status: MediaStatus) => Promise<boolean>;
	} = $props();

	type Sheet = 'log' | 'rate' | 'status' | null;
	let sheet: Sheet = $state(null);

	// Publish the bar's height so anything else anchored to the bottom of the
	// viewport can clear it — a toast lands right here otherwise and covers
	// the button that produced it. Measured rather than declared: offsetHeight
	// is 0 while the bar is display:none, so desktop reports 0 for free and a
	// window crossing the breakpoint corrects itself.
	let barEl: HTMLDivElement | undefined = $state();
	onMount(watchSoftKeyboard);
	$effect(() => {
		const el = barEl;
		if (!el) return;
		// Re-runs when the keyboard flips the bar's display, so the published
		// height follows it down to 0 and back.
		void softKeyboard.up;
		const publish = () =>
			document.documentElement.style.setProperty('--mediabar-h', `${el.offsetHeight}px`);
		publish();
		const observer = new ResizeObserver(publish);
		observer.observe(el);
		return () => {
			observer.disconnect();
			document.documentElement.style.setProperty('--mediabar-h', '0px');
		};
	});

	// What the item was when it went ON the watchlist, so the toggle is
	// reversible within a session. Reset the moment the status moves for any
	// other reason, or it would restore something long gone.
	let beforeWatchlist: MediaStatus | null = $state(null);

	let planned = $derived(item?.status === 'planned');

	async function toggleWatchlist() {
		if (busy || !item) return;
		if (planned) {
			const next = unwatchlistStatus(item, events, beforeWatchlist);
			if (await onstatus(next)) beforeWatchlist = null;
		} else {
			const previous = item.status;
			if (await onstatus('planned')) beforeWatchlist = previous;
		}
	}

	async function setStatus(status: MediaStatus) {
		if (busy) return;
		beforeWatchlist = null;
		if (await onstatus(status)) sheet = null;
	}

	async function rate(rating: number) {
		if (busy) return;
		if (await onrate(rating)) sheet = null;
	}
</script>

<!-- With the keyboard up (editing the review, renaming the item) the screen
     belongs to what is being typed — the same call the tab bar makes. -->
<div
	class="action-bar"
	class:keyboard-up={softKeyboard.up}
	bind:this={barEl}
	role="group"
	aria-label="Item actions"
>
	<button type="button" disabled={busy} onclick={() => (sheet = 'log')}>
		<Glyph name="plus" />
		<span>Log</span>
	</button>
	<button type="button" disabled={busy} onclick={() => (sheet = 'rate')}>
		<Glyph name="star" filled={item?.rating != null} />
		<span>Rate</span>
	</button>
	<!-- A constant label. Showing the standing here instead reads "Watchlist"
	     twice over whenever the item is planned, and a fixed bar whose words
	     move around is a bar you have to read every time. The standing is in
	     the tooltip, and ticked inside the sheet. -->
	<button
		type="button"
		title={item ? `Status: ${statusLabel(item.status, mediaType)}` : 'Status'}
		disabled={busy}
		onclick={() => (sheet = 'status')}
	>
		<Glyph name="flag" />
		<span>Status</span>
	</button>
	<button
		type="button"
		class:on={planned}
		aria-pressed={planned}
		disabled={busy || !item}
		onclick={() => void toggleWatchlist()}
	>
		<Glyph name="bookmark" filled={planned} />
		<span>Watchlist</span>
	</button>
</div>

{#if sheet === 'log'}
	<MediaLogSheet
		{title}
		{mediaType}
		{item}
		{events}
		{busy}
		onsubmit={onlog}
		onclose={() => (sheet = null)}
	/>
{:else if sheet === 'rate'}
	<BottomSheet {title} label="Rate this item" fit onclose={() => (sheet = null)}>
		<div class="pad">
			<div class="rate-stars">
				<StarRating value={item?.rating ?? null} disabled={busy} onrate={(r) => void rate(r)} />
			</div>
		</div>
	</BottomSheet>
{:else if sheet === 'status'}
	<BottomSheet {title} label="Set status" fit onclose={() => (sheet = null)}>
		<div class="pad">
			<div class="status-list" role="radiogroup" aria-label="Status">
				{#each STATUS_ORDER as option (option)}
					<button
						type="button"
						class="status-row"
						role="radio"
						aria-checked={item?.status === option}
						disabled={busy}
						onclick={() => void setStatus(option)}
					>
						<span class="tick" aria-hidden="true">
							{#if item?.status === option}<Glyph name="check" />{/if}
						</span>
						<span>{statusLabel(option, mediaType)}</span>
					</button>
				{/each}
			</div>
		</div>
	</BottomSheet>
{/if}

<style>
	.action-bar {
		display: none;
	}
	.pad {
		padding: 4px 8px 12px;
		overflow-y: auto;
	}
	/* Thumb-sized halves: the whole point of the sheet is that a half star is
	   hittable, which it is not at the rail's 24px. */
	.rate-stars {
		--star-size: 52px;
		display: flex;
		justify-content: center;
		padding: 18px 0 26px;
	}
	.status-list {
		display: flex;
		flex-direction: column;
	}
	.status-row {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		min-height: 48px;
		padding: 0 6px;
		font-family: inherit;
		font-size: 15px;
		text-align: left;
		color: var(--text);
		background: none;
		border: 0;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 60%, transparent);
		border-radius: 0;
		cursor: pointer;
	}
	.status-row:last-child {
		border-bottom: none;
	}
	.status-row:active {
		background: var(--bg-hover);
	}
	.status-row[aria-checked='true'] {
		color: var(--accent);
	}
	.status-row:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.tick {
		flex: none;
		width: 22px;
		height: 22px;
		display: grid;
		place-items: center;
	}
	.tick :global(svg) {
		width: 18px;
		height: 18px;
	}

	@media (max-width: 760px) {
		.action-bar {
			position: fixed;
			left: 0;
			right: 0;
			/* Sits ON the tab bar, not over it: --tabbar-h already carries the
			   safe-area inset, so nothing is added to it here. */
			bottom: var(--tabbar-h, 0px);
			z-index: 44;
			display: grid;
			grid-template-columns: repeat(4, minmax(0, 1fr));
			background: var(--bg);
			border-top: 1px solid var(--border);
		}
		.action-bar.keyboard-up {
			display: none;
		}
		.action-bar button {
			display: flex;
			flex-direction: column;
			align-items: center;
			justify-content: center;
			gap: 3px;
			min-height: 56px;
			padding: 6px 2px;
			font-family: inherit;
			font-size: 11px;
			color: var(--text-muted);
			background: none;
			border: 0;
			border-radius: 0;
			cursor: pointer;
		}
		.action-bar button:active {
			background: var(--bg-hover);
		}
		.action-bar button.on {
			color: var(--accent);
		}
		.action-bar button:disabled {
			opacity: 0.5;
			cursor: default;
		}
		.action-bar button :global(svg) {
			width: 21px;
			height: 21px;
			flex: none;
		}
		.action-bar button span {
			max-width: 100%;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
	}
</style>
