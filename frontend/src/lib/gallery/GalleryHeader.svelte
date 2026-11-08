<script lang="ts">
	// The top of every Gallery grid page: the title, Pictures and Boards, a
	// search field and the two add glyphs. Search lands on /gallery/pins.
	import { goto } from '$app/navigation';
	import Glyph from '$lib/Glyph.svelte';
	import { GALLERY_HOME, pinsHref } from '$lib/gallery/paths';

	let {
		active,
		query = '',
		board = null,
		onadd,
		onnewboard
	}: {
		active: 'pictures' | 'boards';
		query?: string;
		/** The board a search stays inside, null for every board. */
		board?: string | null;
		onadd: () => void;
		onnewboard: () => void;
	} = $props();

	// svelte-ignore state_referenced_locally
	let text = $state(query);
	$effect(() => {
		text = query;
	});

	function search(event: SubmitEvent) {
		event.preventDefault();
		void goto(pinsHref(board, text), { keepFocus: true, noScroll: true });
	}
</script>

<header class="head">
	<h1>Gallery</h1>
	<form class="search" role="search" onsubmit={search}>
		<Glyph name="search" />
		<input type="search" placeholder="Search" aria-label="Search the Gallery" bind:value={text} />
	</form>
	<div class="actions">
		<button type="button" class="glyph" title="New board" aria-label="New board" onclick={onnewboard}>
			<Glyph name="folder" />
		</button>
		<button type="button" class="glyph add" title="Add pictures" aria-label="Add pictures" onclick={onadd}>
			<Glyph name="plus" />
		</button>
	</div>
</header>
<nav class="tabs" aria-label="Gallery views">
	<a href={pinsHref()} class:active={active === 'pictures'} aria-current={active === 'pictures' ? 'page' : undefined}>Pictures</a>
	<a href={GALLERY_HOME} class:active={active === 'boards'} aria-current={active === 'boards' ? 'page' : undefined}>Boards</a>
</nav>

<style>
	.head {
		display: flex;
		align-items: center;
		gap: 16px;
	}
	h1 {
		margin: 0;
		font-size: 34px;
		font-weight: 700;
		letter-spacing: -0.6px;
	}
	.search {
		flex: 1;
		max-width: 520px;
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 8px;
		height: 44px;
		padding: 0 14px;
		border-radius: 22px;
		background: var(--bg-panel);
		color: var(--text-muted);
		transition: background var(--motion-fast) var(--ease-out), box-shadow var(--motion-fast) var(--ease-out);
	}
	.search:hover {
		background: var(--bg-hover);
	}
	.search:focus-within {
		box-shadow: var(--focus-ring);
	}
	.search :global(svg) {
		width: 17px;
		height: 17px;
		flex: none;
	}
	.search input {
		flex: 1;
		min-width: 0;
		border: none;
		background: transparent;
		color: var(--text);
		font: inherit;
		font-size: 15px;
		outline: none;
	}
	.actions {
		display: flex;
		gap: 6px;
	}
	.glyph {
		width: 44px;
		height: 44px;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 50%;
		background: var(--bg-panel);
		color: var(--text);
		cursor: pointer;
		transition: background var(--motion-fast) var(--ease-out);
	}
	.glyph:hover {
		background: var(--bg-hover);
	}
	.glyph.add {
		background: var(--text);
		color: var(--bg);
	}
	.glyph.add:hover {
		background: color-mix(in srgb, var(--text) 82%, var(--bg));
	}
	.glyph:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
	}
	.glyph :global(svg) {
		width: 19px;
		height: 19px;
	}
	.tabs {
		display: flex;
		gap: 22px;
		margin: 18px 0 22px;
	}
	.tabs a {
		padding: 6px 2px;
		color: var(--text);
		font-size: 16px;
		font-weight: 600;
		text-decoration: none;
		border-bottom: 3px solid transparent;
		transition: border-color var(--motion-fast) var(--ease-out);
	}
	.tabs a:hover {
		border-bottom-color: var(--border);
	}
	.tabs a.active {
		border-bottom-color: var(--text);
	}
	.tabs a:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
		border-radius: 4px;
	}
	@media (max-width: 760px) {
		.head {
			flex-wrap: wrap;
			gap: 10px;
		}
		h1 {
			font-size: var(--m-title);
			letter-spacing: -0.2px;
		}
		.search {
			order: 3;
			flex-basis: 100%;
			max-width: none;
			margin-left: 0;
		}
		.search input {
			font-size: var(--m-row-text);
		}
		.actions {
			margin-left: auto;
		}
		.tabs {
			margin: 12px 0 14px;
		}
		.tabs a {
			font-size: var(--m-row-text);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.search,
		.glyph,
		.tabs a {
			transition: none;
		}
	}
</style>
