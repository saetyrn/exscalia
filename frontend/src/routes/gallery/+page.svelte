<script lang="ts">
	// The Gallery's boards: Pinterest's board page in the app's own look. All
	// pictures first, then every board, the one saved to most recently first.
	import { api } from '$lib/api';
	import AddDialog from '$lib/gallery/AddDialog.svelte';
	import BoardCard from '$lib/gallery/BoardCard.svelte';
	import BoardDialog from '$lib/gallery/BoardDialog.svelte';
	import { newBoard } from '$lib/gallery/boards';
	import { isGalleryEvent, pictureDrop } from '$lib/gallery/drop';
	import GalleryHeader from '$lib/gallery/GalleryHeader.svelte';
	import { pinsHref } from '$lib/gallery/paths';
	import { sendPictures } from '$lib/gallery/uploads.svelte';
	import UploadLine from '$lib/gallery/UploadLine.svelte';
	import type { Board } from '$lib/gallery/types';
	import { createLiveList } from '$lib/media/livelist.svelte';
	import { omniaContext } from '$lib/stores.svelte';
	import { notify } from '$lib/ui.svelte';

	let { data } = $props();

	// svelte-ignore state_referenced_locally
	const overview = createLiveList(() => api.galleryOverview(), {
		match: isGalleryEvent,
		initial: data.overview
	});
	$effect(() => overview.listen());
	$effect(() => {
		omniaContext.view = 'Gallery · Boards';
	});

	let adding = $state(false);
	let editing: Board | null = $state(null);
	let boards = $derived(overview.data?.boards ?? []);

	async function drop(files: File[]) {
		const errors = await sendPictures(files, '', () => {});
		if (errors.length) notify(errors.slice(0, 3).join('\n'), { tone: 'error' });
	}

	async function createBoard() {
		const name = await newBoard();
		if (name !== null) await overview.load();
	}
</script>

<svelte:head><title>Gallery</title></svelte:head>

<div class="page" use:pictureDrop={{ onfiles: (files) => void drop(files) }}>
	<GalleryHeader active="boards" onadd={() => (adding = true)} onnewboard={() => void createBoard()} />
	<UploadLine />

	{#if overview.data}
		<div class="boards">
			<BoardCard
				name="All pictures"
				href={pinsHref()}
				count={overview.data.total}
				covers={overview.data.covers}
			/>
			{#each boards as board (board.name)}
				<BoardCard
					name={board.name}
					href={pinsHref(board.name)}
					count={board.count}
					updated={board.updated}
					covers={board.covers}
					onedit={() => (editing = board)}
				/>
			{/each}
		</div>
	{:else if overview.failed}
		<p class="quiet">
			The Gallery did not load. <button type="button" class="retry" onclick={() => void overview.load()}>Retry</button>
		</p>
	{/if}
</div>

{#if adding}
	<AddDialog
		boards={boards.map((b) => b.name)}
		onclose={() => (adding = false)}
		onsaved={(_pin, created) => {
			if (!created) notify('Already on that board');
		}}
	/>
{/if}
{#if editing}
	<BoardDialog
		name={editing.name}
		count={editing.count}
		onclose={() => (editing = null)}
		ondone={() => void overview.load()}
	/>
{/if}

<style>
	.page {
		box-sizing: border-box;
		min-height: 100%;
		padding: 28px 32px 48px;
		border-radius: 18px;
		outline: 2px dashed transparent;
		outline-offset: -10px;
		transition: outline-color var(--motion-fast) var(--ease-out);
	}
	.page:global(.dropping) {
		outline-color: var(--text-muted);
	}
	.boards {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
		gap: 28px 18px;
	}
	.quiet {
		color: var(--text-muted);
		font-size: 14px;
	}
	.retry {
		border: none;
		background: none;
		color: var(--text);
		font: inherit;
		text-decoration: underline;
		cursor: pointer;
	}
	@media (max-width: 760px) {
		.page {
			padding: 14px var(--m-gutter) 32px;
		}
		.boards {
			grid-template-columns: repeat(2, minmax(0, 1fr));
			gap: 20px 12px;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.page {
			transition: none;
		}
	}
</style>
