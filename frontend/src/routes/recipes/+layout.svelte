<script module lang="ts">
	import { watchRecipesLive } from '$lib/recipes/live.svelte';

	// The root layout remounts this layout on every pathname change, and a
	// remount would drop a burst of changes the listener is still coalescing
	// (one that landed while a slide held the old page, say). So every mount
	// shares one listener, which stops a moment after the last one goes.
	let live: { stop: () => void; users: number } | null = null;
	function followLive(): () => void {
		const mine = (live ??= { stop: watchRecipesLive(), users: 0 });
		mine.users += 1;
		return () => {
			mine.users -= 1;
			setTimeout(() => {
				if (mine.users > 0 || live !== mine) return;
				mine.stop();
				live = null;
			});
		};
	}
</script>

<script lang="ts">
	// Every recipe screen stands in this pane: the black look (recipes.css,
	// scoped here and to :root[data-recipes-look], which the root layout sets),
	// the scanline overlay, the corner row a page hands up through CornerNav,
	// the page slides, and OMNIA. Like the media hub's header, the pane offers
	// one OMNIA popover; here the corner's chat button or K opens it, about the
	// recipe the page names or the Recipes conversation.
	//
	// The root layout remounts this layout with the page on every pathname
	// change. The slides rely on that: onNavigate below starts a View
	// Transition before the swap, so the old pane is captured as a picture
	// before it goes and the new one as it lands (lib/recipes/transition.ts).
	import { onNavigate } from '$app/navigation';
	import MediaChatPopover from '$lib/MediaChatPopover.svelte';
	import { overseerStatus } from '$lib/stores.svelte';
	import { recipesChrome } from '$lib/recipes/chrome.svelte';
	import { plainKey } from '$lib/recipes/keys';
	import { slideIn, slideNavigation } from '$lib/recipes/transition';
	import '$lib/recipes/recipes.css';

	let { children } = $props();

	// Each screen opens with OMNIA closed, whatever the last one left.
	recipesChrome.omniaOpen = false;

	onNavigate(slideNavigation);

	// The cache, and so every screen, follows the vault's live events while
	// the user is in Recipes.
	$effect(() => followLive());

	function onKeydown(e: KeyboardEvent) {
		if (e.key.toLowerCase() !== 'k' || e.shiftKey || e.repeat || !plainKey(e)) return;
		if (recipesChrome.omnia === false || !overseerStatus.available) return;
		e.preventDefault();
		recipesChrome.omniaOpen = true;
	}
</script>

<svelte:window onkeydown={onKeydown} />

<div class="rc-root">
	<div class="rc-shell" use:slideIn>
		<div class="rc-scroll">
			{@render children()}
		</div>
		<div class="rc-corner-slot">
			{@render recipesChrome.corner?.()}
		</div>
	</div>
	<div class="rc-tv" aria-hidden="true">
		<div class="rc-tv-scan"></div>
		<div class="rc-tv-grain"></div>
	</div>
	{#if recipesChrome.omnia !== false}
		<div class="rc-omnia">
			<MediaChatPopover
				inline
				trigger={false}
				align="left"
				bind:open={recipesChrome.omniaOpen}
				contextPath={typeof recipesChrome.omnia === 'string' ? recipesChrome.omnia : null}
				hub={{ key: 'recipes:hub', title: 'Recipes' }}
				media={false}
				focusComposer
				placeholder="Ask OMNIA…"
			/>
		</div>
	{/if}
</div>
