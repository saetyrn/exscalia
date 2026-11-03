import { cachedHub, loadHub } from '$lib/mediahub.svelte';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	// A cached payload — from the last visit, or from the idle preloader — hands
	// the page something to draw on the same frame the navigation lands, and the
	// refresh behind it swaps in whatever changed. Only a first-ever visit with
	// nothing cached still waits, and that is the path that keeps the loading
	// state and the error handling below it.
	const cached = cachedHub();
	if (cached) {
		void loadHub().catch(() => {
			// The rendered payload stays; a failed revalidation is not an error
			// worth showing over content that is already on screen.
		});
		return { hub: cached };
	}
	try {
		return { hub: await loadHub() };
	} catch {
		return { hub: null };
	}
};
