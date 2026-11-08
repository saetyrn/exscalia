import { redirect } from '@sveltejs/kit';
import { api, ApiError } from '$lib/api';
import { ordinaryNoteHref } from '$lib/recipes/links';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ params }) => {
	try {
		return {
			path: params.path,
			pin: await api.galleryPin(params.path),
			error: ''
		};
	} catch (e) {
		// Every list links a note in the gallery folder here; one that is no
		// picture (no `gallery: 1`) opens in the editor instead.
		if (e instanceof ApiError && e.status === 404) {
			const href = await ordinaryNoteHref(params.path);
			if (href) redirect(307, href);
		}
		return {
			path: params.path,
			pin: null,
			error: e instanceof Error ? e.message : String(e)
		};
	}
};
