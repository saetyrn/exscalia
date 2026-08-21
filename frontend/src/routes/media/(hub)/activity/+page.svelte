<script lang="ts">
	import { untrack } from 'svelte';
	import { api, encodePath } from '$lib/api';
	import type { MediaEvent } from '$lib/api';
	import { completionVerb, dayNum, mergeDayBookends, monthAbbrev, yearOf } from '$lib/media/helpers';
	import StarDisplay from '$lib/media/StarDisplay.svelte';
	import Glyph from '$lib/Glyph.svelte';

	let { data } = $props();

	// The rating lives in its own column, so the activity cell stays a concise
	// verb — never a second copy of the stars.
	function activityLabel(event: MediaEvent & { bookend?: unknown }): string {
		if (event.kind === 'finished' && event.bookend) return completionVerb(event.media_type);
		switch (event.kind) {
			case 'started':
				return 'Started';
			case 'finished':
				return 'Finished';
			case 'watched':
				return 'Watched';
			case 'rated':
				return 'Rated';
			case 'progress':
				return event.value ?? event.text;
			case 'note':
				return event.text;
			default:
				return event.kind;
		}
	}

	let timeline: MediaEvent[] | null = $state(untrack(() => data.timeline));
	let q = $state('');

	async function load() {
		try {
			timeline = await api.mediaTimeline();
		} catch {
			timeline = null;
		}
	}

	$effect(() => {
		if (timeline === null) void load();
	});

	let liveRefresh: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const onLive = (e: Event) => {
			const detail = (e as CustomEvent).detail as { type?: string; path?: string };
			if (detail?.type !== 'note' && detail?.type !== 'note_removed') return;
			if (!detail.path?.startsWith('media/')) return;
			if (liveRefresh) clearTimeout(liveRefresh);
			liveRefresh = setTimeout(() => {
				liveRefresh = null;
				void load();
			}, 600);
		};
		window.addEventListener('exscalia:live', onLive);
		return () => {
			window.removeEventListener('exscalia:live', onLive);
			if (liveRefresh) clearTimeout(liveRefresh);
		};
	});

	// One row per log event, newest first, with a calendar chip shown only when
	// the month changes — the Letterboxd diary shape.
	let rows = $derived.by(() => {
		const needle = q.trim().toLowerCase();
		const events = mergeDayBookends(
			(timeline ?? []).filter((event) => !needle || event.title.toLowerCase().includes(needle))
		);
		let lastMonth = '';
		return events.map((event) => {
			const month = `${monthAbbrev(event.at)} ${yearOf(event.at)}`;
			const firstOfMonth = month !== lastMonth;
			lastMonth = month;
			return { event, firstOfMonth };
		});
	});
</script>

<div class="media-page">
	<h1 class="sr-only">Media activity</h1>

	<div class="topbar">
		<span class="diary-title">Diary</span>
		<input
			class="filter"
			type="search"
			placeholder="Filter…"
			aria-label="Filter activity by title"
			bind:value={q}
		/>
	</div>

	{#if timeline === null}
		<p class="state">Loading activity…</p>
	{:else if rows.length === 0}
		<p class="state">
			{q.trim()
				? `No logged activity matches “${q.trim()}”.`
				: 'No logged activity yet — progress and finishes will show up here.'}
		</p>
	{:else}
		<div class="diary" role="table" aria-label="Media diary">
			<div class="head" role="row">
				<span class="col-month" role="columnheader">Month</span>
				<span class="col-day" role="columnheader">Day</span>
				<span class="col-film" role="columnheader">Title</span>
				<span class="col-year" role="columnheader">Released</span>
				<span class="col-rating" role="columnheader">Rating</span>
				<span class="col-activity" role="columnheader">Activity</span>
			</div>
			{#each rows as { event, firstOfMonth }, index (index)}
				<a class="row" role="row" href={'/media/' + encodePath(event.path)}>
					<span class="col-month" role="cell">
						{#if firstOfMonth}
							<span class="cal" aria-hidden="true">
								<span class="cal-mon">{monthAbbrev(event.at)}</span>
								<span class="cal-year">{yearOf(event.at)}</span>
							</span>
						{/if}
					</span>
					<span class="col-day" role="cell">{dayNum(event.at)}</span>
					<span class="col-film" role="cell">
						<span class="cover" class:empty={!event.cover}>
							{#if event.cover}
								<img src={event.cover} alt="" loading="lazy" />
							{:else}
								<Glyph name={event.media_type} />
							{/if}
						</span>
						<span class="title">{event.title}</span>
					</span>
					<span class="col-year" role="cell">{event.year ?? ''}</span>
					<span class="col-rating" role="cell"><StarDisplay value={event.rating} /></span>
					<span class="col-activity" role="cell">{activityLabel(event)}</span>
				</a>
			{/each}
		</div>
	{/if}
</div>

<style>
	.media-page {
		padding: 14px 28px 48px;
		max-width: 1180px;
		margin: 0 auto;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
	}
	.topbar {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 10px;
	}
	.diary-title {
		font-size: 15px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.filter {
		margin-left: auto;
		width: min(260px, 40vw);
		height: 32px;
		padding: 0 12px;
		font-family: inherit;
		font-size: 13px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.filter:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.state {
		padding: 28px 10px;
		color: var(--text-muted);
		font-size: 12px;
		text-align: center;
	}

	/* Shared column grid — header and every row use the same template. */
	.head,
	.row {
		display: grid;
		grid-template-columns: 92px 46px minmax(0, 1fr) 84px 108px minmax(0, 190px);
		align-items: center;
		column-gap: 14px;
	}
	.head {
		padding: 0 8px 8px;
		font-size: 11px;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
	}
	.row {
		padding: 12px 8px;
		border-bottom: 1px solid var(--border);
		text-decoration: none;
		color: var(--text);
	}
	.row:hover {
		background: var(--bg-hover);
	}

	/* Calendar chip — a quiet mono take on the Letterboxd diary block. */
	.cal {
		display: inline-flex;
		flex-direction: column;
		width: 56px;
		border: 1px solid var(--border);
		border-radius: 6px;
		overflow: hidden;
		background: var(--bg-panel);
		text-align: center;
		line-height: 1;
	}
	.cal-mon {
		padding: 4px 0 3px;
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.06em;
		color: var(--text);
		background: color-mix(in srgb, #ffffff 6%, transparent);
		border-bottom: 1px solid var(--border);
	}
	.cal-year {
		padding: 3px 0 4px;
		font-size: 11px;
		color: var(--text-muted);
	}

	.col-day {
		font-size: 17px;
		font-weight: 500;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.col-film {
		display: flex;
		align-items: center;
		gap: 12px;
		min-width: 0;
	}
	.cover {
		flex: 0 0 32px;
		height: 48px;
		overflow: hidden;
		background: var(--bg-panel);
		border: 1px solid var(--border);
	}
	.cover img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.cover.empty {
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--text-muted);
	}
	.cover.empty :global(svg) {
		width: 15px;
		height: 15px;
	}
	.title {
		min-width: 0;
		font-size: 15px;
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.col-year {
		font-size: 13px;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.col-rating {
		--star-size: 15px;
	}
	.col-activity {
		font-size: 12px;
		color: var(--text-muted);
		text-align: right;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	@media (max-width: 760px) {
		.media-page {
			padding-inline: 14px;
		}
		.head,
		.row {
			grid-template-columns: 64px 34px minmax(0, 1fr) 96px;
			column-gap: 10px;
		}
		.col-year,
		.col-activity {
			display: none;
		}
		.cal {
			width: 48px;
		}
		.title {
			font-size: 14px;
		}
	}
</style>
