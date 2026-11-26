import { api, type HistoryVersion } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ url }) => {
	const path = url.searchParams.get('path') ?? '';
	if (!path) {
		return {
			path,
			versions: null,
			hasMore: false,
			error: 'Choose a note before opening version history.'
		};
	}
	try {
		const result = await api.history(path);
		const versions: HistoryVersion[] = result.versions;
		return { path, versions, hasMore: result.has_more, error: null };
	} catch (cause) {
		return { path, versions: null, hasMore: false, error: (cause as Error).message };
	}
};
