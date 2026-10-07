<script lang="ts">
	// Add a recipe, after the recipe app's Add.tsx: typed text or a link, and,
	// when the AI step can run, up to six photos or PDFs (the upload link, drag
	// and drop, or paste). "Review draft" reads them into a draft and slides
	// to the review form.
	import { onMount } from 'svelte';
	import Glyph from '$lib/Glyph.svelte';
	import CornerNav from '$lib/recipes/CornerNav.svelte';
	import StretchedTitle from '$lib/recipes/StretchedTitle.svelte';
	import { AddBox } from '$lib/recipes/form/addbox.svelte';
	import { forward } from '$lib/recipes/nav';
	import { pageTitle, RECIPES_HOME, RECIPES_REVIEW } from '$lib/recipes/paths';

	const box = new AddBox();
	let picker = $state<HTMLInputElement>();
	let input = $state<HTMLTextAreaElement>();
	let drag = $state(false);

	let lines = $derived(box.text.trim() ? box.text.trim().split('\n').length : 0);

	onMount(() => {
		void box.loadStatus();
		input?.focus();
		// A drag abandoned outside the window never sends dragleave here.
		const clear = () => (drag = false);
		window.addEventListener('dragend', clear);
		window.addEventListener('drop', clear);
		window.addEventListener('blur', clear);
		return () => {
			window.removeEventListener('dragend', clear);
			window.removeEventListener('drop', clear);
			window.removeEventListener('blur', clear);
		};
	});

	function onDragOver(e: DragEvent) {
		e.preventDefault();
		drag = true;
	}

	function onDragLeave(e: DragEvent) {
		const zone = e.currentTarget as HTMLElement;
		if (!zone.contains(e.relatedTarget as Node | null)) drag = false;
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		drag = false;
		const files = Array.from(e.dataTransfer?.files ?? []);
		if (files.length) void box.takeFiles(files);
		else {
			const text = e.dataTransfer?.getData('text') ?? '';
			if (text) box.appendText(text);
		}
	}

	function onPaste(e: ClipboardEvent) {
		const files = Array.from(e.clipboardData?.files ?? []);
		if (!files.length) return;
		e.preventDefault();
		void box.takeFiles(files);
	}

	function onPicked(e: Event) {
		const target = e.currentTarget as HTMLInputElement;
		const files = Array.from(target.files ?? []);
		target.value = '';
		if (files.length) void box.takeFiles(files);
	}

	async function submit() {
		if (await box.submit()) void forward(RECIPES_REVIEW);
	}
</script>

<svelte:head><title>{pageTitle('New recipe')}</title></svelte:head>

