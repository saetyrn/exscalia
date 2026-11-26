import { api } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	try {
		return { library: await api.paperLibrary() };
	} catch {
		// The page retries on the next live event; navigation itself never
		// blocks on the API.
		return { library: null };
	}
};
