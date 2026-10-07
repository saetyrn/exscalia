<script lang="ts">
	// One recipe, after the recipe app's Recipe screen: one centred column with
	// the drawing, the title in the stretched serif (double-click to rename),
	// time and tags as one caption line, then the servings row directly above
	// the ingredients it scales, the steps with their references scaled in
	// place, the notes, and where the recipe came from.
	//
	// The recipe is read from the cache, so a refresh behind the page (the live
	// listener after an OMNIA edit or a finished drawing) shows without a
	// reload. Every save sends the whole record without its drawing, from the
	// revision on screen; a 409 means the note changed elsewhere, and the page
	// reloads it rather than overwrite it.
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, ApiError } from '$lib/api';
	import { renderMarkdown } from '$lib/render';
	import AsciiArt from '$lib/recipes/AsciiArt.svelte';
	import CornerButton from '$lib/recipes/CornerButton.svelte';
	import CornerNav from '$lib/recipes/CornerNav.svelte';
	import EditableTitle from '$lib/recipes/EditableTitle.svelte';
	import IngredientList from '$lib/recipes/IngredientList.svelte';
	import ServingsSlider from '$lib/recipes/ServingsSlider.svelte';
	import StepList from '$lib/recipes/StepList.svelte';
	import { cachedRecipe, loadRecipe, recipeGone, rememberRecipe } from '$lib/recipes/cache.svelte';
	import { recipesChrome } from '$lib/recipes/chrome.svelte';
	import { plainKey } from '$lib/recipes/keys';
	import { ordinaryNoteHref } from '$lib/recipes/links';
	import { forward } from '$lib/recipes/nav';
	import { pageTitle, RECIPES_HOME, recipeEditHref } from '$lib/recipes/paths';
	import {
		confirmIngredient,
		fitServings,
		localThumbnail,
		metaLine,
		recipeInput,
		savedServings,
		saveServings,
		sourceLink
	} from '$lib/recipes/recipepage';
	import type { Recipe, RecipeIn } from '$lib/recipes/types';

	let { data } = $props();

	// Deleted or moved away while open: the loader's copy is dead too.
	let recipe = $derived(recipeGone(data.path) ? null : (cachedRecipe(data.path) ?? data.recipe));

	// The loader answers null for any failure. Ask once more, to tell a path
	// that is not a recipe from a load that failed. A note in the folder that
	// is not a recipe opens in the editor.
	let missing = $state(false);
	let loadError = $state('');
	$effect(() => {
		if (recipe) return;
		const path = data.path;
		untrack(() => {
			loadRecipe(path).catch(async (e) => {
				if (e instanceof ApiError && e.status === 404) {
					const note = await ordinaryNoteHref(path);
					if (note && !recipe) void goto(note, { replaceState: true });
					else missing = true;
				} else loadError = (e as Error).message || 'Could not load the recipe';
			});
		});
	});

	// The servings the cook picked, kept for the tab; the recipe's own until then.
	let chosen = $state<number | null>(savedServings(untrack(() => data.path)));
	let servings = $derived(recipe ? fitServings(chosen, recipe.base_servings) : 1);
	let factor = $derived(recipe ? servings / recipe.base_servings : 1);

	function changeServings(n: number) {
		chosen = n;
		saveServings(data.path, n);
	}

	// Estimates being confirmed show as confirmed while the save runs.
	let confirming = $state<string[]>([]);
	let ingredients = $derived(
		recipe
			? confirming.reduce((list, id) => confirmIngredient(list, id), recipe.ingredients)
			: []
	);

	let meta = $derived(recipe ? metaLine(recipe) : '');
	let thumbnail = $derived(recipe && !recipe.art ? localThumbnail(recipe.source?.thumbnail) : null);
	let source = $derived(sourceLink(recipe?.source?.url));
	// Notes come from the owner or a website: rendered with OMNIA's rules, so
	// raw HTML shows as text and a remote image is a link, not a request.
	let notesHtml = $derived(
		recipe?.notes.trim() ? renderMarkdown(recipe.notes, { chat: true, headingOffset: 2 }) : ''
	);

	// A short line under the title: a conflict, or a save that failed.
	let notice = $state<{ text: string; error: boolean } | null>(null);

	// Saves run one after another, each from the newest copy, so two quick
	// confirms do not trip over each other's revision.
	let queue: Promise<unknown> = Promise.resolve();

	function save(change: (current: Recipe) => RecipeIn): Promise<Recipe> {
		const run = queue.then(async () => {
			const current = cachedRecipe(data.path) ?? data.recipe;
			if (!current) throw new Error('Not loaded');
			try {
				const saved = rememberRecipe(
					await api.recipesUpdate(current.path, change(current), current.revision)
				);
				notice = null;
				return saved;
			} catch (e) {
				if (e instanceof ApiError && e.status === 409) {
					notice = { text: 'Changed elsewhere. Reloaded.', error: false };
					await loadRecipe(current.path).catch(() => {});
				} else {
					notice = { text: (e as Error).message || 'Not saved', error: true };
				}
				throw e;
			}
		});
		queue = run.catch(() => {});
		return run;
	}

	async function rename(title: string): Promise<void> {
		await save((current) => ({ ...recipeInput(current), title }));
	}

	function confirm(id: string) {
		if (confirming.includes(id)) return;
		confirming = [...confirming, id];
		save((current) => ({
			...recipeInput(current),
			ingredients: confirmIngredient(current.ingredients, id)
		}))
			.catch(() => {
				// The notice says why; the marker is back.
			})
			.finally(() => {
				confirming = confirming.filter((x) => x !== id);
			});
	}

	// E edits. Left and Escape are CornerNav's; K is the layout's.
	function onkeydown(e: KeyboardEvent) {
		if (!recipe || !plainKey(e) || recipesChrome.omniaOpen) return;
		if (e.key.toLowerCase() !== 'e' || e.shiftKey || e.repeat) return;
		e.preventDefault();
		void forward(recipeEditHref(recipe.path));
	}
