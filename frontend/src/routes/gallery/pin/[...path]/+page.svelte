<script lang="ts">
	// One picture, as Pinterest's pin page: the picture beside the owner's own
	// title, source, description and tags (each editable in place), and
	// pictures about the same thing below. OMNIA's reading of the picture is
	// never shown here; "More like this" refetches when it lands.
	import { untrack } from 'svelte';
	import { afterNavigate, goto } from '$app/navigation';
	import { api, apiFetch, ApiError } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { isGalleryEvent } from '$lib/gallery/drop';
	import Masonry from '$lib/gallery/Masonry.svelte';
	import { pinHref, pinsHref } from '$lib/gallery/paths';
	import type { Pin, PinCard, PinUpdate } from '$lib/gallery/types';
	import { createLiveList } from '$lib/media/livelist.svelte';
	import { modal } from '$lib/modal';
	import { omniaContext } from '$lib/stores.svelte';
	import { confirmDialog, notify } from '$lib/ui.svelte';

	let { data } = $props();

	// svelte-ignore state_referenced_locally
	const path: string = data.path;
	// svelte-ignore state_referenced_locally
	const live = createLiveList<Pin>(() => api.galleryPin(path), {
		match: (d) => d.path === path || d.new_path === path,
		initial: data.pin
	});
	$effect(() => live.listen());
	let pin = $derived(live.data);

	// While the picture is embedded or described, ask again now and then: a
	// failure writes nothing, so no live event would say it ended.
	$effect(() => {
		if (!pin?.status.working) return;
		const timer = setInterval(() => void live.load(), 3000);
		return () => clearInterval(timer);
	});

	const overview = createLiveList(() => api.galleryOverview(), { match: isGalleryEvent });
	$effect(() => {
		void overview.load();
		return overview.listen();
	});
	let boards = $derived((overview.data?.boards ?? []).map((b) => b.name));

	let related: PinCard[] = $state([]);
	$effect(() => {
		void pin?.described;
		void pin?.tags.length;
		untrack(() => {
			api
				.galleryRelated(path)
				.then((r) => (related = r.pins))
				.catch(() => {});
		});
	});

	$effect(() => {
		omniaContext.view = pin ? `Gallery picture${pin.title ? ` "${pin.title}"` : ''} (${pin.path})` : 'Gallery';
	});

	// Drafts follow the picture except while their field has the caret.
	let title = $state('');
	let description = $state('');
	let tagText = $state('');
	let editingTitle = false;
	let editingDescription = false;
	let editingTags = $state(false);
	$effect(() => {
		const current = pin;
		if (!current) return;
		untrack(() => {
			if (!editingTitle) title = current.title;
			if (!editingDescription) description = current.description;
			if (!editingTags) tagText = current.tags.join(', ');
		});
	});

	let cameFromGallery = false;
	afterNavigate(({ from }) => {
		if (from?.url.pathname.startsWith('/gallery')) cameFromGallery = true;
	});
	function back() {
		if (cameFromGallery) history.back();
		else void goto(pinsHref(pin?.board || null));
	}

	async function save(update: PinUpdate) {
		const current = pin;
		if (!current) return;
		try {
			const saved = await api.galleryUpdate(current.path, update, current.revision);
			if (saved.path !== current.path) {
				await goto(pinHref(saved.path), { replaceState: true });
				return;
			}
			live.set(saved);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
			if (e instanceof ApiError && e.status === 409) void live.load();
		}
	}

	function saveTitle() {
		editingTitle = false;
		if (pin && title.trim() !== pin.title) void save({ title: title.trim() });
	}
	function saveDescription() {
		editingDescription = false;
		if (pin && description.trim() !== pin.description) void save({ description });
	}
	function saveTags() {
		editingTags = false;
		const tags = tagText
			.split(',')
			.map((t) => t.trim())
			.filter(Boolean);
		if (pin && tags.join(',') !== pin.tags.join(',')) void save({ tags });
	}

	async function describeAgain() {
		if (!pin) return;
		try {
			await api.galleryDescribe(pin.path);
			await live.load();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function remove() {
		if (!pin) return;
		const ok = await confirmDialog('The picture goes to the Trash.', {
			title: 'Delete picture',
			confirmLabel: 'Delete',
			danger: true
		});
		if (!ok) return;
		try {
			await api.galleryDelete(pin.path);
			void goto(pinsHref(pin.board || null), { replaceState: true });
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	/** Through fetch rather than a download link: the desktop app cannot
	 * follow an /api/ link, and the name should be the title. */
	async function download() {
		if (!pin) return;
		try {
			const res = await apiFetch(pin.image, { timeoutMs: 120_000 });
			if (!res.ok) throw new Error(`Download failed (${res.status})`);
			const url = URL.createObjectURL(await res.blob());
			const a = document.createElement('a');
			const ext = pin.image.split('.').pop() ?? 'jpg';
			a.href = url;
			a.download = `${(pin.title || 'picture').replace(/[\\/:*?"<>|]+/g, ' ').trim() || 'picture'}.${ext}`;
			a.click();
			setTimeout(() => URL.revokeObjectURL(url), 10_000);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	/** Fits the textarea to its text; re-run whenever `_value` changes. */
	function grow(node: HTMLTextAreaElement, _value: string) {
		const fit = () => {
			node.style.height = 'auto';
			node.style.height = `${node.scrollHeight}px`;
		};
		fit();
		node.addEventListener('input', fit);
		return {
			update: fit,
			destroy: () => node.removeEventListener('input', fit)
		};
	}

	let viewing = $state(false);
</script>

<svelte:head><title>{pin?.title || 'Picture'} · Gallery</title></svelte:head>

<div class="page">
	{#if pin}
		<div class="card">
			<button type="button" class="back" title="Back" aria-label="Back" onclick={back}>
				<Glyph name="back" />
			</button>
			<div class="media">
				<button type="button" class="picture" title="View" aria-label="View the picture" onclick={() => (viewing = true)}>
					<img
						src={pin.image}
						alt={pin.title}
						style:aspect-ratio={pin.width && pin.height ? `${pin.width} / ${pin.height}` : undefined}
						style:background-color={pin.color ?? 'var(--bg-panel)'}
					/>
				</button>
			</div>
			<div class="side">
				<div class="tools">
					<button type="button" class="glyph" title="Download" aria-label="Download" onclick={() => void download()}>
						<Glyph name="download" />
					</button>
					<button type="button" class="glyph" title="View" aria-label="View" onclick={() => (viewing = true)}>
						<Glyph name="expand" />
					</button>
					<button
						type="button"
						class="glyph"
						title="Describe again"
						aria-label="Describe again"
						disabled={pin.status.working}
						onclick={() => void describeAgain()}
					>
						<Glyph name="redraw" />
					</button>
					<button type="button" class="glyph" title="Delete" aria-label="Delete" onclick={() => void remove()}>
						<Glyph name="trash" />
					</button>
					<select
						class="board"
						title="Board"
						aria-label="Board"
						value={pin.board}
						onchange={(e) => void save({ board: (e.currentTarget as HTMLSelectElement).value })}
					>
						<option value="">No board</option>
						{#each boards.includes(pin.board) || !pin.board ? boards : [pin.board, ...boards] as name (name)}
							<option value={name}>{name}</option>
						{/each}
					</select>
				</div>

				<input
					class="pin-title"
					type="text"
					placeholder="Title"
					aria-label="Title"
					maxlength="200"
					bind:value={title}
					onfocus={() => (editingTitle = true)}
					onblur={saveTitle}
					onkeydown={(e) => {
						if (e.key === 'Enter') (e.currentTarget as HTMLInputElement).blur();
					}}
				/>

				{#if pin.source_url}
					<a class="visit" href={pin.source_url} target="_blank" rel="noopener noreferrer">Visit site</a>
				{/if}

				<h2>Description</h2>
				<textarea
					class="description"
					aria-label="Description"
					rows="2"
					placeholder="Description"
					bind:value={description}
					use:grow={description}
					onfocus={() => (editingDescription = true)}
					onblur={saveDescription}
				></textarea>
				{#if pin.status.error}
					<p class="error">
						{pin.status.error}
						<button type="button" class="retry" onclick={() => void describeAgain()}>Retry</button>
					</p>
				{/if}

				<div class="tags">
					{#if editingTags}
						<!-- svelte-ignore a11y_autofocus -->
						<input
							class="tag-input"
							type="text"
							aria-label="Tags, separated by commas"
							bind:value={tagText}
							autofocus
							onblur={saveTags}
							onkeydown={(e) => {
								if (e.key === 'Enter') (e.currentTarget as HTMLInputElement).blur();
								if (e.key === 'Escape') {
									e.preventDefault();
									tagText = pin?.tags.join(', ') ?? '';
									editingTags = false;
								}
							}}
						/>
					{:else}
						{#each pin.tags as tag (tag)}
							<a class="tag" href={pinsHref(null, tag)}>{tag}</a>
						{/each}
						<button type="button" class="tag-edit" title="Edit tags" aria-label="Edit tags" onclick={() => (editingTags = true)}>
							<Glyph name="edit" />
						</button>
					{/if}
				</div>
			</div>
		</div>

		{#if related.length}
			<h2 class="more">More like this</h2>
			<Masonry pins={related} />
		{/if}
	{:else}
		<p class="quiet">{data.error || 'This picture is gone.'}</p>
	{/if}
</div>

{#if viewing && pin}
	<div
		class="viewer"
		role="dialog"
		aria-modal="true"
		aria-label="Picture"
		tabindex="-1"
		use:modal={{ onEscape: () => (viewing = false) }}
		onclick={() => (viewing = false)}
		onkeydown={() => {}}
	>
		<img src={pin.image} alt={pin.title} />
	</div>
{/if}

<style>
	.page {
		box-sizing: border-box;
		padding: 24px 32px 48px;
	}
	.card {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(300px, 1fr);
		gap: 28px;
		max-width: 1080px;
		margin: 0 auto;
		padding: 20px 20px 20px 64px;
		border-radius: 28px;
		background: var(--bg);
		box-shadow: 0 1px 18px color-mix(in srgb, var(--text) 10%, transparent);
	}
	.back {
		position: absolute;
		top: 18px;
		left: 12px;
		width: 44px;
		height: 44px;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 50%;
		background: transparent;
		color: var(--text);
		cursor: pointer;
	}
	.back:hover,
	.glyph:hover {
		background: var(--bg-hover);
	}
	.back :global(svg),
	.glyph :global(svg) {
		width: 20px;
		height: 20px;
	}
	.media {
		display: flex;
		justify-content: center;
		align-items: flex-start;
	}
	.picture {
		display: block;
		padding: 0;
		border: none;
		background: none;
		cursor: zoom-in;
		border-radius: 20px;
	}
	.picture:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
	}
	.picture img {
		display: block;
		max-width: 100%;
		max-height: 80vh;
		border-radius: 20px;
		object-fit: contain;
	}
	.side {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.tools {
		display: flex;
		align-items: center;
		gap: 2px;
		margin-bottom: 14px;
	}
	.glyph {
		width: 44px;
		height: 44px;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 50%;
		background: transparent;
		color: var(--text);
		cursor: pointer;
		transition: background var(--motion-fast) var(--ease-out);
	}
	.glyph:disabled {
		opacity: 0.35;
		cursor: default;
	}
	.glyph:focus-visible,
	.board:focus-visible,
	.visit:focus-visible,
	.tag:focus-visible {
		outline: none;
		box-shadow: var(--focus-ring);
	}
	.board {
		margin-left: auto;
		max-width: 46%;
		height: 40px;
		padding: 0 10px;
		border: none;
		border-radius: 20px;
		background: var(--bg-panel);
		color: var(--text);
		font: inherit;
		font-size: 15px;
		font-weight: 600;
		cursor: pointer;
	}
	.board:hover {
		background: var(--bg-hover);
	}
	.pin-title {
		width: 100%;
		margin: 0 0 14px;
		padding: 2px 0;
		border: none;
		border-bottom: 1px solid transparent;
		background: transparent;
		color: var(--text);
		font: inherit;
		font-size: 30px;
		font-weight: 700;
		letter-spacing: -0.5px;
		outline: none;
	}
	.pin-title:hover {
		border-bottom-color: var(--border);
	}
	.pin-title:focus {
		border-bottom-color: var(--text-muted);
	}
	.visit {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 48px;
		margin-bottom: 18px;
		border-radius: 24px;
		background: var(--bg-panel);
		color: var(--text);
		font-size: 16px;
		font-weight: 600;
		text-decoration: none;
		transition: background var(--motion-fast) var(--ease-out);
	}
	.visit:hover {
		background: var(--bg-hover);
	}
	h2 {
		margin: 0 0 6px;
		font-size: 16px;
		font-weight: 700;
	}
	.description {
		width: 100%;
		min-height: 2.6em;
		padding: 4px 0;
		border: none;
		border-bottom: 1px solid transparent;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: 15px;
		line-height: 1.5;
		resize: none;
		overflow: hidden;
		outline: none;
	}
	.description:hover {
		border-bottom-color: var(--border);
	}
	.description:focus {
		color: var(--text);
		border-bottom-color: var(--text-muted);
	}
	.quiet {
		margin: 0;
		color: var(--text-muted);
		font-size: 14px;
	}
	.error {
		margin: 6px 0 0;
		color: var(--danger);
		font-size: 13px;
	}
	.retry {
		border: none;
		background: none;
		color: var(--text);
		font: inherit;
		text-decoration: underline;
		cursor: pointer;
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin-top: 14px;
	}
	.tag {
		padding: 5px 11px;
		border-radius: 14px;
		background: var(--bg-panel);
		color: var(--text);
		font-size: 13px;
		text-decoration: none;
		transition: background var(--motion-fast) var(--ease-out);
	}
	.tag:hover {
		background: var(--bg-hover);
	}
	.tag-edit {
		width: 30px;
		height: 30px;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 50%;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.tag-edit:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.tag-edit :global(svg) {
		width: 14px;
		height: 14px;
	}
	.tag-input {
		flex: 1;
		height: 36px;
		padding: 0 10px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 14px;
	}
	.tag-input:focus-visible {
		outline: none;
		border-color: var(--accent);
		box-shadow: var(--focus-ring);
	}
	.more {
		max-width: 1080px;
		margin: 40px auto 16px;
		font-size: 20px;
		text-align: center;
	}
	.viewer {
		position: fixed;
		inset: 0;
		z-index: var(--z-dialog);
		overflow: auto;
		display: grid;
		place-items: center;
		padding: 24px;
		background: color-mix(in srgb, #000 88%, transparent);
		cursor: zoom-out;
	}
	.viewer img {
		max-width: 100%;
	}
	@media (max-width: 760px) {
		.page {
			padding: 8px 0 32px;
		}
		.card {
			grid-template-columns: 1fr;
			gap: 14px;
			padding: 0 var(--m-gutter) 16px;
			border-radius: 0;
			box-shadow: none;
		}
		.back {
			position: static;
		}
		.media {
			margin: 0 calc(-1 * var(--m-gutter));
		}
		.picture,
		.picture img {
			border-radius: 0;
			width: 100%;
		}
		.pin-title {
			font-size: var(--m-title);
		}
		.description,
		.board {
			font-size: var(--m-row-text);
		}
		.tag {
			font-size: var(--m-meta);
			padding: 11px 14px;
			border-radius: 22px;
		}
		.tag-edit {
			width: 44px;
			height: 44px;
		}
		.more {
			margin: 28px var(--m-gutter) 12px;
			font-size: 18px;
		}
		.page :global(.masonry) {
			padding: 0 var(--m-gutter);
			box-sizing: border-box;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.glyph,
		.visit,
		.tag {
			transition: none;
		}
	}
</style>
