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
	import { isIPadShell } from '$lib/runtime';
	import { omniaActivity, vault } from '$lib/stores.svelte';
	import { onMount } from 'svelte';

	const BASE_TABS = [
		{ id: 'home', href: '/', label: 'Home', icon: 'home' },
		{ id: 'notes', href: '/notes', label: 'Notes', icon: 'note' },
		{ id: 'media', href: '/media', label: 'Media', icon: 'movie' },
		{ id: 'omnia', href: '/omnia', label: 'OMNIA', icon: 'chat' }
	];

	// No fifth tab on the web: the bar only renders under 760px, where Paper
	// is a read-only viewer and reaches its audience from the home shortcuts
	// row. Inside the iPad shell the WKWebView normally runs at iPad width
	// (sidebar visible, Paper one click away) — but in Split View it drops
	// under the breakpoint and the sidebar goes with it, so there the bar is
	// the only bridge to the native section and earns the extra tab.
	const tabs = isIPadShell
		? [...BASE_TABS, { id: 'paper', href: '/paper', label: 'Paper', icon: 'paper' }]
		: BASE_TABS;

	/**
	 * Which tab owns a path. Routes the bar does not represent — settings,
	 * calendar, tasks, search — deliberately light nothing up rather than
	 * leaving the last tab lit and lying about where you are.
	 */
	function activeTab(pathname: string): string | null {
		if (pathname === '/') return 'home';
		// /paper deliberately lights nothing. On the web no tab owns it; in
		// the iPad shell it is never a URL we come to rest on, because the
		// layout cancels the navigation and hands off to the native section.
		// Stated rather than left to the fallthrough, so it reads as a
		// decision instead of an oversight.
		if (pathname.startsWith('/paper')) return null;
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

	// The desktop tab strip has said "OMNIA working…" since the badge existed,
	// and it is display:none under this breakpoint — so on a phone a turn ran
	// with nothing on screen to say so, and the pending tray filled silently.
	// Both live on the OMNIA tab: the glyph breathes while a turn is in flight,
	// and the count sits on it. Neither carries a word.
	let working = $derived(omniaActivity.turns > 0);
	let pending = $derived(vault.pendingCount);

	function tabTitle(id: string): string | undefined {
		if (id !== 'omnia') return undefined;
		const notes = [
			working ? (omniaActivity.mediaTurns > 0 ? 'OMNIA(M) working…' : 'OMNIA working…') : '',
			pending > 0 ? `${pending} pending change${pending === 1 ? '' : 's'}` : ''
		].filter(Boolean);
		return notes.length ? notes.join(' · ') : undefined;
	}

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
			title={tabTitle(tab.id)}
			onclick={() => goto(tab.href)}
		>
			<span class="icon" class:working={tab.id === 'omnia' && working}>
				<Glyph name={tab.icon} />
				{#if tab.id === 'omnia' && pending > 0}
					<span class="badge" aria-hidden="true">{pending > 99 ? '99+' : pending}</span>
				{/if}
			</span>
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
			position: relative; /* anchors the pending badge */
			display: block;
			width: var(--m-icon-bar);
			height: var(--m-icon-bar);
		}
		.icon :global(svg) {
			display: block;
			width: 100%;
			height: 100%;
		}
		/* The same breathing the chat's "Working…" bubble uses — opacity only,
		   so nothing moves and the glyph stays the glyph. */
		.icon.working {
			animation: omnia-working 1.15s ease-in-out infinite;
		}
		@keyframes omnia-working {
			0%,
			100% {
				opacity: 0.45;
			}
			50% {
				opacity: 1;
			}
		}
		/* Muted on purpose: a count, not an alarm. */
		.badge {
			position: absolute;
			top: -4px;
			right: -10px;
			min-width: 14px;
			height: 14px;
			padding: 0 3px;
			border-radius: 7px;
			background: var(--bg-hover);
			color: var(--text-muted);
			font-size: 9px;
			font-variant-numeric: tabular-nums;
			line-height: 14px;
			text-align: center;
		}
		.label {
			font-size: 10px;
			line-height: 1;
			letter-spacing: 0.2px;
		}
	}
</style>
