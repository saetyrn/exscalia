import { cachedRecipe, loadRecipe } from '$lib/recipes/cache.svelte';
import { recipePathFromParam } from '$lib/recipes/paths';
import type { PageLoad } from './$types';

// The URL is /recipes/<vault path>, and recipe notes live under the vault's
// recipes/ folder, so the literal second segment is the folder and the rest
// param the file, as on /media/media/[...file]. A recipe already in the cache
// (prefetched from the menu, or seen before) draws on the frame the slide
// lands and refreshes behind it; cachedRecipe(path) is reactive.
export const load: PageLoad = async ({ params }) => {
	const path = recipePathFromParam(params.file);
	const cached = cachedRecipe(path);
	if (cached) {
		void loadRecipe(path).catch(() => {
			// The cached copy stays on screen.
		});
		return { path, recipe: cached };
	}
	try {
		return { path, recipe: await loadRecipe(path) };
	} catch {
		return { path, recipe: null };
	}
};
