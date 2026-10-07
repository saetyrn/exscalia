<script lang="ts">
	// Review a new draft before it is saved, after the recipe app's Review.tsx
	// in its "new" mode. The draft comes from the add page through the
	// hand-off module (and this tab's storage, so a reload shows it again);
	// with none, this is an empty form to type a recipe into. Save creates the
	// recipe and slides to its page in place of this one, so Back from there
	// does not return to a spent form.
	import { ApiError } from '$lib/api';
	import CornerNav from '$lib/recipes/CornerNav.svelte';
	import { RecipeFormModel } from '$lib/recipes/form/form.svelte';
	import { clearDraft, readDraft } from '$lib/recipes/form/handoff.svelte';
	import { emptyForm, fromDraft } from '$lib/recipes/form/model';
	import RecipeForm from '$lib/recipes/form/RecipeForm.svelte';
	import { saveNew } from '$lib/recipes/form/save';
	import { forward } from '$lib/recipes/nav';
	import { pageTitle, RECIPES_ADD, recipeHref } from '$lib/recipes/paths';
	import type { RecipeIn } from '$lib/recipes/types';
	import { refreshNotes } from '$lib/stores.svelte';

	const handoff = readDraft();
	const model = new RecipeFormModel(handoff ? fromDraft(handoff.draft) : emptyForm());

	let busy = $state(false);
	let message = $state('');

	async function save(recipe: RecipeIn) {
		busy = true;
		message = '';
		try {
			const saved = await saveNew(recipe, handoff);
			clearDraft();
			void refreshNotes().catch(() => {});
			await forward(recipeHref(saved.path), { replace: true });
		} catch (cause) {
			if (cause instanceof ApiError && cause.status === 422) model.refused(cause.message);
			else message = (cause as Error).message;
			busy = false;
		}
	}
</script>

<svelte:head><title>{pageTitle('Review draft')}</title></svelte:head>

<div class="rc-page rc-form">
	<CornerNav back={RECIPES_ADD} backLabel="Back to add" />
	<RecipeForm
		{model}
		caption="Review draft"
		method={handoff?.draft.method ?? null}
		missing={handoff?.draft.missing ?? []}
		saveLabel="Save recipe"
		{busy}
		{message}
		onsave={save}
	/>
</div>
