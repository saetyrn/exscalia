// Stream and download URLs. On the web the login cookie rides along with the
// <audio> request. The desktop app signs in with a Bearer token that an
// <audio> element cannot send, so there the URL points at the server and
// carries a day-long signed token instead (backend/app/music_links.py).
import { apiBase, isDesktop } from '$lib/runtime';
import { sessionApi } from './api';
import { losslessOn } from './settings';

let token: { token: string; expires: number } | null = null;
let fetching: Promise<void> | null = null;

/** Fetch a token when there is none or it has under an hour left. */
export function refreshToken(force = false): Promise<void> {
	if (!isDesktop) return Promise.resolve();
	const fresh = token && token.expires - Date.now() / 1000 > 3600;
	if (fresh && !force) return Promise.resolve();
	fetching ??= sessionApi
		.token()
		.then((t) => {
			token = t;
		})
		.catch(() => {})
		.finally(() => {
			fetching = null;
		});
	return fetching;
}

function signed(path: string, query: URLSearchParams): string {
	if (!isDesktop) {
		const qs = query.toString();
		return qs ? `${path}?${qs}` : path;
	}
	void refreshToken();
	if (token) query.set('t', token.token);
	return `${apiBase()}${path}?${query.toString()}`;
}

export function streamUrl(id: string): string {
	const query = new URLSearchParams();
	if (losslessOn()) query.set('lossless', '1');
	return signed(`/api/music/stream/${encodeURIComponent(id)}`, query);
}

export function downloadUrl(id: string): string {
	return signed(`/api/music/download/${encodeURIComponent(id)}`, new URLSearchParams());
}
