<script lang="ts">
	// The recipes home, after the recipe app's Library: the revolving menu
	// (RecipeCarousel) and, in the corner, the plus and OMNIA. On a wide pane
	// the focused recipe's drawing, or else its photo, stands in the column to
	// the left of the menu, and a thin track and counter sit under it.
	//
	// The list is read from the cache, so the loader's refresh behind the
	// cached menu, or one a live change starts, lands in place; focus follows
	// the recipe, not the slot. A first visit that has not loaded yet shows
	// nothing, an empty library only a plus.
	import ArtPre from '$lib/recipes/ArtPre.svelte';
	import CornerButton from '$lib/recipes/CornerButton.svelte';
	import CornerNav from '$lib/recipes/CornerNav.svelte';
	import RecipeCarousel from '$lib/recipes/RecipeCarousel.svelte';
	import { cachedList } from '$lib/recipes/cache.svelte';
	import { COMPACT_WIDTH, counter, localImage } from '$lib/recipes/menu/menu';
	import { forward } from '$lib/recipes/nav';
	import { pageTitle, RECIPES_ADD } from '$lib/recipes/paths';

	let { data } = $props();

	let list = $derived(cachedList() ?? data.list);
	let cards = $derived(list?.recipes ?? []);

	let menu = $state<HTMLDivElement>();
	let width = $state(0);
	let compact = $derived(width > 0 && width <= COMPACT_WIDTH);

	let active = $state(0);
	let focused = $derived(cards.length ? cards[Math.min(active, cards.length - 1)] : undefined);
	let photo = $derived(localImage(focused?.thumbnail));

	function addRecipe(e: MouseEvent) {
		if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
		e.preventDefault();
		void forward(RECIPES_ADD);
	}
</script>

<svelte:head><title>{pageTitle()}</title></svelte:head>

<CornerNav label="Menu actions" omnia={true}>
	<CornerButton glyph="plus" label="Add a recipe" href={RECIPES_ADD} />
</CornerNav>

<div class="rc-page rc-menu" class:compact bind:this={menu} bind:clientWidth={width}>
	{#if cards.length}
		<div class="rc-menu-body">
			{#if !compact}
				<div class="rc-menu-side" aria-hidden="true">
					{#if focused}
						{#key focused.path}
							{#if focused.art}
								<ArtPre
									class="rc-menu-art"
									art={focused.art}
									colors={focused.art_colors}
									palette={focused.art_palette}
									label={focused.title}
								/>
							{:else if photo}
								<img class="rc-menu-photo" src={photo} alt="" />
							{/if}
						{/key}
					{/if}
				</div>
			{/if}
			<div class="rc-menu-main">
				<RecipeCarousel {cards} {compact} bind:active wheelTarget={menu} />
				<div class="rc-menu-progress" aria-hidden="true">
					<div
						class="rc-menu-track"
						style:--n={cards.length}
						style:--frac={cards.length > 1 ? active / (cards.length - 1) : 0}
					>
						<span></span>
					</div>
					<span class="rc-caption">{counter(active, cards.length)}</span>
				</div>
			</div>
		</div>
	{:else if list}
		<div class="rc-menu-body">
			{#if !compact}
				<div class="rc-menu-side" aria-hidden="true"></div>
			{/if}
			<div class="rc-menu-main rc-menu-empty">
				<p class="rc-menu-empty-title">No recipes yet</p>
				<a class="rc-btn primary" href={RECIPES_ADD} onclick={addRecipe}>Add a recipe</a>
			</div>
		</div>
	{:else if data.error}
		<p class="rc-error">{data.error}</p>
	{/if}
</div>

<style>
	/* The menu fills the pane and never scrolls: it turns. */
	.rc-menu {
		flex: 1;
		min-height: 0;
		max-width: 1240px;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		padding: 18px var(--rc-gutter) 14px;
	}
	.rc-menu-body {
		flex: 1;
		min-height: 0;
		display: grid;
		grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr);
		gap: 28px;
	}
	.rc-menu.compact .rc-menu-body {
		grid-template-columns: minmax(0, 1fr);
		gap: 0;
	}
	.rc-menu-main {
		display: flex;
		flex-direction: column;
		min-width: 0;
		min-height: 0;
	}

	/* The focused recipe's drawing or photo, centred in the left column, under
	   the titles that curve over it. */
	.rc-menu-side {
		position: relative;
		display: grid;
		place-items: center;
		min-width: 0;
		pointer-events: none;
	}
	.rc-menu-side :global(.rc-menu-art) {
		max-width: 100%;
		overflow: hidden;
		font-size: clamp(9px, 0.82vw, 11px);
		animation: rc-menu-art-in 0.6s cubic-bezier(0.2, 0.7, 0.2, 1) both;
	}
	.rc-menu-photo {
		width: min(66%, 370px);
		aspect-ratio: 1;
		object-fit: cover;
		border-radius: 50%;
		border: 1px solid rgba(255, 255, 255, 0.18);
		box-shadow: 0 18px 60px rgba(0, 0, 0, 0.6);
		filter: saturate(0.9) contrast(0.96);
		animation: rc-menu-photo-in 0.6s cubic-bezier(0.2, 0.7, 0.2, 1) both;
	}
	@keyframes rc-menu-art-in {
		from {
			opacity: 0;
			filter: blur(3px);
		}
	}
	@keyframes rc-menu-photo-in {
		from {
			opacity: 0;
			transform: scale(0.94);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}

	/* A thin track with the counter centred under it, lined up with the
	   titles' left edge; centred on a compact menu. */
	.rc-menu-progress {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 10px;
		box-sizing: content-box;
		width: 168px;
		padding: 10px 0 4px 4px;
		pointer-events: none;
	}
	.rc-menu.compact .rc-menu-progress {
		align-self: center;
		padding-left: 0;
	}
	.rc-menu-track {
		position: relative;
		width: 100%;
		height: 2px;
		border-radius: 2px;
		background: var(--rc-hair);
	}
	.rc-menu-track span {
		--seg: max(14px, calc(100% / var(--n)));
		position: absolute;
		top: 0;
		bottom: 0;
		width: var(--seg);
		left: calc((100% - var(--seg)) * var(--frac));
		border-radius: 2px;
		background: var(--rc-muted);
		transition: left 0.5s var(--rc-ease);
	}

	/* An empty library: the line where the menu would turn, and the way to
	   add the first recipe; centred on a compact menu. */
	.rc-menu-empty {
		justify-content: center;
		align-items: flex-start;
		gap: 18px;
	}
	.rc-menu-empty-title {
		margin-bottom: 26px;
		color: var(--rc-muted);
		font: 400 clamp(2rem, 5vw, 3.6rem) / 1.2 var(--rc-serif);
		letter-spacing: -0.05em;
		text-shadow: var(--rc-fringe);
		/* The stretched serif; not feedback. */
		transform: scaleY(1.9);
		transform-origin: left center;
	}
	.rc-menu.compact .rc-menu-empty {
		align-items: center;
		text-align: center;
	}
	.rc-menu.compact .rc-menu-empty-title {
		transform-origin: center;
	}


	@media (prefers-reduced-motion: reduce) {
		.rc-menu-side :global(.rc-menu-art),
		.rc-menu-photo {
			animation: none;
		}
		.rc-menu-track span {
			transition: none;
		}
	}
</style>
