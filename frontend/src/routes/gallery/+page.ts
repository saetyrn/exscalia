import { api } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	try {
		return { overview: await api.galleryOverview() };
	} catch {
		// The page retries on the next live event; navigation never blocks on it.
		return { overview: null };
	}
};
