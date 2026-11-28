<script lang="ts">
	// A phone-sized modal drawer: scrim, then a panel hinged to the bottom edge.
	//
	// Two places needed the same thing at once — OMNIA on a note, and the media
	// item page's chat popover, which on a phone was a 440px dropdown pinned to
	// a corner. Both wanted the identical geometry, the identical z lane and the
	// identical soft-keyboard behaviour, so it lives here once instead of
	// twice.
	//
	// Mobile only. The callers keep their desktop chrome (a side panel tab, a
	// dropdown) and only reach for this under the 760px breakpoint; the resize
	// guard below closes the sheet if the window grows past it mid-session, so a
	// hidden ChatPanel is never left mounted behind a desktop layout.
	import { onMount, type Snippet } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from '$lib/motion';
	import Glyph from '$lib/Glyph.svelte';
	import { softKeyboard, watchSoftKeyboard } from '$lib/keyboard.svelte';
	import { modal } from '$lib/modal';

	let {
		title,
		label,
		fit = false,
		onclose,
		children
	}: {
		/** Shown muted in the header — what this conversation is about. */
		title: string;
		/** Accessible name for the dialog itself. */
		label: string;
		/** Size to the content instead of the standing 72dvh. A chat wants the
		 * full drawer whatever is in it; a four-row form does not, and the
		 * dead space below one reads as a broken sheet. 72dvh stays the cap. */
		fit?: boolean;
		onclose: () => void;
		children: Snippet;
	} = $props();

	onMount(() => {
		watchSoftKeyboard();
		const wide = window.matchMedia('(min-width: 761px)');
		if (wide.matches) {
			onclose();
			return;
		}
		const onChange = (e: MediaQueryListEvent) => {
			if (e.matches) onclose();
		};
		wide.addEventListener('change', onChange);
		return () => wide.removeEventListener('change', onChange);
	});
</script>

<!-- One element owns the z lane (--z-sheet, 70: above the tab bar and
     MobileEditBar, below dialogs at 1000). Scrim and sheet stack inside it, so
     neither has to know what else is on screen. The panel is the modal: focus
     opens on its close button and goes back to the opener on close. -->
<div class="sheet-layer">
	<!-- In-only transitions, the same reasoning as the layout's route view: an
	     out transition on the sheet would animate a panel whose contents are
	     already being torn down. Closing is immediate and clean. -->
	<button
		type="button"
		class="sheet-scrim"
		aria-label="Close"
		tabindex="-1"
		onclick={onclose}
		in:fade={{ duration: 120 }}
	></button>
	<div
		class="sheet-panel"
		class:fit
		role="dialog"
		aria-modal="true"
		aria-label={label}
		style:bottom="{softKeyboard.inset}px"
		style:max-height="calc(100dvh - {softKeyboard.inset}px - 48px)"
		style:padding-bottom={softKeyboard.inset ? '0px' : 'env(safe-area-inset-bottom)'}
		use:modal={{ onEscape: () => onclose() }}
		in:fly={{ y: 28, duration: 170, easing: cubicOut }}
	>
		<header class="sheet-head">
			<span class="sheet-title">{title}</span>
			<button type="button" class="sheet-close" aria-label="Close" onclick={onclose}>
				<Glyph name="x" />
			</button>
		</header>
		<div class="sheet-body">
			{@render children()}
		</div>
	</div>
</div>

<style>
	.sheet-layer {
		position: fixed;
		inset: 0;
		z-index: var(--z-sheet);
	}
	.sheet-scrim {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		min-height: 0;
		padding: 0;
		border: 0;
		border-radius: 0;
		background: rgb(0 0 0 / 42%);
	}
	.sheet-panel {
		position: absolute;
		left: 0;
		right: 0;
		/* 72dvh normally; the max-height above takes over when an overlay
		   keyboard is covering part of the screen that dvh still counts. */
		height: 72dvh;
		display: flex;
		flex-direction: column;
		min-height: 0;
		border-radius: 16px 16px 0 0;
		background: var(--bg);
		/* Not a border: intoxicate mode forces every --border transparent, and
		   the sheet would lose its edge against the scrim. A shadow drawn from
		   --text survives the theme and separates it either way. */
		box-shadow:
			0 -1px 0 color-mix(in srgb, var(--text) 14%, transparent),
			0 -12px 32px rgb(0 0 0 / 26%);
		overflow: hidden;
	}
	.sheet-panel.fit {
		height: auto;
	}
	/* Even gutters at both ends. The head was padded 16px on the left and 4px
	   on the right and looked lopsided for it: the right-hand item is a 44px
	   touch target wrapped around an 18px glyph, so the gutter has to measure
	   to the glyph, not to the button. The close button's negative margin below
	   eats the difference back. */
	.sheet-head {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px var(--m-gutter);
	}
	.sheet-title {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		color: var(--text-muted);
		font-size: var(--m-meta);
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sheet-close {
		flex-shrink: 0;
		width: 44px;
		height: 44px;
		/* Gutter (16) − 13 leaves the glyph's own edge on the gutter line while
		   the tap target keeps its full 44px. */
		margin-right: -13px;
		display: grid;
		place-items: center;
		padding: 0;
		border: 0;
		border-radius: 0;
		background: none;
		color: var(--text-muted);
	}
	.sheet-close:active {
		background: var(--bg-active);
	}
	.sheet-close :global(svg) {
		width: var(--m-icon);
		height: var(--m-icon);
	}
	/* ChatPanel wants a bounded flex parent — flex column, min-height 0 —
	   or its transcript grows the sheet instead of scrolling inside it.
	   The side padding stays at 8px deliberately: every consumer already lays
	   its own gutter inside (ChatPanel's transcript, the log/rate sheets' .pad,
	   the briefing), so raising this to the 16px gutter would stack two of them
	   and inset the content by 24–32px. */
	.sheet-body {
		flex: 1;
		display: flex;
		flex-direction: column;
		min-height: 0;
		padding: 0 8px 8px;
	}
</style>
