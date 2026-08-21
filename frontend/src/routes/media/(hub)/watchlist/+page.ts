import { api } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	try {
		return { items: await api.mediaList({ status: 'planned' }) };
	} catch {
		return { items: null };
	}
};
