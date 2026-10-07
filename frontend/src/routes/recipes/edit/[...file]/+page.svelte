<script lang="ts">
	// Edit a recipe, after the recipe app's Review.tsx in its "edit" mode. The
	// URL is /recipes/edit/ plus the whole vault path. The form opens on the
	// cached copy when there is one and takes the server's fresh one if it
	// arrives before you change anything. Save updates from the revision the
	// form loaded and never overwrites: a recipe changed elsewhere meanwhile
	// is answered 409, and the form offers to reload it (a drawing landing
	// meanwhile is no such change: the form follows it, and saveEdit goes
	// again past one it has not seen yet). The update carries no art, so the
	// drawing is kept. Delete moves the note to the Trash.
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, ApiError } from '$lib/api';
	import { cachedRecipe, forgetRecipe, loadRecipe } from '$lib/recipes/cache.svelte';
	import CornerNav from '$lib/recipes/CornerNav.svelte';
	import { RecipeFormModel } from '$lib/recipes/form/form.svelte';
	import { fromRecipe } from '$lib/recipes/form/model';
	import RecipeForm from '$lib/recipes/form/RecipeForm.svelte';
	import { followStored, saveEdit } from '$lib/recipes/form/save';
	import { back, slideTo } from '$lib/recipes/nav';
	import { editPathFromParam, pageTitle, RECIPES_HOME, recipeHref } from '$lib/recipes/paths';
	import type { Recipe, RecipeIn } from '$lib/recipes/types';
	import { afterPathsRemoved, refreshNotes } from '$lib/stores.svelte';
	import { confirmDialog, notify } from '$lib/ui.svelte';

	// The layout remounts the page on every pathname change, so the path is
	// fixed for this component's life.
	const path = editPathFromParam(page.params.file ?? '');
	const cached = cachedRecipe(path);

	/** The stored recipe the form was loaded from: its revision is the base
	 * of the update. */
	let base = $state<Recipe | null>(cached ?? null);
	let model = $state<RecipeFormModel | null>(cached ? new RecipeFormModel(fromRecipe(cached)) : null);
	let loadError = $state('');
	let missing = $state(false);
	let busy = $state(false);
	let message = $state('');
	let conflict = $state(false);

	/** Take the stored recipe as it moves on behind the form (followStored). */
	function follow(fresh: Recipe) {
		if (!model || !base) {
			model = new RecipeFormModel(fromRecipe(fresh));
			base = fresh;
			return;
		}
		const step = followStored(base, fresh, model.pristine);
		if (step === 'load') model.load(fromRecipe(fresh));
		if (step === 'load' || step === 'adopt') {
			base = fresh;
			conflict = false;
		}
	}

	loadRecipe(path).then(follow, (cause: Error) => {
		if (model) return;
		if (cause instanceof ApiError && cause.status === 404) missing = true;
		else loadError = cause.message;
	});

	// The live listener keeps the cache in step with the vault: a drawing
	// landing, OMNIA or another device saving.
	$effect(() => {
		const fresh = cachedRecipe(path);
		if (fresh) untrack(() => follow(fresh));
	});

	async function save(recipe: RecipeIn) {
		if (!base) return;
		busy = true;
		message = '';
		conflict = false;
		try {
			await saveEdit(path, recipe, base);
			await back(recipeHref(path));
		} catch (cause) {
			if (cause instanceof ApiError && cause.status === 409) conflict = true;
			else if (cause instanceof ApiError && cause.status === 422) model?.refused(cause.message);
			else message = (cause as Error).message;
			busy = false;
		}
	}

	/** After a conflict: the stored recipe replaces the form. */
	async function reload() {
		busy = true;
		try {
			const fresh = await loadRecipe(path);
			base = fresh;
			model?.load(fromRecipe(fresh));
			conflict = false;
			message = '';
		} catch (cause) {
			message = (cause as Error).message;
		} finally {
			busy = false;
		}
	}

	async function remove() {
		if (!base || busy) return;
		if (
			!(await confirmDialog(`Move “${base.title}” to Trash for 30 days?`, {
				title: 'Delete recipe',
				confirmLabel: 'Delete',
				danger: true
			}))
		)
			return;
		busy = true;
		try {
			await api.recipesDelete(path);
			forgetRecipe(path);
			await afterPathsRemoved([path]);
			void refreshNotes().catch(() => {});
			notify('Recipe moved to Trash.', {
				tone: 'success',
				actionLabel: 'View Trash',
				action: () => goto('/trash')
			});
			await slideTo(RECIPES_HOME, 'back', { replace: true });
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
			busy = false;
		}
	}
</script>

<svelte:head><title>{pageTitle(base ? `Edit ${base.title}` : 'Edit recipe')}</title></svelte:head>

<div class="rc-page rc-form" aria-busy={!model && !loadError && !missing}>
	<CornerNav back={recipeHref(path)} backLabel="Back to recipe" omnia={path} />
	{#if model}
		<RecipeForm
			{model}
			caption="Edit recipe"
			saveLabel="Save changes"
			{busy}
			{message}
			{conflict}
			onsave={save}
			ondelete={remove}
			onreload={reload}
		/>
	{:else if missing}
		<p class="rc-empty">Recipe not found</p>
	{:else if loadError}
		<p class="rc-error rc-state">{loadError}</p>
	{/if}
</div>
