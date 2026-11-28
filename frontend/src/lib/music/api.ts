// /api/music beyond the library read (backend/app/api/music.py).
import { apiFetch, toApiError } from '$lib/api';
import type { MusicFolder, MusicPlaylist, MusicSong } from './types';

async function call<T>(path: string, method: string, body?: unknown): Promise<T> {
	const res = await apiFetch(path, {
		method,
		headers: body === undefined ? undefined : { 'content-type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!res.ok) throw await toApiError(res, path);
	return res.json();
}

const enc = encodeURIComponent;

export const musicApi = {
	editSong: (id: string, fields: Record<string, unknown>) =>
		call<MusicSong>(`/api/music/songs/${enc(id)}`, 'PATCH', { fields }),
	like: (id: string, liked: boolean) => call<MusicSong>(`/api/music/songs/${enc(id)}/liked`, 'PUT', { liked }),
	move: (body: { ids: string[]; target: string; source?: string | null; copy?: boolean; index?: number | null }) =>
		call<MusicFolder>('/api/music/songs/move', 'POST', body),
	trash: (ids: string[]) => call<{ trashed: string[] }>('/api/music/songs/trash', 'POST', { ids }),
	restore: (ids: string[]) => call<{ restored: string[] }>('/api/music/songs/restore', 'POST', { ids }),
	emptyTrash: (ids?: string[]) => call<{ deleted: string[] }>('/api/music/trash/empty', 'POST', { ids: ids ?? [] }),
	orderLiked: (ids: string[]) => call<{ liked: string[] }>('/api/music/liked/order', 'PUT', { ids }),

	createFolder: (name: string, parent: string | null, index?: number) =>
		call<MusicFolder>('/api/music/folders', 'POST', { name, parent, index }),
	editFolder: (id: string, fields: Record<string, unknown>) =>
		call<MusicFolder>(`/api/music/folders/${enc(id)}`, 'PATCH', { fields }),
	moveFolder: (id: string, parent: string | null, index?: number | null) =>
		call<MusicFolder>(`/api/music/folders/${enc(id)}/move`, 'POST', { parent, index }),
	orderFolder: (id: string, order: { songs?: string[]; liked?: string[] }) =>
		call<MusicFolder>(`/api/music/folders/${enc(id)}/order`, 'PUT', order),
	removeFromFolder: (id: string, ids: string[]) =>
		call<{ removed: string[] }>(`/api/music/folders/${enc(id)}/remove`, 'POST', { ids }),
	deleteFolder: (id: string) =>
		call<{ folders: string[]; trashed: string[] }>(`/api/music/folders/${enc(id)}`, 'DELETE'),
	orderChildren: (parent: string | null, ids: string[]) =>
		call<{ ids: string[] }>('/api/music/folder-order', 'PUT', { parent, ids }),

	createPlaylist: (name: string, songs: string[] = []) =>
		call<MusicPlaylist>('/api/music/playlists', 'POST', { name, songs }),
	editPlaylist: (id: string, fields: Record<string, unknown>) =>
		call<MusicPlaylist>(`/api/music/playlists/${enc(id)}`, 'PATCH', { fields }),
	playlistAdd: (id: string, ids: string[], index?: number | null) =>
		call<MusicPlaylist>(`/api/music/playlists/${enc(id)}/add`, 'POST', { ids, index }),
	playlistRemove: (id: string, ids: string[]) =>
		call<MusicPlaylist>(`/api/music/playlists/${enc(id)}/remove`, 'POST', { ids }),
	deletePlaylist: (id: string) => call<{ ok: true }>(`/api/music/playlists/${enc(id)}`, 'DELETE'),
	orderPlaylists: (ids: string[]) => call<{ ids: string[] }>('/api/music/playlist-order', 'PUT', { ids }),

	/** A picture from this computer into the vault; returns its vault path. */
	async picture(file: File): Promise<string> {
		const form = new FormData();
		form.append('file', file);
		const res = await apiFetch('/api/music/pictures', { method: 'POST', body: form });
		if (!res.ok) throw await toApiError(res, '/api/music/pictures');
		return (await res.json()).path;
	},

	openUpload: (body: { name: string; size: number; folder: string | null; parts: string[] }) =>
		call<{ id: string; received: number; piece: number }>('/api/music/uploads', 'POST', body),
	uploadStatus: (id: string) => call<{ received: number; size: number }>(`/api/music/uploads/${enc(id)}`, 'GET'),
	async uploadPiece(id: string, offset: number, piece: Blob): Promise<number> {
		const path = `/api/music/uploads/${enc(id)}?offset=${offset}`;
		const res = await apiFetch(path, {
			method: 'PUT',
			headers: { 'content-type': 'application/octet-stream' },
			body: piece
		});
		if (!res.ok) throw await toApiError(res, path);
		return (await res.json()).received;
	},
	finishUpload: (id: string) =>
		call<{ added: string[]; duplicates: { name: string; in: string[] }[]; failed: { name: string; error: string }[] }>(
			`/api/music/uploads/${enc(id)}/finish`,
			'POST'
		),
	cancelUpload: (id: string) => call<{ ok: true }>(`/api/music/uploads/${enc(id)}`, 'DELETE')
};

// -- the shared session (Deploy 3) ----------------------------------------------

export interface SessionState {
	version: number;
	queue: string[];
	original: string[];
	index: number;
	context: string | null;
	shuffle: boolean;
	repeat: 'off' | 'all' | 'one';
	playing: boolean;
	position: number;
	/** Server seconds when `position` was true. */
	position_at: number;
	/** Rises on every explicit jump. */
	seek: number;
	speaker: string | null;
	speaker_name: string | null;
	/** The server's clock when it answered, for the skew. */
	now: number;
}

export class StaleSession extends Error {
	constructor(public session: SessionState) {
		super('The player changed meanwhile');
	}
}

export const sessionApi = {
	get: () => call<SessionState>('/api/music/session', 'GET'),
	async put(base: number, state: Partial<SessionState>): Promise<SessionState> {
		const path = '/api/music/session';
		const res = await apiFetch(path, {
			method: 'PUT',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ base, state })
		});
		if (res.status === 409) throw new StaleSession((await res.json()).session);
		if (!res.ok) throw await toApiError(res, path);
		return res.json();
	},
	heartbeat: (body: { speaker: string; position: number; playing: boolean; final?: boolean }) =>
		call<SessionState>('/api/music/session/heartbeat', 'POST', body),
	play: (body: {
		song: string;
		device: string;
		seconds: number;
		duration: number | null;
		ended: 'finished' | 'skipped' | 'stopped';
		repeat: boolean;
		context: string | null;
	}) => call<{ ok: true }>('/api/music/plays', 'POST', body),
	recent: (limit = 100) => call<{ played: { song: string; at: string; plays: number }[] }>(`/api/music/recent?limit=${limit}`, 'GET'),
	token: () => call<{ token: string; expires: number }>('/api/music/token', 'GET')
};
