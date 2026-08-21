import { api, type HistoryVersion } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ url }) => {
	const path = url.searchParams.get('path') ?? '';
	if (!path) {
		return { path, versions: null, error: 'Choose a note before opening version history.' };
	}
	try {
		const versions: HistoryVersion[] = (await api.history(path)).versions;
		return { path, versions, error: null };
	} catch (cause) {
		return { path, versions: null, error: (cause as Error).message };
	}
};