<div class="rc-page rc-form add">
	<CornerNav back={RECIPES_HOME} backLabel="Back to recipes" />
	<header class="add-head">
		<StretchedTitle text="New recipe" maxSize={60} stretch={1.9} align="center" />
	</header>

	<div
		class="drop"
		class:is-drag={drag}
		role="group"
		aria-label="Recipe source"
		ondragover={onDragOver}
		ondragleave={onDragLeave}
		ondrop={onDrop}
	>
		{#if box.uploads.length}
			<ul class="uploads" aria-label="Files to read">
				{#each box.uploads as upload (upload.key)}
					<li class="upload" class:is-doc={upload.kind === 'pdf'} title={upload.name}>
						{#if upload.thumb}
							<img src={upload.thumb} alt={upload.name} />
						{:else}
							<Glyph name="file" />
							<span class="upload-name">{upload.name}</span>
						{/if}
						<button
							type="button"
							class="upload-remove"
							aria-label={`Remove ${upload.name}`}
							title="Remove"
							onclick={() => box.remove(upload.key)}
						>
							<Glyph name="x" />
						</button>
					</li>
				{/each}
			</ul>
		{/if}
		<label class="rc-caption drop-label" for="add-source">
			{box.uploads.length ? 'Text' : 'Link or text'}
		</label>
		<textarea
			id="add-source"
			class="drop-input"
			class:short={box.uploads.length > 0}
			bind:this={input}
			bind:value={box.text}
			onpaste={onPaste}
			rows={box.uploads.length ? 3 : 9}
		></textarea>
		<div class="drop-foot">
			{#if box.ai}
				<button
					type="button"
					class="rc-link drop-attach"
					disabled={box.full || box.busy}
					onclick={() => picker?.click()}
				>
					<Glyph name="camera" />{box.uploads.length ? 'Add another page' : 'Upload a photo or PDF'}
				</button>
				<input
					bind:this={picker}
					type="file"
					accept="image/*,application/pdf,.pdf"
					multiple
					hidden
					onchange={onPicked}
				/>
			{/if}
			<span class="rc-caption drop-count">{lines ? `${lines} ${lines === 1 ? 'line' : 'lines'}` : ''}</span>
		</div>
	</div>

	{#if box.error}<p class="rc-error" role="alert">{box.error}</p>{/if}

	<div class="add-actions">
		<button type="button" class="rc-btn primary" disabled={!box.ready} onclick={submit}>
			{box.busy ? 'Reading…' : 'Review draft'}
		</button>
	</div>
</div>

<style>
	.add-head {
		margin-bottom: 30px;
		text-align: center;
	}

	/* One calm writing surface. */
	.drop {
		padding: 18px 20px 12px;
		border: 1px solid rgba(255, 255, 255, 0.09);
		border-radius: 18px;
		background: rgba(255, 255, 255, 0.025);
		transition:
			border-color 0.2s ease,
			background-color 0.2s ease;
	}
	.drop:focus-within {
		border-color: rgba(255, 255, 255, 0.18);
	}
	.drop.is-drag {
		border-color: rgba(169, 149, 238, 0.8);
		background: rgba(169, 149, 238, 0.06);
	}
	.drop-label {
		display: block;
		margin-bottom: 6px;
	}
	.add .drop-input,
	.add .drop-input:hover,
	.add .drop-input:focus {
		display: block;
		width: 100%;
		min-height: 38vh;
		padding: 0;
		border: 0;
		border-radius: 0;
		background: none;
		resize: none;
		font: 400 1rem/1.65 var(--rc-sans);
	}
	.add .drop-input.short {
		min-height: 5.5em;
	}
	.drop-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		min-height: 27px;
		margin-top: 10px;
		padding-top: 10px;
		border-top: 1px solid var(--rc-rule-faint);
	}
	.drop-count {
		margin-left: auto;
	}

	/* Photos and PDFs waiting to be read. */
	.uploads {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		margin: 0 0 14px;
		padding: 0;
		list-style: none;
	}
	.upload {
		position: relative;
		width: 84px;
		height: 84px;
		overflow: hidden;
		border: 1px solid rgba(255, 255, 255, 0.12);
		border-radius: 12px;
		background: rgba(255, 255, 255, 0.04);
	}
	.upload img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.upload.is-doc {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 8px;
		color: var(--rc-muted);
	}
	.upload.is-doc > :global(svg) {
		width: 26px;
		height: 26px;
		stroke-width: 1.4;
	}
	.upload-name {
		max-width: 100%;
		overflow: hidden;
		color: var(--rc-ink);
		font-size: 0.68rem;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.upload-remove {
		position: absolute;
		top: 4px;
		right: 4px;
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		min-height: 0;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: rgba(0, 0, 0, 0.6);
		color: #ffffff;
		cursor: pointer;
		opacity: 0;
		transition: opacity 0.15s ease;
	}
	.upload-remove :global(svg) {
		width: 13px;
		height: 13px;
		stroke-width: 1.6;
	}
	.upload:hover .upload-remove,
	.upload-remove:focus-visible {
		opacity: 1;
	}
	@media (hover: none) {
		.upload-remove {
			opacity: 1;
		}
	}

	.add-actions {
		display: flex;
		justify-content: flex-end;
		margin: 18px 0 48px;
	}

	@media (max-width: 760px) {
		.add-head {
			margin-bottom: 22px;
		}
		.drop {
			padding: 14px var(--m-gutter) 8px;
		}
		.add .drop-input,
		.add .drop-input:hover,
		.add .drop-input:focus {
			min-height: 32vh;
			font-size: var(--m-row-text);
		}
		.drop-foot {
			min-height: 44px;
		}
		/* The remove dot keeps its small look; its hit area is the corner. */
		.upload-remove::after {
			content: '';
			position: absolute;
			inset: -10px;
		}
		.add-actions .rc-btn {
			width: 100%;
			min-height: 48px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.drop,
		.upload-remove {
			transition: none;
		}
	}
</style>
