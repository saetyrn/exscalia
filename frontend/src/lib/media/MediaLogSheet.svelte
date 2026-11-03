<script lang="ts">
	// The phone's answer to the desktop rail's quick-log: the same POST, laid
	// out as a form you can fill with a thumb. One field per type, a date that
	// starts at today, and a Finished toggle that brings the stars with it so
	// "I finished it, four and a half" is one submit rather than three taps in
	// three different controls.
	import { untrack } from 'svelte';
	import BottomSheet from '$lib/BottomSheet.svelte';
	import type { MediaItem, MediaType } from '$lib/api';
	import StarRating from '$lib/media/StarRating.svelte';
	import {
		buildLogRequest,
		emptyDraft,
		type LogEventLite,
		type LogRequest
	} from '$lib/media/mediaactions';

	let {
		title,
		mediaType,
		item,
		events,
		busy,
		onsubmit,
		onclose
	}: {
		title: string;
		mediaType: MediaType;
		item: MediaItem | null;
		events: LogEventLite[];
		busy: boolean;
		/** Resolves true when the entry landed; false leaves the sheet open so
		 * the typed value survives the error the caller already surfaced. */
		onsubmit: (body: LogRequest) => Promise<boolean>;
		onclose: () => void;
	} = $props();

	// Seeded once, on purpose: the sheet is mounted per opening, and a live
	// note event repainting `item` mid-edit must not overwrite what is typed.
	let draft = $state(untrack(() => emptyDraft(mediaType, item, events)));
	let error = $state('');

	let field = $derived(
		mediaType === 'book'
			? { label: 'Current page', placeholder: 'e.g. 120', inputmode: 'numeric' as const }
			: { label: 'Episode', placeholder: 'S02E05', inputmode: 'text' as const }
	);

	// A film has no half-way point, so its only entry is the watch itself.
	let verb = $derived(mediaType === 'movie' ? 'Log watch' : 'Log');

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (busy) return;
		const built = buildLogRequest(mediaType, draft);
		if ('error' in built) {
			error = built.error;
			return;
		}
		error = '';
		if (await onsubmit(built.body)) onclose();
	}
</script>

<BottomSheet {title} label="Log progress" fit {onclose}>
	<form class="log-form" onsubmit={submit}>
		{#if !draft.finished && (mediaType === 'book' || mediaType === 'tv')}
			<label class="row">
				<span class="row-label">{field.label}</span>
				<input
					class="row-input"
					type="text"
					inputmode={field.inputmode}
					placeholder={field.placeholder}
					aria-label={field.label}
					bind:value={draft.value}
					disabled={busy}
					onfocus={(e) => e.currentTarget.select()}
				/>
			</label>
		{:else if !draft.finished && mediaType === 'game'}
			<label class="row stack">
				<span class="row-label">Session</span>
				<textarea
					class="row-area"
					rows="3"
					placeholder="What you played…"
					aria-label="Session note"
					bind:value={draft.note}
					disabled={busy}
				></textarea>
			</label>
		{/if}

		<label class="row">
			<span class="row-label">Date</span>
			<input
				class="row-input"
				type="date"
				aria-label="Date"
				bind:value={draft.date}
				disabled={busy}
			/>
		</label>

		{#if mediaType !== 'movie'}
			<label class="row check">
				<input type="checkbox" bind:checked={draft.finished} disabled={busy} />
				<span class="row-label">Finished</span>
			</label>
		{/if}

		{#if draft.finished || mediaType === 'movie'}
			<div class="row stars">
				<span class="row-label">Rating</span>
				<StarRating
					value={draft.rating ?? item?.rating ?? null}
					disabled={busy}
					onrate={(rating) => (draft.rating = draft.rating === rating ? null : rating)}
				/>
			</div>
		{/if}

		{#if error}<p class="error">{error}</p>{/if}

		<button class="go" type="submit" disabled={busy}>
			{busy ? '…' : draft.finished ? 'Log finish' : verb}
		</button>
	</form>
</BottomSheet>

<style>
	.log-form {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 4px 8px 12px;
		overflow-y: auto;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 12px;
		min-height: 48px;
	}
	.row.stack {
		align-items: flex-start;
		flex-direction: column;
		gap: 6px;
	}
	.row-label {
		flex: none;
		min-width: 96px;
		font-size: 13px;
		color: var(--text-muted);
	}
	.row-input {
		flex: 1;
		min-width: 0;
		height: 44px;
		padding: 0 12px;
		font-family: inherit;
		font-size: var(--m-row-text);
		color: var(--text);
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: var(--radius);
	}
	.row-input:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.row-area {
		width: 100%;
		padding: 10px 12px;
		font-family: inherit;
		font-size: var(--m-row-text);
		color: var(--text);
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		resize: vertical;
	}
	.row-area:focus {
		outline: none;
		border-color: color-mix(in srgb, var(--accent) 60%, var(--border));
	}
	.row.check {
		gap: 12px;
	}
	.row.check input {
		width: 22px;
		height: 22px;
		flex: none;
		accent-color: var(--accent);
	}
	.row.check .row-label {
		min-width: 0;
		color: var(--text);
		font-size: 15px;
	}
	/* Big enough to hit a half star with a thumb. */
	.row.stars {
		--star-size: 40px;
		flex-wrap: wrap;
	}
	.error {
		margin: 0;
		font-size: 13px;
		color: var(--danger, #e5484d);
	}
	.go {
		margin-top: 6px;
		height: 50px;
		font-family: inherit;
		font-size: 15px;
		font-weight: 600;
		color: var(--text);
		background: var(--bg-panel);
		border: 1px solid color-mix(in srgb, var(--accent) 55%, var(--border));
		border-radius: var(--radius);
		cursor: pointer;
	}
	.go:active {
		background: var(--bg-hover);
	}
	.go:disabled {
		opacity: 0.55;
		cursor: default;
	}
</style>
