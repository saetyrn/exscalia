<script lang="ts">
	import { goto } from '$app/navigation';
	import { untrack } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from 'svelte/transition';
	import { api, type CardGrade, type Deck, type Flashcard } from '$lib/api';
	import { renderNote } from '$lib/render';
	import { notify } from '$lib/ui.svelte';

	let { data } = $props();

	const deck = untrack(() => data.deck) as Deck | null;
	const initialOrder = untrack(() => data.order);

	// Grades are keyboard-first: 1–4 left to right, hardest to easiest.
	const GRADES: { id: CardGrade; label: string; key: string; hint: string }[] = [
		{ id: 'again', label: 'Again', key: '1', hint: 'Missed it — show again this session' },
		{ id: 'hard', label: 'Hard', key: '2', hint: 'Recalled with difficulty' },
		{ id: 'good', label: 'Good', key: '3', hint: 'Recalled correctly' },
		{ id: 'easy', label: 'Easy', key: '4', hint: 'Instant — push it far out' }
	];

	const ORDERS: { id: string; label: string }[] = [
		{ id: 'due', label: 'Due' },
		{ id: 'least_known', label: 'Least known' },
		{ id: 'most_known', label: 'Most known' }
	];

	let cards = $state<Flashcard[]>(deck?.cards ?? []);
	let queue = $state<string[]>(deck?.queue ?? []);
	let position = $state(0);
	let revealed = $state(false);
	let order = $state(initialOrder);
	let view = $state<'study' | 'browse'>('study');
	let reviewed = $state(0);
	let grading = $state(false);
	let sessionCounts = $state<Record<CardGrade, number>>({ again: 0, hard: 0, good: 0, easy: 0 });

	let byId = $derived(new Map(cards.map((card) => [card.id, card])));
	let current = $derived<Flashcard | null>(byId.get(queue[position] ?? '') ?? null);
	let finished = $derived(position >= queue.length);
	let stats = $derived.by(() => {
		const counts = { new: 0, learning: 0, known: 0 };
		for (const card of cards) {
			if (!card.reps) counts.new += 1;
			else if (card.interval >= 21) counts.known += 1;
			else counts.learning += 1;
		}
		return counts;
	});

	// Cards are markdown so formulas render — the whole point for a stats or
	// accounting deck. renderNote sanitizes.
	function render(text: string): string {
		return renderNote(text);
	}

	async function grade(value: CardGrade) {
		if (!current || grading || !deck) return;
		grading = true;
		const card = current;
		try {
			const updated = await api.reviewCard(deck.id, card.id, value);
			cards = cards.map((item) => (item.id === updated.id ? updated : item));
			sessionCounts[value] += 1;
			reviewed += 1;
			// "Again" means the card comes back before the session ends.
			if (value === 'again') queue = [...queue, card.id];
			position += 1;
			revealed = false;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			grading = false;
		}
	}

	async function restart(nextOrder = order) {
		if (!deck) return;
		try {
			const fresh = await api.getDeck(deck.id, nextOrder);
			cards = fresh.cards;
			queue = fresh.queue;
			order = nextOrder;
			position = 0;
			revealed = false;
			reviewed = 0;
			sessionCounts = { again: 0, hard: 0, good: 0, easy: 0 };
			view = 'study';
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	function exit() {
		goto('/');
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.metaKey || event.ctrlKey || event.altKey) return;
		const target = event.target as HTMLElement | null;
		if (target && /^(INPUT|TEXTAREA)$/.test(target.tagName)) return;

		if (event.key === 'Escape') {
			event.preventDefault();
			exit();
			return;
		}
		if (view !== 'study' || finished) return;
		if (event.key === ' ' || event.key === 'Enter') {
			event.preventDefault();
			revealed = true;
			return;
		}
		if (revealed) {
			const match = GRADES.find((g) => g.key === event.key);
			if (match) {
				event.preventDefault();
				void grade(match.id);
			}
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

<svelte:head><title>{deck ? `${deck.name} — study` : 'Study'}</title></svelte:head>

<div class="study">
	{#if !deck}
		<div class="empty">
			<h1>Deck not found</h1>
			<p>{data.error ?? 'Ask OMNIA to pull up one of your decks.'}</p>
			<button class="primary" onclick={exit}>Back to notes</button>
		</div>
	{:else}
		<header>
			<div class="titles">
				<h1>{deck.name}</h1>
				<p class="counts">
					<span>{stats.new} new</span>
					<span>{stats.learning} learning</span>
					<span>{stats.known} known</span>
				</p>
			</div>
			<div class="tools">
				<div class="orders" role="radiogroup" aria-label="Study order">
					{#each ORDERS as option (option.id)}
						<button
							class:selected={order === option.id}
							onclick={() => restart(option.id)}
							title={option.id === 'due'
								? 'Spaced repetition: only cards scheduled for today'
								: option.id === 'least_known'
									? 'Weakest cards first'
									: 'Strongest cards first'}
						>
							{option.label}
						</button>
					{/each}
				</div>
				<button onclick={() => (view = view === 'study' ? 'browse' : 'study')}>
					{view === 'study' ? 'Browse' : 'Study'}
				</button>
				<button onclick={exit} title="Close study mode (Esc)" aria-label="Close study mode">✕</button>
			</div>
		</header>

		{#if view === 'browse'}
			<div class="browse">
				{#each cards as card (card.id)}
					<article>
						<!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized in render.ts -->
						<div class="side front">{@html render(card.front)}</div>
						<!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized in render.ts -->
						<div class="side back">{@html render(card.back)}</div>
						<footer>
							<span class="chip">{!card.reps ? 'new' : card.interval >= 21 ? 'known' : 'learning'}</span>
							{#if card.reps}<span>{card.reps} reviews</span>{/if}
							{#if card.lapses}<span>{card.lapses} lapses</span>{/if}
						</footer>
					</article>
				{/each}
			</div>
		{:else if queue.length === 0}
			<div class="empty">
				<h2>Nothing due right now</h2>
				<p>Every card in this deck is scheduled for later.</p>
				<div class="empty-actions">
					<button class="primary" onclick={() => restart('least_known')}>
						Drill least known
					</button>
					<button onclick={() => (view = 'browse')}>Browse cards</button>
				</div>
			</div>
		{:else if finished}
			<div class="empty" in:fly={{ y: 8, duration: 200, easing: cubicOut }}>
				<h2>Session complete</h2>
				<p>{reviewed} card{reviewed === 1 ? '' : 's'} reviewed.</p>
				<ul class="summary">
					{#each GRADES as g (g.id)}
						<li><strong>{sessionCounts[g.id]}</strong> {g.label}</li>
					{/each}
				</ul>
				<div class="empty-actions">
					<button class="primary" onclick={() => restart()}>Study again</button>
					<button onclick={exit}>Done</button>
				</div>
			</div>
		{:else}
			<div class="progress" aria-hidden="true">
				<span style:width="{(position / queue.length) * 100}%"></span>
			</div>
			<p class="position">{position + 1} of {queue.length}</p>

			{#key current?.id}
				<button
					class="card"
					type="button"
					onclick={() => (revealed = true)}
					in:fade={{ duration: 140 }}
					aria-label={revealed ? 'Card answer' : 'Show answer'}
				>
					<!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized in render.ts -->
					<div class="face">{@html render(current?.front ?? '')}</div>
					{#if revealed}
						<div class="divider" in:fade={{ duration: 120 }}></div>
						<!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized in render.ts -->
						<div class="face answer" in:fly={{ y: 6, duration: 160, easing: cubicOut }}>
							{@html render(current?.back ?? '')}
						</div>
					{/if}
				</button>
			{/key}

			<div class="controls">
				{#if revealed}
					<div class="grades" in:fade={{ duration: 120 }}>
						{#each GRADES as g (g.id)}
							<button class="grade {g.id}" disabled={grading} title={g.hint} onclick={() => grade(g.id)}>
								{g.label}<span class="key">{g.key}</span>
							</button>
						{/each}
					</div>
				{:else}
					<button class="primary reveal" onclick={() => (revealed = true)}>
						Show answer<span class="key">Space</span>
					</button>
				{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	/* Full-screen takeover: the sidebar and note panel are hidden by the layout
	   while this route is active. Everything is theme-variable driven so all
	   palettes, including custom ones, work. */
	.study {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		min-height: 0;
		padding: 18px 20px calc(28px + env(safe-area-inset-bottom));
		background: var(--bg);
		overflow-y: auto;
	}
	header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		width: 100%;
		max-width: 860px;
		margin-bottom: 18px;
	}
	h1 {
		margin: 0;
		font-size: 18px;
	}
	h2 {
		margin: 0 0 6px;
		font-size: 19px;
	}
	.counts {
		display: flex;
		gap: 10px;
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 12px;
	}
	.tools {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
	}
	.orders {
		display: flex;
		gap: 2px;
	}
	.orders button {
		font-size: 12px;
		padding: 4px 10px;
		color: var(--text-muted);
		border: 1px solid transparent;
	}
	.orders button.selected {
		border-color: var(--accent);
		background: var(--accent-soft);
		color: var(--accent);
	}
	.progress {
		width: 100%;
		max-width: 860px;
		height: 3px;
		border-radius: 2px;
		background: var(--bg-hover);
		overflow: hidden;
	}
	.progress span {
		display: block;
		height: 100%;
		background: var(--accent);
		transition: width var(--motion-base) var(--ease-out);
	}
	.position {
		width: 100%;
		max-width: 860px;
		margin: 6px 0 0;
		color: var(--text-muted);
		font-size: 12px;
	}
	.card {
		display: flex;
		flex-direction: column;
		justify-content: center;
		gap: 14px;
		width: 100%;
		max-width: 860px;
		min-height: 46vh;
		margin: 10px 0 18px;
		padding: 32px 34px;
		border: 1px solid var(--border);
		border-radius: 14px;
		background: var(--bg-panel);
		color: var(--text);
		text-align: center;
		font-size: 15px;
		cursor: pointer;
		box-shadow: var(--lift-shadow);
	}
	.card:hover {
		border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
	}
	.face {
		font-size: 19px;
		line-height: 1.5;
	}
	.face.answer {
		font-size: 16px;
		color: var(--text-muted);
	}
	.face :global(p) {
		margin: 0 0 0.5em;
	}
	.face :global(p:last-child) {
		margin-bottom: 0;
	}
	.face :global(img) {
		max-width: 100%;
		border-radius: 8px;
	}
	.face :global(table) {
		margin: 0 auto;
		border-collapse: collapse;
		font-size: 0.9em;
	}
	.face :global(th),
	.face :global(td) {
		padding: 4px 9px;
		border: 1px solid var(--border);
	}
	.divider {
		height: 1px;
		background: var(--border);
	}
	.controls {
		width: 100%;
		max-width: 860px;
		display: flex;
		justify-content: center;
	}
	.grades {
		display: grid;
		grid-template-columns: repeat(4, minmax(0, 1fr));
		gap: 8px;
		width: 100%;
	}
	.grade {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 7px;
		padding: 11px 8px;
		border: 1px solid var(--border);
		font-size: 13.5px;
	}
	.grade.again:hover {
		border-color: var(--danger);
		color: var(--danger);
	}
	.grade.easy:hover,
	.grade.good:hover {
		border-color: var(--added);
		color: var(--added);
	}
	.grade.hard:hover {
		border-color: var(--accent);
		color: var(--accent);
	}
	.reveal {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 11px 26px;
		font-size: 13.5px;
	}
	.key {
		padding: 1px 6px;
		border-radius: 4px;
		background: color-mix(in srgb, var(--text) 12%, transparent);
		font-family: var(--font-mono);
		font-size: 11px;
		opacity: 0.75;
	}
	.empty {
		margin: auto;
		max-width: 460px;
		text-align: center;
		color: var(--text-muted);
	}
	.empty h1,
	.empty h2 {
		color: var(--text);
	}
	.empty-actions {
		display: flex;
		justify-content: center;
		gap: 8px;
		margin-top: 16px;
	}
	.summary {
		display: flex;
		justify-content: center;
		gap: 16px;
		margin: 14px 0 0;
		padding: 0;
		list-style: none;
		font-size: 13px;
	}
	.summary strong {
		color: var(--text);
	}
	.browse {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
		gap: 10px;
		width: 100%;
		max-width: 860px;
	}
	.browse article {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg-panel);
		font-size: 13.5px;
	}
	.browse .back {
		color: var(--text-muted);
		padding-top: 8px;
		border-top: 1px solid var(--border);
	}
	.browse footer {
		display: flex;
		gap: 8px;
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.chip {
		padding: 1px 7px;
		border-radius: 999px;
		background: var(--bg-hover);
	}
	.side :global(p) {
		margin: 0;
	}

	@media (max-width: 760px) {
		.study {
			padding: 12px 12px calc(20px + env(safe-area-inset-bottom));
		}
		header {
			flex-direction: column;
			gap: 10px;
		}
		.card {
			min-height: 38vh;
			padding: 22px 18px;
		}
		.face {
			font-size: 17px;
		}
		.grades {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
		.grade {
			min-height: 46px;
		}
		.key {
			display: none;
		}
	}
</style>
