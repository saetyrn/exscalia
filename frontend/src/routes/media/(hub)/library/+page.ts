import { api } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	try {
		return { items: await api.mediaList() };
	} catch {
		// The page retries; navigation itself never blocks on the API.
		return { items: null };
	}
};
