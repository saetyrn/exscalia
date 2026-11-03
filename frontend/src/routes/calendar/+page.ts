import { cachedMonth, loadMonth } from '$lib/calendarview.svelte';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	const today = new Date();
	const year = today.getFullYear();
	const month = today.getMonth() + 1;
	// A cached month — from the last visit or from the idle preloader — hands
	// the grid its dots on the frame the navigation lands, and the refresh
	// behind it corrects anything that moved. Only a first visit still waits.
	const cached = cachedMonth(year, month);
	if (cached) {
		void loadMonth(year, month).catch(() => {
			// The rendered month stays; a failed revalidation is not worth
			// throwing away content already on screen.
		});
		return { view: cached };
	}
	try {
		return { view: await loadMonth(year, month) };
	} catch {
		return { view: null };
	}
};
