import { ApiError, api } from '$lib/api';
import type { PageLoad } from './$types';

// The route param is the notebook path without the `paper/` root; the api
// module puts it back. Fetching here keeps the previous view on screen until
// the manifest is ready instead of flashing an empty scroller.
export const load: PageLoad = async ({ params }) => {
	const route = params.notebook;
	// A durable route is /paper/id/<notebook id>/<page id>. Resolve it once to
	// the current path; the viewer still uses the same notebook scroller.
	const stable = route.match(/^id\/([^/]+)\/([^/]+)$/);
	const notebook = stable ? stable[1] : route;
	// The ids outlive a failed resolve: the viewer's retry has to resolve them
	// again, not fetch the notebook id as if it were a path.
	const ids = stable ? { notebookId: decodeId(stable[1]), pageId: decodeId(stable[2]) } : null;
	try {
		if (ids) {
			const resolved = await api.paperResolve(ids.notebookId, ids.pageId);
			return {
				notebook: resolved.notebook,
				book: await api.paperNotebook(resolved.notebook),
				initialPageId: ids.pageId,
				stable: { notebookId: resolved.notebook_id, pageId: ids.pageId },
				missing: false
			};
		}
		return { notebook, book: await api.paperNotebook(notebook), initialPageId: null, stable: null, missing: false };
	} catch (error) {
		const initialPageId = ids?.pageId ?? null;
		// A 404 is a dead end the page states plainly, not something to retry.
		if (error instanceof ApiError && error.status === 404) {
			return { notebook, book: null, initialPageId, stable: ids, missing: true };
		}
		return { notebook, book: null, initialPageId, stable: ids, missing: false };
	}
};

// A stray `%` in an id is a dead link to state, not a thrown route.
function decodeId(raw: string): string {
	try {
		return decodeURIComponent(raw);
	} catch {
		return raw;
	}
}