</script>

<svelte:window {onkeydown} />

<svelte:head>
	<title>{pageTitle(recipe?.title ?? (missing ? 'Not found' : null))}</title>
</svelte:head>

<div class="rc-page">
	<CornerNav
		back={RECIPES_HOME}
		backLabel="Back to recipes"
		omnia={recipe ? recipe.path : false}
		label="Recipe actions"
	>
		{#if recipe}
			<CornerButton glyph="edit" label="Edit" shortcut="E" href={recipeEditHref(recipe.path)} />
		{/if}
	</CornerNav>

	{#if recipe}
		<article class="recipe">
			<header class="head">
				{#if thumbnail}
					<img class="lens" src={thumbnail} alt="" />
				{/if}
				<AsciiArt
					path={recipe.path}
					title={recipe.title}
					art={recipe.art}
					colors={recipe.art_colors}
					palette={recipe.art_palette}
				/>
				<EditableTitle text={recipe.title} onrename={rename} />
				{#if meta}
					<p class="meta rc-caption">{meta}</p>
				{/if}
			</header>

			<!-- Always here, and without height while it is empty: a live region
			     speaks when its text changes, not when it arrives holding it. -->
			<p class="notice rc-small" class:rc-error={notice?.error} role="status">{notice?.text ?? ''}</p>

			{#if recipe.ingredients.length > 0}
				<ServingsSlider base={recipe.base_servings} value={servings} onchange={changeServings} />
				<IngredientList {ingredients} {factor} onconfirm={confirm} />
			{/if}

			<StepList steps={recipe.steps} {ingredients} {factor} />

			{#if notesHtml}
				<section>
					<h2 class="rc-caption">Notes</h2>
					<!-- eslint-disable-next-line svelte/no-at-html-tags — renderMarkdown sanitizes -->
					<div class="notes">{@html notesHtml}</div>
				</section>
			{/if}

			{#if source}
				<p class="source rc-muted rc-small">
					From <a href={source.href} target="_blank" rel="noopener noreferrer">{source.host}</a>
				</p>
			{/if}
		</article>
	{:else if missing}
		<p class="rc-empty">Recipe not found</p>
	{:else if loadError}
		<p class="rc-error rc-state">{loadError}</p>
	{/if}
</div>

<style>
	.head {
		text-align: center;
	}
	/* The source's photo, set in a round lens, until there is a drawing. */
	.lens {
		display: block;
		width: 150px;
		aspect-ratio: 1;
		margin: 0 auto 24px;
		border: 1px solid rgba(255, 255, 255, 0.18);
		border-radius: 50%;
		object-fit: cover;
		box-shadow: 0 18px 60px rgba(0, 0, 0, 0.6);
	}
	.meta {
		margin-top: 12px;
	}
	.notice {
		margin: 16px 0 0;
		text-align: center;
		color: var(--rc-muted);
	}
	.notice:empty {
		margin: 0;
	}
	.notice.rc-error {
		color: var(--rc-danger);
	}
	.notes {
		font-family: var(--rc-serif);
		font-size: 1.05rem;
		font-style: italic;
		letter-spacing: -0.01em;
		overflow-wrap: anywhere;
	}
	.notes :global(:is(p, ul, ol, blockquote, pre, table)) {
		margin: 0 0 0.8em;
	}
	.notes :global(:is(ul, ol)) {
		padding-left: 1.3em;
	}
	.notes :global(:is(h3, h4, h5, h6)) {
		margin: 1.2em 0 0.4em;
		font: 600 var(--rc-label) / 1.5 var(--rc-sans);
		letter-spacing: var(--rc-label-track);
		text-transform: uppercase;
		color: var(--rc-muted);
	}
	.notes :global(a) {
		color: var(--rc-ink);
		text-decoration-color: rgba(255, 255, 255, 0.3);
		text-underline-offset: 0.2em;
	}
	.notes :global(img) {
		max-width: 100%;
		height: auto;
	}
	.notes :global(:is(code, pre)) {
		font-family: var(--rc-mono);
		font-size: 0.9em;
		font-style: normal;
	}
	.source {
		margin-top: 28px;
	}
	.source a {
		color: var(--rc-ink);
		text-decoration-color: rgba(255, 255, 255, 0.3);
		text-underline-offset: 0.2em;
	}

	@media (max-width: 760px) {
		.notes {
			font-size: var(--rc-text);
		}
		.source a {
			display: inline-flex;
			align-items: center;
			min-height: 44px;
		}
	}
</style>
