import { ApiError, api } from '$lib/api';
import type { PageLoad } from './$types';

// Fetching during navigation keeps the previous page in place until the board
// is ready — no loading flash. Errors fall through to the page's own loader.
// An empty `folder` is the universal board; anything else is that folder's.
export const load: PageLoad = async ({ params }) => {
	const folder = params.folder ?? '';
	try {
		return { tasks: await api.listTasks(false, '', folder), folder, missing: false };
	} catch (error) {
		// A 404 means the folder has no board — a dead end the page states
		// plainly, rather than a transient failure it should retry.
		if (error instanceof ApiError && error.status === 404) {
			return { tasks: [], folder, missing: true };
		}
		return { tasks: null, folder, missing: false };
	}
};
