<script lang="ts">
	import { cubicOut } from 'svelte/easing';
	import { slide } from 'svelte/transition';
	import type { SuggestionCategory } from './proofread';

	/** A suggestion with everything the panel needs to draw it. The note page
	 * builds these from the editor's live anchors, so `context` and `stale`
	 * follow the document as it is edited. */
	export interface Card {
		id: string;
		category: SuggestionCategory;
		before: string;
		after: string;
		reason: string;
		stale: boolean;
		pre: string;
		post: string;
	}

	let {
		cards,
		activeId,
		onSelect,
		onAccept,
		onDismiss,
		onClear
	}: {
		cards: Card[];
		activeId: string | null;
		onSelect: (id: string) => void;
		onAccept: (id: string) => void;
		onDismiss: (id: string) => void;
		onClear: () => void;
	} = $props();

	const LABELS: Record<SuggestionCategory, string> = {
		spelling: 'Spelling',
		grammar: 'Grammar',
		clarity: 'Clarity'
	};

	let index = $derived(Math.max(0, cards.findIndex((c) => c.id === activeId)));
	let active = $derived(cards[index] ?? cards[0] ?? null);
	let counts = $derived.by(() => {
		const out: Partial<Record<SuggestionCategory, number>> = {};
		for (const card of cards) out[card.category] = (out[card.category] ?? 0) + 1;
		return out;
	});

	function step(by: number) {
		if (cards.length === 0) return;
		const next = (index + by + cards.length) % cards.length;
		onSelect(cards[next].id);
	}
</script>

<div class="proofread">
	{#if cards.length === 0}
		<p class="done">Nothing left to review.</p>
	{:else}
		{#if active}
			<article class="card" class:stale={active.stale}>
				<header>
					<span class="dot {active.category}" aria-hidden="true"></span>
					<span class="cat">{LABELS[active.category]}</span>
					{#if active.reason}<span class="why">{active.reason}</span>{/if}
				</header>
				<p class="context">
					{#if active.pre}<span class="dim">{active.pre}</span>{/if}<span class="was"
						>{active.before}</span
					><span class="now">{active.after}</span>{#if active.post}<span class="dim"
							>{active.post}</span
						>{/if}
				</p>
				{#if active.stale}
					<p class="warn">This text changed since the review, so it can't be applied.</p>
				{/if}
				<div class="row">
					<button class="primary" disabled={active.stale} onclick={() => onAccept(active.id)}>
						Accept
					</button>
					<button onclick={() => onDismiss(active.id)}>Dismiss</button>
					<span class="nav">
						<button
							class="step"
							title="Previous suggestion"
							aria-label="Previous suggestion"
							onclick={() => step(-1)}>‹</button
						>
						<span class="pos">{index + 1}/{cards.length}</span>
						<button
							class="step"
							title="Next suggestion"
							aria-label="Next suggestion"
							onclick={() => step(1)}>›</button
						>
					</span>
				</div>
			</article>
		{/if}

		<ul class="rest">
			{#each cards as card (card.id)}
				{#if card.id !== active?.id}
					<li transition:slide={{ duration: 140, easing: cubicOut }}>
						<button class="mini" class:stale={card.stale} onclick={() => onSelect(card.id)}>
							<span class="dot {card.category}" aria-hidden="true"></span>
							<span class="mini-word">{card.before.trim() || '(blank)'}</span>
							<span class="mini-why">{card.reason || LABELS[card.category]}</span>
						</button>
					</li>
				{/if}
			{/each}
		</ul>

		<footer>
			<span class="tally">
				{#each Object.entries(counts) as [name, n] (name)}
					<span class="chip"
						><span class="dot {name}" aria-hidden="true"></span>{n}
						{LABELS[name as SuggestionCategory]}</span
					>
				{/each}
			</span>
			<button class="clear" onclick={onClear}>Dismiss all</button>
		</footer>
	{/if}
</div>

<style>
	.proofread {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		gap: 10px;
		padding: 12px;
	}
	.done {
		color: var(--text-muted);
		font-size: 13px;
		margin: 0;
	}
	.card {
		border: 1px solid var(--accent);
		border-radius: 10px;
		padding: 12px;
		background: var(--bg-panel);
	}
	.card.stale {
		border-color: var(--border);
		opacity: 0.75;
	}
	header {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 12px;
		margin-bottom: 8px;
	}
	.cat {
		font-weight: 600;
		color: var(--text);
	}
	.why {
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	/* Colour carries the category here, so it is information rather than
	   decoration — the same hues as the underlines in the editor. */
	.dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		flex-shrink: 0;
		display: inline-block;
	}
	.dot.spelling,
	.dot.grammar {
		background: var(--danger);
	}
	.dot.clarity {
		background: var(--accent);
	}
	.context {
		margin: 0 0 10px;
		font-size: 13px;
		line-height: 1.55;
		word-break: break-word;
	}
	.dim {
		color: var(--text-muted);
	}
	.was {
		text-decoration: line-through;
		color: var(--danger);
	}
	.now {
		color: var(--accent);
		font-weight: 600;
	}
	.warn {
		margin: 0 0 10px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.row {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	button {
		font: inherit;
		font-size: 12px;
		padding: 5px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--bg);
		color: var(--text);
		cursor: pointer;
	}
	button:hover:not(:disabled) {
		border-color: var(--accent);
	}
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.primary {
		background: var(--accent);
		border-color: var(--accent);
		color: #fff;
		font-weight: 600;
	}
	.nav {
		margin-left: auto;
		display: flex;
		align-items: center;
		gap: 2px;
	}
	.step {
		padding: 2px 7px;
		font-size: 14px;
		line-height: 1;
	}
	.pos {
		font-size: 11px;
		color: var(--text-muted);
		min-width: 30px;
		text-align: center;
	}
	.rest {
		list-style: none;
		margin: 0;
		padding: 0;
		overflow-y: auto;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.mini {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 8px;
		text-align: left;
		padding: 8px 10px;
		border-radius: 8px;
	}
	.mini.stale {
		opacity: 0.55;
	}
	.mini-word {
		font-weight: 600;
		flex-shrink: 0;
		max-width: 45%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.mini-why {
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	footer {
		display: flex;
		align-items: center;
		gap: 8px;
		border-top: 1px solid var(--border);
		padding-top: 9px;
		flex-wrap: wrap;
	}
	.tally {
		display: flex;
		gap: 10px;
		flex-wrap: wrap;
	}
	.chip {
		display: flex;
		align-items: center;
		gap: 5px;
		font-size: 11px;
		color: var(--text-muted);
	}
	.clear {
		margin-left: auto;
		font-size: 11px;
		padding: 4px 8px;
	}
</style>
