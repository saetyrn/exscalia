import { api } from '$lib/api';
import type { PageLoad } from './$types';

// The URL is /media/<vault path>, and media notes live under the vault's
// media/ folder — so the literal second segment carries the folder name and
// the rest param the file. Reassembled here; keeps /media itself free for
// the hub without a rest-route conflict.
export const load: PageLoad = async ({ params }) => {
	const path = `media/${params.file}`;
	try {
		return { detail: await api.mediaGet(path), path };
	} catch {
		return { detail: null, path };
	}
};
