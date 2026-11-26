<script lang="ts">
	import { page } from '$app/state';
	import { cubicOut } from 'svelte/easing';
	import MediaChatPopover from '$lib/MediaChatPopover.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import MediaNav from '$lib/media/MediaNav.svelte';

	let { children } = $props();

	// This shell now persists across the four hub tabs (the root {#key}
	// collapses them to one key), so only the content below swaps. Slide the
	// incoming content horizontally in tab order: forward (a later tab) enters
	// from the right, backward from the left. In-only — the outgoing content is
	// removed instantly, so nothing lingers in flow to shift the layout; the
	// wrapper clips the horizontal overhang so no scrollbar appears.
	const TAB_INDEX: Record<string, number> = {
		'/media': 0,
		'/media/library': 1,
		'/media/activity': 2,
		'/media/watchlist': 3
	};

	// Tracked across intros to pick direction. Read at intro time (the new
	// node is created with page.url.pathname already advanced), so it needs no
	// effect-ordering coordination.
	let prevIndex = TAB_INDEX[page.url.pathname] ?? 0;

	let shell = $state<HTMLDivElement>();
	// The persisted shell keeps its scrollTop; reset to the top on every tab
	// change so each tab opens at its start, matching the old remount behavior.
	$effect(() => {
		const p = page.url.pathname;
		if (TAB_INDEX[p] !== undefined && shell) shell.scrollTop = 0;
	});

	function hubSlide(_node: Element) {
		const curIdx = TAB_INDEX[page.url.pathname] ?? prevIndex;
		const direction = curIdx >= prevIndex ? 1 : -1;
		prevIndex = curIdx;

		const reduce =
			typeof window !== 'undefined' &&
			window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		const mobile =
			typeof window !== 'undefined' &&
			window.matchMedia('(max-width: 760px)').matches;

		// Reduced motion: opacity only. Desktop: a plain fade matching the old
		// remount fade — no slide, no layout change. Mobile: directional slide.
		if (reduce) return { duration: 0, css: (t: number) => `opacity: ${t}` };
		if (!mobile)
			return { duration: 140, easing: cubicOut, css: (t: number) => `opacity: ${t}` };
		return {
			duration: 220,
			easing: cubicOut,
			css: (t: number, u: number) =>
				`opacity: ${t}; transform: translateX(${direction * 28 * u}px)`
		};
	}
</script>

<div class="media-shell" bind:this={shell}>
	<!-- One header row in normal flow — arrows, section nav, OMNIA — so the
	     chrome is identical on every hub page and nothing can overlap. Pages
	     put their own controls (search, sort, filters) below, never here. -->
	<header class="media-header">
		<div class="nav-history" role="group" aria-label="Page history">
			<button
				type="button"
				class="nav-btn"
				title="Back"
				aria-label="Go back to the previously visited page"
				onclick={() => window.history.back()}
			>
				<Glyph name="back" />
			</button>
			<button
				type="button"
				class="nav-btn"
				title="Forward"
				aria-label="Go forward to the next visited page"
				onclick={() => window.history.forward()}
			>
				<Glyph name="forward" />
			</button>
			<a class="nav-btn" href="/media" title="Media home" aria-label="Go to the media hub">
				<Glyph name="home" />
			</a>
		</div>
		<MediaNav />
		<div class="header-end">
			<MediaChatPopover
				inline
				placeholder="Ask OMNIA…"
				starters={['What should I watch next?', 'Log something I just finished']}
			/>
		</div>
	</header>

	<div class="hub-content">
		{#key page.url.pathname}
			<div class="hub-page" in:hubSlide>
				{@render children()}
			</div>
		{/key}
	</div>
</div>

<style>
	.media-shell {
		/* The standard media look — black surface, white type — shared by every
		   hub page; only --accent follows the app theme. Keep in step with the
		   identical block on the item page (media/media/[...file]). */
		--bg: #000000;
		--bg-panel: #17171a;
		--border: #2a2a2e;
		--text: #ffffff;
		--text-muted: #9c9ca3;
		--accent-soft: color-mix(in srgb, var(--accent) 16%, transparent);
		--bg-hover: color-mix(in srgb, #ffffff 7%, transparent);
		position: relative;
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		background: var(--bg);
		color: var(--text);
	}
	.hub-content {
		/* Clip (not hidden) the horizontal slide overhang so the incoming tab
		   never spawns a scrollbar. `clip` leaves overflow-y visible, so the
		   .media-shell above stays the sole vertical scroll container. */
		overflow-x: clip;
	}
	.media-header {
		display: flex;
		align-items: center;
		gap: 14px;
		max-width: 1760px;
		margin: 0 auto;
		padding: 10px 16px 0;
	}
	.media-header :global(.media-nav) {
		margin-bottom: 0;
		flex: none;
	}
	.header-end {
		margin-left: auto;
		display: flex;
		align-items: center;
	}
	.nav-history {
		display: flex;
		align-items: center;
		gap: 2px;
		flex: none;
	}
	.nav-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		padding: 0;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		text-decoration: none;
		transition: color var(--motion-fast);
	}
	.nav-btn:hover {
		color: var(--text);
	}
	.nav-btn :global(svg) {
		width: 17px;
		height: 17px;
	}
	@media (max-width: 760px) {
		.media-header {
			flex-wrap: wrap;
			gap: 8px;
			padding-inline: var(--m-gutter);
		}
		.media-header :global(.media-nav) {
			order: 3;
			width: 100%;
			overflow-x: auto;
		}
		/* Thumb-sized boxes, the glyph still centred. The home link is an <a>,
		   which the mobile button floor never reached. */
		.nav-btn {
			width: 44px;
			height: 44px;
		}
	}
</style>
