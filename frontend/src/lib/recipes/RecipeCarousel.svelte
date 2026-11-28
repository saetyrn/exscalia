<script lang="ts">
	// The recipe menu as a revolving carousel, after the recipe app's
	// RecipeCarousel: titles ride an arc (carousel.ts), the focused one large
	// with its time and tags as a caption, neighbours tilting and fading away.
	// The wheel over the page, Up and Down, drag or swipe turn it; click, tap,
	// Right or Enter opens the focused recipe with a forward slide.
	//
	// Focus is held as a recipe, not a slot: when the list changes under it (a
	// refresh, a recipe added elsewhere) the same recipe stays in focus, and a
	// deleted one hands focus to whichever took its place. It is remembered
	// for the next visit (menu/focus.ts), and the focused recipe is fetched
	// once focus settles, so its page slides in filled.
	import { untrack } from 'svelte';
	import { prefetchRecipe } from './cache.svelte';
	import {
		clamp,
		createWheelStepper,
		geometry,
		itemLayout,
		itemTransform,
		LINE,
		titleSize
	} from './carousel';
	import { recipesChrome } from './chrome.svelte';
	import { plainKey } from './keys';
	import { focusIndex, recalledFocus, rememberFocus } from './menu/focus';
	import {
		cardMeta,
		DRAG_SLOP,
		dragPosition,
		landing,
		optionId,
		REACH,
		renderRange,
		wheelPixels
	} from './menu/menu';
	import { forward } from './nav';
	import { recipeHref } from './paths';
	import type { RecipeCard } from './types';

	let {
		cards,
		compact = false,
		active = $bindable(0),
		wheelTarget
	}: {
		/** The recipes in menu order; never empty. */
		cards: RecipeCard[];
		/** The phone geometry: one centred column. */
		compact?: boolean;
		/** The focused index, for the page around the menu. */
		active?: number;
		/** Where the wheel turns the menu: the whole page. Defaults to the stage. */
		wheelTarget?: HTMLElement;
	} = $props();

	let stage = $state<HTMLDivElement>();
	let width = $state(0);
	let height = $state(0);

	// The focused recipe, and the slot it was last seen in (for when it goes).
	let focus = $state(untrack(() => ({ path: recalledFocus(), at: 0 })));
	// The live position while a drag is under way; null when settled.
	let dragAt = $state<number | null>(null);
	let dragging = $derived(dragAt !== null);

	let settled = $derived(focusIndex(cards, focus.path, focus.at));
	let pos = $derived(dragAt ?? settled);
	let current = $derived(landing(pos, cards.length));
	let range = $derived(renderRange(current, cards.length));
	let g = $derived(geometry(width, height, compact));
	let titleWidth = $derived(Math.max(120, width - (compact ? 0 : REACH) - 8));

	function focusOn(index: number) {
		const card = cards[clamp(index, 0, cards.length - 1)];
		if (card) focus = { path: card.path, at: clamp(index, 0, cards.length - 1) };
	}

	function step(delta: number) {
		focusOn(current + delta);
	}

	$effect(() => {
		active = current;
	});

	// Remember the focused recipe; a deleted one's stand-in becomes the focus
	// proper, so a later refresh follows it rather than the slot.
	$effect(() => {
		const card = cards[current];
		if (!card) return;
		rememberFocus(card.path);
		if (dragAt === null && untrack(() => focus.path) !== card.path)
			focus = { path: card.path, at: current };
	});

	// Warm the focused recipe once focus rests, so opening it slides in a
	// filled page.
	$effect(() => {
		const path = cards[current]?.path;
		if (!path) return;
		const timer = setTimeout(() => void prefetchRecipe(path), 150);
		return () => clearTimeout(timer);
	});

	let opening = false;
	function open(index: number) {
		const card = cards[index];
		if (!card || opening) return;
		opening = true;
		forward(recipeHref(card.path))
			.catch(() => undefined)
			.finally(() => (opening = false));
	}

	// The wheel anywhere on the page turns the menu, a step per gesture
	// stride (carousel.ts). Not passive: the page must not scroll instead.
	$effect(() => {
		const target = wheelTarget ?? stage;
		if (!target) return;
		const stepper = createWheelStepper();
		const onWheel = (e: WheelEvent) => {
			// Pinch zoom and sideways scrolls are not turns.
			if (e.ctrlKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
			e.preventDefault();
			const travel = wheelPixels(e.deltaY, e.deltaMode, target.clientHeight);
			const dir = stepper(travel, performance.now());
			if (dir) step(dir);
		};
		target.addEventListener('wheel', onWheel, { passive: false });
		return () => target.removeEventListener('wheel', onWheel);
	});

	function onKeydown(e: KeyboardEvent) {
		if (e.shiftKey || !plainKey(e) || recipesChrome.omniaOpen || !cards.length) return;
		// Enter on a corner button or link is that control's.
		const onControl =
			e.target instanceof Element && e.target !== stage && e.target.closest('a, button') !== null;
		switch (e.key) {
			case 'ArrowDown':
				step(1);
				break;
			case 'ArrowUp':
				step(-1);
				break;
			case 'PageDown':
				step(3);
				break;
			case 'PageUp':
				step(-3);
				break;
			case 'Home':
				focusOn(0);
				break;
			case 'End':
				focusOn(cards.length - 1);
				break;
			case 'ArrowRight':
				open(current);
				break;
			case 'Enter':
				if (onControl) return;
				open(current);
				break;
			default:
				return;
		}
		e.preventDefault();
	}

	// Drag or swipe: the menu follows the pointer, then settles on the nearest
	// recipe. The stage takes the pointer only once a press has moved past the
	// slop, so a plain tap still clicks the title under it.
	let press: { id: number; y: number; from: number; moved: boolean } | null = null;
	let justDragged = false;

	function onPointerDown(e: PointerEvent) {
		if (e.button !== 0 || !cards.length) return;
		press = { id: e.pointerId, y: e.clientY, from: current, moved: false };
	}

	function onPointerMove(e: PointerEvent) {
		const p = press;
		if (!p || p.id !== e.pointerId) return;
		const dy = p.y - e.clientY;
		if (!p.moved) {
			if (Math.abs(dy) < DRAG_SLOP) return;
			p.moved = true;
			try {
				stage?.setPointerCapture(e.pointerId);
			} catch {
				// The pointer is already gone.
			}
		}
		dragAt = dragPosition(p.from, dy, compact, cards.length);
	}

	function endDrag() {
		const p = press;
		press = null;
		if (!p?.moved) return;
		const at = dragAt;
		dragAt = null;
		if (at !== null) focusOn(landing(at, cards.length));
		// The click that ends a drag is not a choice.
		justDragged = true;
		setTimeout(() => (justDragged = false), 0);
	}

	function onPointerEnd(e: PointerEvent) {
		if (press?.id === e.pointerId) endDrag();
	}

	function choose(index: number) {
		if (justDragged) return;
		if (index === current) open(index);
		else focusOn(index);
	}
</script>

<svelte:window onkeydown={onKeydown} onblur={endDrag} />

<div
	bind:this={stage}
	bind:clientWidth={width}
	bind:clientHeight={height}
	class="cz-stage"
	class:compact
	style:--reach="{compact ? 0 : REACH}px"
	class:dragging
	role="listbox"
	aria-label="Recipes"
	aria-activedescendant={cards[current] ? optionId(cards[current].path) : undefined}
	tabindex="0"
	onpointerdown={onPointerDown}
	onpointermove={onPointerMove}
	onpointerup={onPointerEnd}
	onpointercancel={onPointerEnd}
>
	{#if width > 0}
		<!-- Options take no keys of their own: the listbox's keys are on the
		     window, and a click is the pointer's way in. -->
		{#each cards.slice(range[0], range[1] + 1) as card, k (card.path)}
			{@const i = range[0] + k}
			{@const layout = itemLayout(i - pos, g)}
			{@const size = titleSize(card.title, titleWidth, g)}
			{@const meta = cardMeta(card)}
			<!-- svelte-ignore a11y_click_events_have_key_events, a11y_interactive_supports_focus -->
			<div
				id={optionId(card.path)}
				class="cz-item"
				class:is-active={i === current}
				role="option"
				aria-selected={i === current}
				aria-label={card.title}
				style:transform={itemTransform(layout)}
				style:opacity={layout.opacity}
				style:z-index={50 - Math.round(Math.abs(i - pos) * 10)}
				style:pointer-events={layout.opacity > 0.05 ? 'auto' : 'none'}
				onclick={() => choose(i)}
			>
				<div
					class="cz-title"
					style:font-size="{size}px"
					style:height="{size * LINE * g.stretch}px"
					style:max-width="{titleWidth}px"
				>
					<span style:transform="scaleY({g.stretch})">{card.title}</span>
				</div>
				<p class="cz-meta rc-caption" aria-hidden={i !== current}>{meta}</p>
			</div>
		{/each}
	{/if}
</div>

<style>
	/* The box reaches left (--reach, menu.ts REACH) over the drawing's column
	   so items curving toward it stay inside the edge mask. */
	.cz-stage {
		position: relative;
		flex: 1;
		min-height: 0;
		margin-left: calc(-1 * var(--reach));
		perspective: 1000px;
		touch-action: none;
		user-select: none;
		-webkit-user-select: none;
		cursor: grab;
		outline: none;
		/* Neighbours dissolve toward the top and bottom edges. */
		-webkit-mask-image: linear-gradient(to bottom, transparent 0%, #000 14%, #000 86%, transparent 100%);
		mask-image: linear-gradient(to bottom, transparent 0%, #000 14%, #000 86%, transparent 100%);
	}
	.cz-stage.dragging {
		cursor: grabbing;
	}
	/* The underline on the focused title marks keyboard focus instead. */
	.cz-stage:focus-visible {
		outline: none;
	}

	.cz-item {
		position: absolute;
		top: 50%;
		left: calc(var(--reach) + 4px);
		transform-origin: left center;
		will-change: transform, opacity;
		transition:
			transform 0.62s cubic-bezier(0.22, 1.12, 0.36, 1),
			opacity 0.45s cubic-bezier(0.4, 0, 0.2, 1);
		color: var(--rc-ink);
		cursor: pointer;
	}
	.cz-stage.dragging .cz-item {
		transition: none;
		cursor: grabbing;
	}

	.cz-title {
		line-height: 1.2;
		white-space: nowrap;
	}
	.cz-title span {
		display: block;
		overflow: hidden;
		text-overflow: ellipsis;
		padding-bottom: 0.06em;
		font-family: var(--rc-serif);
		font-weight: 400;
		letter-spacing: -0.05em;
		transform-origin: top left;
		text-shadow: var(--rc-fringe);
	}

	.cz-meta {
		min-height: 1.4em;
		margin-top: 6px;
		opacity: 0;
		transform: translateY(-4px);
		transition:
			opacity 0.35s ease,
			transform 0.35s ease;
		white-space: nowrap;
	}
	.cz-item.is-active .cz-meta {
		opacity: 1;
		transform: none;
		transition-delay: 0.12s;
	}

	.cz-stage:focus-visible .cz-item.is-active .cz-title span {
		text-decoration: underline;
		text-decoration-thickness: 1px;
		text-underline-offset: 0.12em;
	}

	/* Phones and narrow panes: one centred column. */
	.cz-stage.compact .cz-item {
		left: 0;
		right: 0;
		text-align: center;
		transform-origin: center center;
	}
	.cz-stage.compact .cz-title {
		margin: 0 auto;
	}
	.cz-stage.compact .cz-title span {
		transform-origin: top center;
	}

	@media (prefers-reduced-motion: reduce) {
		.cz-item,
		.cz-meta,
		.cz-item.is-active .cz-meta {
			transition: none;
		}
	}
</style>
