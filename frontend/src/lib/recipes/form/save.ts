// Saving a recipe form: a new draft becomes a recipe, an edit updates one.
// The pages own navigation and messages; these own the calls and the cache.
import { api, ApiError, uploadAttachment } from '$lib/api';
import { rememberRecipe } from '../cache.svelte';
import { recipeInput } from '../recipepage';
import type { Recipe, RecipeIn } from '../types';
import { dataUrlFile } from './files';
import { setThumbUrl, type Handoff } from './handoff.svelte';

/** The card photo for a photo import, uploaded through the attachments
 * route; null when there is none or the upload fails (the recipe saves
 * without one). Uploaded once per draft. */
async function cardPhoto(handoff: Handoff | null): Promise<string | null> {
	if (!handoff?.thumb) return null;
	if (handoff.thumbUrl) return handoff.thumbUrl;
	try {
		const { url } = await uploadAttachment(dataUrlFile(handoff.thumb, 'recipe.jpg'));
		setThumbUrl(url);
		return url;
	} catch {
		return null;
	}
}

/** Create the recipe; a photo import gets its first photo as the card. A
 * link import's own thumbnail (a remote https URL) goes through untouched for
 * the server to fetch. */
export async function saveNew(recipe: RecipeIn, handoff: Handoff | null): Promise<Recipe> {
	let body = recipe;
	if (!recipe.source?.thumbnail) {
		const thumbnail = await cardPhoto(handoff);
		if (thumbnail)
			body = {
				...recipe,
				source: { ...(recipe.source ?? { type: 'photo', url: null }), thumbnail }
			};
	}
	return rememberRecipe(await api.recipesCreate(body));
}

const sameRecord = (a: Recipe, b: Recipe): boolean =>
	JSON.stringify(recipeInput(a)) === JSON.stringify(recipeInput(b));

/** Update from the copy the form loaded. `recipe` carries no art, so the
 * drawing is kept. A drawing that landed while the form was open bumps the
 * revision and changes nothing the form holds, so then the update goes again
 * from the new revision; any other change elsewhere throws ApiError 409 and
 * writes nothing. */
export async function saveEdit(path: string, recipe: RecipeIn, base: Recipe): Promise<Recipe> {
	try {
		return rememberRecipe(await api.recipesUpdate(path, recipe, base.revision));
	} catch (cause) {
		if (!(cause instanceof ApiError) || cause.status !== 409) throw cause;
		const stored = await api.recipesGet(path);
		if (!sameRecord(stored, base)) throw cause;
		return rememberRecipe(await api.recipesUpdate(path, recipe, stored.revision));
	}
}

/** What the edit form does when the stored recipe moves on behind it (a
 * drawing lands, OMNIA or another device saves). A change to nothing the form
 * shows (the drawing) only moves the base revision, so the next save does not
 * 409 over it; a real change loads into a form nobody has touched, and is
 * otherwise left for the save's 409 to report. */
export function followStored(
	base: Recipe,
	fresh: Recipe,
	pristine: boolean
): 'same' | 'adopt' | 'load' | 'keep' {
	if (fresh.revision === base.revision) return 'same';
	if (JSON.stringify(recipeInput(fresh)) === JSON.stringify(recipeInput(base))) return 'adopt';
	return pristine ? 'load' : 'keep';
}
