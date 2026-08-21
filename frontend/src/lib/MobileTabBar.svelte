<script lang="ts">
	// The four places a phone actually goes.
	//
	// On a laptop the sidebar is always open and every route is one click
	// away; on a phone it lives behind a hamburger, so the four destinations
	// that carry the app — the home dashboard, notes, media, OMNIA — cost a
	// drawer open each. This bar puts them under the thumb instead.
	//
	// It is in flow, the last child of <main>, so the route view shrinks to
	// make room rather than having content hide behind it. Two consequences:
	// only chrome that *floats* over the route (fixed sheets, the pending box)
	// needs --tabbar-h from app.css to stay clear, and a route's own scroll
	// container must NOT pad env(safe-area-inset-bottom) — the bar below it
	// already carries the inset, so adding it again is dead space twice.
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Glyph from '$lib/Glyph.svelte';
	import { softKeyboard, watchSoftKeyboard } from '$lib/keyboard.svelte';
	import { onMount } from 'svelte';

	const tabs = [
		{ id: 'home', href: '/', label: 'Home', icon: 'home' },
		{ id: 'notes', href: '/notes', label: 'Notes', icon: 'note' },
		{ id: 'media', href: '/media', label: 'Media', icon: 'movie' },
		{ id: 'omnia', href: '/omnia', label: 'OMNIA', icon: 'chat' }
	] as const;

	/**
	 * Which tab owns a path. Routes the bar does not represent — settings,
	 * calendar, tasks, search — deliberately light nothing up rather than
	 * leaving the last tab lit and lying about where you are.
	 */
	function activeTab(pathname: string): string | null {
		if (pathname === '/') return 'home';
		if (
			pathname === '/notes' ||
			pathname.startsWith('/notes/') ||
			pathname.startsWith('/note/') ||
			pathname.startsWith('/tags/')
		)
			return 'notes';
		if (pathname.startsWith('/media')) return 'media';
		if (pathname === '/omnia' || pathname === '/pending' || pathname === '/history/omnia')
			return 'omnia';
		return null;
	}

	// page.url.pathname, not the layout's routeKey: the key deliberately
	// freezes during a held navigation (a rename moving the URL under the open
	// note), so reading it would leave the bar pointing at the old route.
	let active = $derived(activeTab(page.url.pathname));

	onMount(watchSoftKeyboard);
</script>

<!-- With the keyboard up the screen belongs to what is being typed, and
     MobileEditBar takes this strip of it. Hiding the bar in flow only gives
     the shortened viewport its height back — nothing above it moves. -->
<nav class="tab-bar" class:keyboard-up={softKeyboard.up} aria-label="Main">
	{#each tabs as tab (tab.id)}
		<button
			type="button"
			class="tab"
			class:active={active === tab.id}
			aria-current={active === tab.id ? 'page' : undefined}
			onclick={() => goto(tab.href)}
		>
			<span class="icon"><Glyph name={tab.icon} /></span>
			<span class="label">{tab.label}</span>
		</button>
	{/each}
</nav>

<style>
	/* Desktop keeps the sidebar; the bar exists only under the app-wide
	   mobile breakpoint. */
	.tab-bar {
		display: none;
	}

	@media (max-width: 760px) {
		.tab-bar {
			display: flex;
			/* Fixed to the real viewport bottom: the 100dvh flex column ends
			   short of the physical screen on some iOS contexts, and an
			   in-flow bar inherited that gap. Fixed positioning anchors to
			   the true bottom regardless; main pads by --tabbar-h to clear. */
			position: fixed;
			left: 0;
			right: 0;
			bottom: 0;
			z-index: 45;
			/* Content-sized on purpose: an explicit height built from the same
			   env() as the padding double-counted the inset on real iPhones.
			   Height = the 48px tabs + whatever inset the device reports —
			   nothing to disagree. */
			height: auto;
			/* Clamp the inset: iOS reports address-bar-sized clearance that an
			   installed web app doesn't need. A small cushion for the home
			   indicator, never the full reservation. */
			padding-bottom: max(8px, min(env(safe-area-inset-bottom, 0px), 20px));
			background: var(--bg);
			/* A border would vanish in intoxicate mode, which forces every
			   --border to transparent; a shadow drawn from --text survives it
			   and still separates the bar from the route above. */
			box-shadow: 0 -1px 0 color-mix(in srgb, var(--text) 12%, transparent);
		}
		.tab-bar.keyboard-up {
			display: none;
		}
		.tab {
			flex: 1 1 0;
			min-width: 0;
			display: flex;
			flex-direction: column;
			align-items: center;
			justify-content: center;
			gap: 3px;
			height: 48px;
			padding: 0;
			border: 0;
			border-radius: 0;
			background: none;
			color: var(--text-muted);
		}
		/* Press feedback is background only — no lift, no squash. */
		.tab:active {
			background: var(--bg-active);
		}
		.tab.active {
			color: var(--accent);
		}
		.icon {
			display: block;
			width: 21px;
			height: 21px;
		}
		.icon :global(svg) {
			display: block;
			width: 100%;
			height: 100%;
		}
		.label {
			font-size: 10px;
			line-height: 1;
			letter-spacing: 0.2px;
		}
	}
</style>
