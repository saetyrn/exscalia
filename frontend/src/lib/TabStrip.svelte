<script lang="ts">
	import { flip } from '$lib/motion';
	import { horizontalGapIndexAmong } from '$lib/dragmath';
	import { desktopPlatform, isDesktop } from '$lib/runtime';
	import { omniaActivity } from '$lib/stores.svelte';
	import {
		activateTab,
		closeTab,
		cycleTab,
		jumpToTab,
		newTab,
		reorderTab,
		routeTitle,
		tabs,
		type Tab
	} from '$lib/tabs.svelte';

	let strip: HTMLDivElement | undefined = $state();
	let dragging = $state<string | null>(null);
	let dropIndex = $state<number | null>(null);
	let pointerDrag: { id: string; pointerId: number; startX: number } | null = null;
	let suppressClick = false;

	// Windows has no title bar of its own here, so the strip carries the
	// minimise/maximise/close buttons. macOS keeps its own traffic lights and
	// only needs the room made for them.
	const onWindows = desktopPlatform === 'windows';

	async function windowAction(action: 'minimize' | 'toggleMaximize' | 'close') {
		try {
			const { getCurrentWindow } = await import('@tauri-apps/api/window');
			await getCurrentWindow()[action]();
		} catch {
			// Nothing sensible to do if the window won't answer.
		}
	}

	function middleClose(e: MouseEvent, id: string) {
		if (e.button !== 1) return;
		e.preventDefault();
		e.stopPropagation();
		closeTab(id);
	}

	function pointerDown(e: PointerEvent, id: string) {
		if (e.button !== 0) return;
		pointerDrag = { id, pointerId: e.pointerId, startX: e.clientX };
	}

	function pointerMove(e: PointerEvent) {
		if (!pointerDrag || pointerDrag.pointerId !== e.pointerId || !strip) return;
		if (!dragging) {
			if (Math.abs(e.clientX - pointerDrag.startX) < 5) return;
			dragging = pointerDrag.id;
			dropIndex = tabs.list.findIndex((tab) => tab.id === pointerDrag?.id);
		}
		e.preventDefault();
		const next = horizontalGapIndexAmong(
			strip.querySelectorAll('.tab:not(.dragging)'),
			e.clientX
		);
		if (dropIndex !== next) dropIndex = next;
	}

	function previewTabs(): Tab[] {
		if (!dragging || dropIndex === null) return tabs.list;
		const moving = tabs.list.find((tab) => tab.id === dragging);
		if (!moving) return tabs.list;
		const next = tabs.list.filter((tab) => tab.id !== dragging);
		next.splice(Math.min(dropIndex, next.length), 0, moving);
		return next;
	}

	function clearDrag() {
		pointerDrag = null;
		dragging = null;
		dropIndex = null;
	}

	function pointerUp(e: PointerEvent) {
		if (!pointerDrag || pointerDrag.pointerId !== e.pointerId) return;
		const moving = dragging;
		const at = dropIndex;
		clearDrag();
		if (moving && at !== null) {
			suppressClick = true;
			window.setTimeout(() => (suppressClick = false), 0);
			reorderTab(moving, at);
		}
	}

	function clickTab(id: string) {
		if (suppressClick) return;
		activateTab(id);
	}

	// Track on the window: the keyed live preview moves the grabbed node in
	// the DOM, which can release pointer capture partway through a long drag.
	$effect(() => {
		window.addEventListener('pointermove', pointerMove);
		window.addEventListener('pointerup', pointerUp);
		window.addEventListener('pointercancel', clearDrag);
		return () => {
			window.removeEventListener('pointermove', pointerMove);
			window.removeEventListener('pointerup', pointerUp);
			window.removeEventListener('pointercancel', clearDrag);
		};
	});

	// Keep the active tab visible when it changes (shortcut jumps, restores).
	$effect(() => {
		tabs.activeId;
		strip
			?.querySelector('.tab.active')
			?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
	});

	// Desktop-app-only: real browsers own Cmd+T/W and Ctrl+Tab, so on the web
	// the strip is mouse-driven. Capture phase, or CodeMirror's keymap wins
	// while the editor has focus (same pattern as the sidebar's Cmd+M).
	$effect(() => {
		if (!isDesktop) return;
		const handle = (e: KeyboardEvent) => {
			if (e.ctrlKey && !e.metaKey && !e.altKey && e.key === 'Tab') {
				e.preventDefault();
				e.stopPropagation();
				cycleTab(e.shiftKey ? -1 : 1);
				return;
			}
			const mod = e.metaKey || e.ctrlKey;
			if (!mod || e.altKey || e.shiftKey || e.repeat) return;
			const key = e.key.toLowerCase();
			if (key === 't') {
				e.preventDefault();
				e.stopPropagation();
				newTab();
			} else if (key === 'w') {
				e.preventDefault();
				e.stopPropagation();
				closeTab(tabs.activeId);
			} else if (/^[1-9]$/.test(e.key)) {
				e.preventDefault();
				e.stopPropagation();
				jumpToTab(Number(e.key));
			}
		};
		window.addEventListener('keydown', handle, true);
		return () => window.removeEventListener('keydown', handle, true);
	});
</script>

<!-- The strip doubles as the window's title bar: its background drags the
     window, and a double-click on it zooms, the same as a native one. Child
     buttons carry no drag region, so they stay clickable. -->
<div
	class="strip"
	class:desktop-chrome={isDesktop}
	role="tablist"
	tabindex="-1"
	aria-label="Open tabs"
	bind:this={strip}
	data-tauri-drag-region={isDesktop ? '' : undefined}
