import { api } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	try {
		return { actions: (await api.listOmniaActions()).actions };
	} catch {
		return { actions: null };
	}
};
