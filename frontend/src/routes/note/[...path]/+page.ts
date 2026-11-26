import { api, type Note } from '$lib/api';
import { readDraft } from '$lib/drafts';
import { takePrefetchedNote } from '$lib/noteprefetch';
import type { PageLoad } from './$types';

// Prefetch the server copy during navigation so the editor opens fully formed.
// The page still owns the draft-recovery flow; it just starts with data in hand.
export const load: PageLoad = async ({ params }) => {
	const path = params.path ?? '';
	// The preloader may already hold this body. Taking it renders on the frame
	// the link is clicked instead of a round-trip later — but only in the plain
	// case: no draft waiting on this device, so the page has nothing to
	// reconcile and would simply have fetched and rendered. Anything else falls
	// through to the fetch and the recovery flow it feeds. `prefetched` tells
	// the page to revalidate behind the instant render.
	if (!(await readDraft(path))) {
		const prefetched = takePrefetchedNote(path);
		if (prefetched) return { path, note: prefetched, loadError: null, prefetched: true };
	}
	try {
		const note: Note = await api.readNote(path);
		return { path, note, loadError: null, prefetched: false };
	} catch (cause) {
		return { path, note: null, loadError: (cause as Error).message, prefetched: false };
	}
};
