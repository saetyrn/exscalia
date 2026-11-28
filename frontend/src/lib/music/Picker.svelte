<!--
	Choose a folder or a playlist from a filterable list: Move to…, Add to
	folder…, Add to playlist…
-->
<script lang="ts">
	import { modal } from '$lib/modal';

	let {
		title,
		options,
		onpick,
		onclose,
		oncreate
	}: {
		title: string;
		options: { id: string; label: string }[];
		onpick: (id: string) => void;
		onclose: () => void;
		/** Offer "New …" with the typed name when nothing matches it. */
		oncreate?: (name: string) => void;
	} = $props();

	let query = $state('');
	let input: HTMLInputElement | undefined = $state();
	// Act first, then close: closing clears the parent's state these
	// callbacks come from.
	function choose(id: string) {
		onpick(id);
		onclose();
	}
	function create() {
		oncreate?.(query.trim());
		onclose();
	}

	const shown = $derived(
		options.filter((o) => o.label.toLowerCase().includes(query.trim().toLowerCase()))
	);
</script>

<div class="backdrop" role="presentation" onpointerdown={(e) => e.target === e.currentTarget && onclose()}>
	<div class="sheet" role="dialog" aria-label={title} use:modal={{ onEscape: onclose, initialFocus: () => input }}>
		<h2>{title}</h2>
		<input bind:this={input} bind:value={query} placeholder="Filter" aria-label="Filter" />
		<ul>
			{#each shown as option (option.id)}
				<li>
					<button type="button" onclick={() => choose(option.id)}>{option.label}</button>
				</li>
			{/each}
			{#if oncreate && query.trim() && !options.some((o) => o.label.toLowerCase() === query.trim().toLowerCase())}
				<li>
					<button type="button" class="new" onclick={create}>
						New: {query.trim()}
					</button>
				</li>
			{/if}
		</ul>
	</div>
</div>

<style>
	.backdrop {
		position: fixed;
		inset: 0;
		z-index: var(--z-dialog);
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.3);
	}
	.sheet {
		width: min(440px, calc(100vw - 32px));
		max-height: min(560px, calc(100dvh - 64px));
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 18px;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 14px;
		box-shadow: 0 20px 48px rgba(0, 0, 0, 0.22);
	}
	h2 {
		margin: 0;
		font-size: 16px;
	}
	input {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 16px;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		overflow-y: auto;
	}
	button {
		width: 100%;
		padding: 9px 10px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--text);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	button:hover,
	button:focus-visible {
		background: var(--bg-hover);
		outline: none;
	}
	.new {
		color: var(--text-muted);
	}
	@media (max-width: 760px) {
		button {
			min-height: 44px;
		}
	}
</style>
