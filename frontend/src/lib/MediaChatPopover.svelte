<script lang="ts">
	import { onMount } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fly } from '$lib/motion';
	import BottomSheet from '$lib/BottomSheet.svelte';
	import ChatPanel from '$lib/ChatPanel.svelte';
	import { modalOpen } from '$lib/modal';
	import {
		ensureMediaHubThread,
		ensureNoteThread,
		mediaHubChat,
		noteChatFor,
		overseerStatus,
		restoreMediaHubThread,
		restoreNoteThread
	} from '$lib/stores.svelte';

	// On an item page `contextPath` is the media note, so the conversation is
	// that note's own thread and OMNIA sees the item; the hub has a thread of
	// its own. Absolute placement (with `offset` from the right edge) is
	// for the item page's hero chrome; `inline` puts the button in normal flow
	// so a header row can carry it without overlap.
	let {
		contextPath = null,
		offset = 12,
		inline = false,
		placeholder = 'Ask OMNIA…',
		starters = []
	}: {
		contextPath?: string | null;
		offset?: number;
		inline?: boolean;
		placeholder?: string;
		starters?: string[];
	} = $props();

	let open = $state(false);

	// After a reload the popover opened on the empty state and its starters
	// even when a conversation existed — nothing loaded it until you sent a
	// message. Keyed on `open` because the panel only exists while open.
	$effect(() => {
		if (!open) return;
		void (contextPath ? restoreNoteThread(contextPath) : restoreMediaHubThread());
	});

	// A 440px dropdown anchored to a corner is a laptop shape. Under the app's
	// breakpoint the same conversation opens as a bottom sheet instead — same
	// component the note page uses, so there is one drawer in the app, not two.
	// This is a structural swap, not a restyle, so it has to be decided in JS.
	let mobile = $state(false);
	onMount(() => {
		const query = window.matchMedia('(max-width: 760px)');
		mobile = query.matches;
		const onChange = (e: MediaQueryListEvent) => (mobile = e.matches);
		query.addEventListener('change', onChange);
		return () => query.removeEventListener('change', onChange);
	});

	/** The note's name, for the sheet header. */
	let sheetTitle = $derived(
		contextPath ? (contextPath.split('/').pop()?.replace(/\.md$/, '') ?? 'OMNIA') : 'OMNIA'
	);

	function onWindowClick(e: MouseEvent) {
		// The sheet has its own scrim; a click anywhere inside it would otherwise
		// count as "outside the dropdown" and close it mid-typing.
		if (!open || mobile) return;
		const target = e.target;
		if (target instanceof Element && target.closest('.omnia-pop-wrap')) return;
		open = false;
	}

	function onWindowKeydown(e: KeyboardEvent) {
		// The phone sheet is a modal and takes its own Escape from the layer
		// stack; this is for the desktop dropdown, and yields to any modal open
		// over it (a confirm raised from a chat card).
		if (e.key === 'Escape' && open && !modalOpen()) {
			// Consumed: the layout's Escape (the briefing) must not act too.
			e.preventDefault();
			open = false;
		}
	}
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

{#snippet conversation()}
	{#if contextPath}
		<ChatPanel
			chat={noteChatFor(contextPath)}
			ensureThread={() => ensureNoteThread(contextPath)}
			{contextPath}
			compact
			media
			{placeholder}
			{starters}
		/>
	{:else}
		<!-- The hub used to pass the global chat with no thread behind it, so a
		     question here wrote into whichever conversation was last active. -->
		<ChatPanel
			chat={mediaHubChat()}
			ensureThread={ensureMediaHubThread}
			compact
			media
			{placeholder}
			{starters}
		/>
	{/if}
{/snippet}

{#if overseerStatus.available}
	<div class="omnia-pop-wrap" class:inline style="--offset: {offset}px">
		<button
			type="button"
			class="omnia-btn"
			title="Chat with OMNIA"
			aria-label="Chat with OMNIA"
			aria-expanded={open}
			aria-haspopup={mobile ? 'dialog' : 'true'}
			onclick={() => (open = !open)}
		>
			<svg
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />
			</svg>
		</button>
		{#if open && !mobile}
			<div class="omnia-pop" transition:fly={{ y: -6, duration: 160, easing: cubicOut }}>
				{@render conversation()}
			</div>
		{/if}
	</div>
	{#if open && mobile}
		<BottomSheet title={sheetTitle} label="Chat with OMNIA" onclose={() => (open = false)}>
			{@render conversation()}
		</BottomSheet>
	{/if}
{/if}

<style>
	.omnia-pop-wrap {
		position: absolute;
		top: 10px;
		right: var(--offset);
		z-index: 3;
	}
	.omnia-pop-wrap.inline {
		position: relative;
		top: auto;
		right: auto;
	}
	.omnia-btn {
		width: 30px;
		height: 30px;
		display: grid;
		place-items: center;
		padding: 0;
		border: none;
		background: none;
		color: var(--text);
		cursor: pointer;
		opacity: 0.8;
		filter: drop-shadow(0 1px 4px color-mix(in srgb, var(--bg) 85%, transparent));
		transition: opacity var(--motion-fast), color var(--motion-fast);
	}
	.omnia-btn:hover,
	.omnia-btn[aria-expanded='true'] {
		color: var(--accent);
		opacity: 1;
	}
	.omnia-btn:focus-visible {
		outline: 1px solid currentColor;
		outline-offset: 1px;
	}
	.omnia-btn svg {
		width: 17px;
		height: 17px;
	}
	.omnia-pop {
		position: absolute;
		top: calc(100% + 8px);
		right: 0;
		width: min(440px, calc(100vw - 24px));
		height: min(520px, 68dvh);
		display: flex;
		flex-direction: column;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg);
		box-shadow: var(--lift-shadow);
		overflow: hidden;
		padding: 0 10px 10px;
	}

	@media (max-width: 760px) {
		/* Same reasoning as the item page's .page-nav: hero chrome on a phone
		   needs a thumb-sized box, not a 30px one. */
		.omnia-pop-wrap:not(.inline) {
			top: 0;
			right: calc(var(--offset) - 6px);
		}
		.omnia-pop-wrap:not(.inline) .omnia-btn {
			width: 44px;
			height: 44px;
			opacity: 1;
		}
		.omnia-pop-wrap:not(.inline) .omnia-btn svg {
			width: 21px;
			height: 21px;
		}
		/* In the hub header row: same box as the history buttons beside it. */
		.omnia-pop-wrap.inline .omnia-btn {
			width: 44px;
			height: 44px;
		}
	}
</style>
