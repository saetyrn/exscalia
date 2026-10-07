import { cachedList, loadRecipes } from '$lib/recipes/cache.svelte';
import type { PageLoad } from './$types';

// The menu as last seen draws on the frame the navigation lands, and the
// refresh behind it lands in the cache, which the page can read reactively
// (cachedList()). Only a first visit waits for the server. This is the one
// request an arrival makes, Back included: the page starts none of its own.
export const load: PageLoad = async () => {
	const cached = cachedList();
	if (cached) {
		void loadRecipes().catch(() => {
			// What is on screen stays; a failed refresh is not worth an error.
		});
		return { list: cached, error: '' };
	}
	try {
		return { list: await loadRecipes(), error: '' };
	} catch (e) {
		return { list: null, error: e instanceof Error ? e.message : String(e) };
	}
};
