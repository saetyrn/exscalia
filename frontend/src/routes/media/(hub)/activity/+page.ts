import { api } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async () => {
	try {
		return { timeline: await api.mediaTimeline() };
	} catch {
		return { timeline: null };
	}
};