>
	{#each previewTabs() as tab (tab.id)}
		<div
			animate:flip={{ duration: 140 }}
			class="tab"
			class:active={tab.id === tabs.activeId}
			class:dragging={tab.id === dragging}
			role="presentation"
			onauxclick={(e) => middleClose(e, tab.id)}
		>
			<button
				type="button"
				class="label"
				title={routeTitle(tab.url)}
				role="tab"
				aria-selected={tab.id === tabs.activeId}
				onpointerdown={(e) => pointerDown(e, tab.id)}
				onclick={() => clickTab(tab.id)}
			>
				{routeTitle(tab.url)}
			</button>
			<button
				type="button"
				class="close"
				title="Close tab"
				aria-label="Close tab"
				onclick={(e) => {
					e.stopPropagation();
					closeTab(tab.id);
				}}
			>
				✕
			</button>
		</div>
	{/each}
	<button type="button" class="new" title="New tab" aria-label="New tab" onclick={() => newTab()}>
		＋
	</button>
	{#if omniaActivity.turns > 0}
		<span class="omnia-working" role="status" aria-live="polite">
			<span class="omnia-dot" aria-hidden="true"></span>
			{omniaActivity.mediaTurns > 0 ? 'OMNIA(M) working…' : 'OMNIA working…'}
		</span>
	{/if}
	{#if onWindows}
		<div class="window-controls">
			<button type="button" title="Minimise" aria-label="Minimise" onclick={() => windowAction('minimize')}>
				&#xE921;
			</button>
			<button
				type="button"
				title="Maximise"
				aria-label="Maximise"
				onclick={() => windowAction('toggleMaximize')}
			>
				&#xE922;
			</button>
			<button
				type="button"
				class="wc-close"
				title="Close"
				aria-label="Close"
				onclick={() => windowAction('close')}
			>
				&#xE8BB;
			</button>
		</div>
	{/if}
</div>

<style>
	.strip {
		display: flex;
		align-items: stretch;
		height: 33px;
		flex-shrink: 0;
		background: var(--bg-panel);
		border-bottom: 1px solid var(--border);
		overflow-x: auto;
		scrollbar-width: none;
	}
	/* As the title bar it has to clear the window controls, and it shares its
	   height with the sidebar header beside it. */
	.strip.desktop-chrome {
		height: var(--titlebar-h);
	}
	/* With the sidebar collapsed the traffic lights reach past the rail into
	   the start of the strip. */
	:global(html[data-desktop='mac'] .shell.rail-only) .strip {
		padding-left: 44px;
	}
	.window-controls {
		display: flex;
		margin-left: auto;
		flex-shrink: 0;
		/* Sits hard against the window's edge, like a native caption bar. */
		align-self: stretch;
	}
	.window-controls button {
		width: 46px;
		border: 0;
		border-radius: 0;
		background: none;
		color: var(--text-muted);
		/* The glyphs are Windows' own caption icons. */
		font-family: 'Segoe Fluent Icons', 'Segoe MDL2 Assets', sans-serif;
		font-size: 10px;
		cursor: pointer;
	}
	.window-controls button:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.window-controls .wc-close:hover {
		background: #c42b1c;
		color: #fff;
	}
	.strip::-webkit-scrollbar {
		display: none;
	}
	.tab {
		display: flex;
		align-items: center;
		min-width: 90px;
		max-width: 180px;
		flex: 0 1 auto;
		border-right: 1px solid var(--border);
		color: var(--text-muted);
	}
	/* Active and hovered tabs speak through text color alone — no background
	   blocks, which would punch holes in the media glass. */
	.tab.active {
		color: var(--text);
	}
	.tab:not(.active):hover {
		color: var(--text);
	}
	.tab.dragging {
		opacity: 0.45;
	}
	.label {
		display: flex;
		align-items: center;
		align-self: stretch;
		flex: 1;
		min-width: 0;
		padding: 0 6px 0 12px;
		border: 0;
		border-radius: 0;
		background: none;
		color: inherit;
		font-size: 12.5px;
		text-align: left;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		user-select: none;
	}
	/* The app-wide button hover paints a pill; tabs are color-only. */
	.label:hover {
		background: none;
		cursor: pointer;
	}
	.close {
		flex-shrink: 0;
		padding: 0 8px 0 2px;
		border: 0;
		background: none;
		color: var(--text-muted);
		font-size: 11px;
		opacity: 0;
		cursor: pointer;
	}
	.tab:hover .close,
	.tab.active .close,
	.close:focus-visible {
		opacity: 1;
	}
	.close:hover {
		color: var(--text);
	}
	.new {
		flex-shrink: 0;
		width: 32px;
		border: 0;
		background: none;
		color: var(--text-muted);
		font-size: 15px;
		cursor: pointer;
	}
	.new:hover {
		color: var(--text);
		background: var(--bg-hover);
	}
	/* OMNIA activity indicator, pinned to the right of the tab bar. */
	.omnia-working {
		margin-left: auto;
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		gap: 7px;
		padding: 0 14px;
		font-size: 12px;
		font-weight: 500;
		white-space: nowrap;
		color: var(--text-muted);
	}
	.omnia-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--accent);
		animation: omnia-pulse 1.1s ease-in-out infinite;
	}
	@keyframes omnia-pulse {
		0%,
		100% {
			opacity: 0.3;
			transform: scale(0.85);
		}
		50% {
			opacity: 1;
			transform: scale(1);
		}
	}

	@media (max-width: 760px) {
		.strip {
			display: none;
		}
	}
</style>
