<script lang="ts">
	import { api } from '$lib/api';
	import type { MediaItem, MediaStatus, MediaType } from '$lib/api';
	import StarRating from '$lib/media/StarRating.svelte';
	import { notify } from '$lib/ui.svelte';

	let { path, meta } = $props<{ path: string; meta: Record<string, unknown> }>();

	// The last mutation's returned item is shown until fresh server meta
	// arrives through the editor's merge path, then it steps aside.
	let item: MediaItem | null = $state(null);
	let busy = $state(false);
	let logValue = $state('');

	$effect(() => {
		void meta;
		item = null;
	});

	const str = (value: unknown) => (value == null ? null : String(value));
	const num = (value: unknown) =>
		typeof value === 'number' && Number.isFinite(value) ? value : null;

	// $derived.by closures: at the top level TS narrows `item` to its `null`
	// initializer and flags the accesses; inside a function the union holds.
	let type = $derived.by(() => (item?.media_type ?? str(meta.media) ?? 'book') as MediaType);
	let status = $derived.by(
		() => (item?.status ?? str(meta.media_status) ?? 'planned') as MediaStatus
	);
	let rating = $derived.by(() => (item ? item.rating : num(meta.rating)));
	let cover = $derived.by(() => (item ? item.cover : str(meta.cover)));
	let creator = $derived.by(() => (item ? item.creator : str(meta.creator)));
	let year = $derived.by(() => (item ? item.year : num(meta.year)));
	let total = $derived.by(() => (item ? item.total : num(meta.total)));
	let started = $derived.by(() => (item ? item.started : str(meta.started)));
	let finished = $derived.by(() => (item ? item.finished : str(meta.finished)));

	const TYPE_LABEL: Record<MediaType, string> = {
		book: 'Book',
		movie: 'Movie',
		tv: 'TV show',
		game: 'Game'
	};

	function statusLabel(value: MediaStatus): string {
		// Planned IS the watchlist — one concept, one word, everywhere.
		if (value === 'planned') return 'Watchlist';
		if (value === 'active') {
			if (type === 'book') return 'Reading';
			if (type === 'game') return 'Playing';
			return 'Watching';
		}
		return value.charAt(0).toUpperCase() + value.slice(1);
	}

	let facts = $derived(
		[
			creator && { label: type === 'book' ? 'Author' : type === 'game' ? 'Studio' : 'Creator', value: creator },
			year != null && { label: 'Year', value: String(year) },
			total != null && {
				label: type === 'book' ? 'Pages' : 'Episodes',
				value: String(total)
			},
			started && { label: 'Started', value: started },
			finished && { label: 'Finished', value: finished }
		].filter(Boolean) as { label: string; value: string }[]
	);

	async function patch(body: { rating?: number; status?: MediaStatus }) {
		if (busy) return;
		busy = true;
		try {
			const result = await api.mediaPatch(path, body);
			if (result.item) item = result.item;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	async function log(event: SubmitEvent) {
		event.preventDefault();
		if (busy) return;
		const value = logValue.trim();
		// No date field: a quick log means "today", and the server stamps it
		// in the app timezone. Backdating stays possible through OMNIA.
		let body: Parameters<typeof api.mediaLog>[1];
		if (type === 'movie') {
			body = { kind: 'watched' };
		} else if (type === 'game') {
			if (!value) {
				notify('Write a short note about the session.', { tone: 'error' });
				return;
			}
			body = { kind: 'note', text: value };
		} else if (type === 'book') {
			if (!/^\d+$/.test(value) || Number(value) < 1) {
				notify('Enter your current page.', { tone: 'error' });
				return;
			}
			body = { kind: 'progress', value };
		} else {
			if (!/^s?\d{1,2}\s*e\d{1,3}$/i.test(value) && !/^\d+$/.test(value)) {
				notify('Enter an episode like S02E05 (or a number).', { tone: 'error' });
				return;
			}
			body = { kind: 'progress', value };
		}
		busy = true;
		try {
			const result = await api.mediaLog(path, body);
			if (result.item) item = result.item;
			logValue = '';
			notify(result.summary + '.');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}
</script>

<div class="media-panel">
	<div class="mp-head">
		<div class="mp-cover">
			{#if cover}
				<img src={cover} alt="" />
			{:else}
				<span>{TYPE_LABEL[type]}</span>
			{/if}
		</div>
		<dl class="mp-facts">
			<div><dt>Type</dt><dd>{TYPE_LABEL[type]}</dd></div>
			{#each facts as fact (fact.label)}
				<div><dt>{fact.label}</dt><dd>{fact.value}</dd></div>
			{/each}
		</dl>
	</div>

	<section>
		<h3>Rating</h3>
		<div class="mp-rating">
			<StarRating
				value={rating}
				disabled={busy}
				onrate={(value) => void patch({ rating: value })}
			/>
		</div>
	</section>

	<section>
		<h3>Status</h3>
		<select
			aria-label="Status"
			value={status}
			disabled={busy}
			onchange={(event) =>
				void patch({ status: (event.currentTarget as HTMLSelectElement).value as MediaStatus })}
		>
			{#each ['planned', 'active', 'finished', 'dropped'] as const as option (option)}
				<option value={option}>{statusLabel(option)}</option>
			{/each}
		</select>
	</section>

	<section>
		<h3>Log</h3>
		<form onsubmit={log}>
			{#if type !== 'movie'}
				<input
					bind:value={logValue}
					placeholder={type === 'book' ? 'Current page' : type === 'tv' ? 'S02E05' : 'Session note…'}
					aria-label={type === 'book' ? 'Current page' : type === 'tv' ? 'Episode reached' : 'Session note'}
					inputmode={type === 'book' ? 'numeric' : undefined}
					disabled={busy}
				/>
			{/if}
			<button class="primary" type="submit" disabled={busy}>
				{type === 'movie' ? 'Log watch' : 'Log'}
			</button>
		</form>
	</section>
</div>

<style>
	.media-panel {
		display: flex;
		flex-direction: column;
		gap: 14px;
		min-height: 0;
		overflow-y: auto;
		padding: 14px;
	}
	.mp-head {
		display: flex;
		gap: 12px;
	}
	.mp-cover {
		flex: 0 0 84px;
		aspect-ratio: 2 / 3;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-panel);
	}
	.mp-cover img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.mp-cover span {
		font-size: 11px;
		color: var(--text-muted);
	}
	.mp-facts {
		flex: 1;
		min-width: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 12px;
	}
	.mp-facts div {
		display: flex;
		gap: 6px;
		min-width: 0;
	}
	.mp-facts dt {
		flex: 0 0 58px;
		color: var(--text-muted);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.4px;
		text-transform: uppercase;
		line-height: 1.7;
	}
	.mp-facts dd {
		margin: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	section h3 {
		margin: 0 0 6px;
		color: var(--text-muted);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.4px;
		text-transform: uppercase;
	}
	.mp-rating {
		--star-size: 22px;
	}
	select {
		width: 100%;
		height: 32px;
		font-family: inherit;
		font-size: 12px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 0 8px;
	}
	form {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 6px;
	}
	form input {
		min-width: 0;
		height: 32px;
	}
	form button {
		height: 32px;
		padding-inline: 11px;
	}
</style>
