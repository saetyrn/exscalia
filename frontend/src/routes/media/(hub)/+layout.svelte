<script lang="ts">
	import MediaChatPopover from '$lib/MediaChatPopover.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import MediaNav from '$lib/media/MediaNav.svelte';

	let { children } = $props();
</script>

<div class="media-shell">
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

	{@render children()}
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
			padding-inline: 10px;
		}
		.media-header :global(.media-nav) {
			order: 3;
			width: 100%;
			overflow-x: auto;
		}
	}
</style>
