// The music library, held once for the whole app: the Music page browses it
// and the player bar names what is playing from it. One fetch of the whole
// library (a thousand songs is a few hundred KB); the server announces every
// change with a `music` live event, and a resync after a gap in the stream
// (`exscalia:data-changed`) refetches too.
import { api } from '$lib/api';
import type { LiveNoteEvent } from '$lib/live';
import type { MusicFolder, MusicLibrary, MusicPlaylist, MusicSong } from './types';

class Library {
	songs = $state<Record<string, MusicSong>>({});
	folders = $state<Record<string, MusicFolder>>({});
	playlists = $state<Record<string, MusicPlaylist>>({});
	root = $state<string[]>([]);
	liked = $state<string[]>([]);
	playlistOrder = $state<string[]>([]);
	revision = $state(-1);
	loaded = $state(false);
	error = $state<string | null>(null);
	#inflight: Promise<void> | null = null;
	#again = false;

	/** Folder id -> parent folder id (null at the top level). */
	parents = $derived.by(() => {
		const out: Record<string, string | null> = {};
		for (const id of this.root) out[id] = null;
		for (const folder of Object.values(this.folders))
			for (const child of folder.children) out[child] = folder.id;
		return out;
	});

	load(): Promise<void> {
		if (this.#inflight) {
			// A change landed while a fetch was out: fetch once more after it.
			this.#again = true;
			return this.#inflight;
		}
		this.#inflight = (async () => {
			try {
				do {
					this.#again = false;
					this.#apply(await api.musicLibrary());
				} while (this.#again);
				this.error = null;
			} catch (err) {
				this.error = err instanceof Error ? err.message : String(err);
			} finally {
				this.#inflight = null;
			}
		})();
		return this.#inflight;
	}

	/** Songs in the trash, newest first. */
	trashed = $derived(
		Object.values(this.songs)
			.filter((s) => s.trashed)
			.sort((a, b) => (b.trashed?.at ?? '').localeCompare(a.trashed?.at ?? ''))
	);

	/** Every folder, pinned or not, as (folder, path of names). */
	allFolders(): { folder: MusicFolder; path: string }[] {
		const out: { folder: MusicFolder; path: string }[] = [];
		const walk = (ids: string[], prefix: string) => {
			for (const id of ids) {
				const folder = this.folders[id];
				if (!folder) continue;
				const path = prefix ? `${prefix} / ${folder.name}` : folder.name;
				out.push({ folder, path });
				walk(folder.children, path);
			}
		};
		walk(this.root, '');
		return out;
	}

	ensure(): Promise<void> {
		return this.loaded ? Promise.resolve() : this.load();
	}

	#apply(data: MusicLibrary) {
		this.songs = Object.fromEntries(data.songs.map((s) => [s.id, s]));
		this.folders = Object.fromEntries(data.folders.map((f) => [f.id, f]));
		this.playlists = Object.fromEntries((data.playlists ?? []).map((p) => [p.id, p]));
		this.root = data.root;
		this.liked = data.liked ?? [];
		this.playlistOrder = data.playlist_order ?? [];
		this.revision = data.revision;
		this.loaded = true;
	}

	/** Names from the top level down to `id`, for the path along the top. */
	trail(id: string | null): MusicFolder[] {
		const out: MusicFolder[] = [];
		const seen = new Set<string>();
		let at = id;
		while (at && this.folders[at] && !seen.has(at)) {
			seen.add(at);
			out.unshift(this.folders[at]);
			at = this.parents[at] ?? null;
		}
		return out;
	}

	/** Keep in step with the server while the app is open. */
	listen(): () => void {
		const onLive = (event: Event) => {
			const detail = (event as CustomEvent<LiveNoteEvent | undefined>).detail;
			// The player follows `music/session` itself.
			if (detail?.type !== 'music' || detail.path === 'music/session') return;
			if (typeof detail.revision === 'number' && detail.revision === this.revision) return;
			if (this.loaded) void this.load();
		};
		const onResync = () => {
			if (this.loaded) void this.load();
		};
		window.addEventListener('exscalia:live', onLive);
		window.addEventListener('exscalia:data-changed', onResync);
		return () => {
			window.removeEventListener('exscalia:live', onLive);
			window.removeEventListener('exscalia:data-changed', onResync);
		};
	}
}

export const library = new Library();

export { downloadUrl, streamUrl } from './links';

/** A vault attachment path ("attachments/x.png") as a URL. */
export function coverUrl(path: string | null | undefined): string | null {
	if (!path) return null;
	return `/api/${path.split('/').map(encodeURIComponent).join('/')}`;
}

/** The cover a song shows in a folder: the folder's picked cover inside it,
 * the song's own everywhere else (MUSIC.md, Covers). */
export function coverIn(song: MusicSong | undefined, folderId: string | null): string | null {
	if (!song) return null;
	const folder = folderId ? library.folders[folderId] : undefined;
	if (folder?.cover && folder.songs.includes(song.id)) return coverUrl(folder.cover);
	return coverUrl(song.cover);
}

export function formatTime(seconds: number | null | undefined): string {
	if (!seconds || !Number.isFinite(seconds) || seconds < 0) return '0:00';
	const total = Math.floor(seconds);
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = String(total % 60).padStart(2, '0');
	return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}
