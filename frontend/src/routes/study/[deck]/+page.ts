import { api } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ params, url }) => {
	const order = url.searchParams.get('order') ?? 'due';
	try {
		return { deck: await api.getDeck(params.deck, order), order, error: null };
	} catch (cause) {
		return { deck: null, order, error: (cause as Error).message };
	}
};
