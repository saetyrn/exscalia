// Wire shapes of /api/music (backend/app/music.py). MUSIC.md has the design.

export interface MusicSong {
	id: string;
	file: string;
	bytes: number;
	mime: string;
	title: string;
	artist: string | null;
	album: string | null;
	year: number | null;
	genre: string | null;
	track: number | null;
	disc: number | null;
	duration: number | null;
	/** Vault path of the cover image (attachments/…), or null. */
	cover: string | null;
	liked: boolean;
	added: string;
	/** The audio file is gone from the server; the record stays. */
	missing: boolean;
	/** Fields the owner typed; nothing automatic overwrites them. */
	manual?: string[];
	/** The 256 kbps MP3 copy that streams, once made. */
	stream?: { file: string; bytes: number } | null;
	/** In the trash: when, and the folders it came from. */
	trashed?: { at: string; folders: string[] } | null;
}

export interface MusicFolder {
	id: string;
	name: string;
	children: string[];
	songs: string[];
	liked: string[];
	cover: string | null;
	colour: string | null;
	glyph: string | null;
	pinned: boolean;
}

export interface MusicPlaylist {
	id: string;
	name: string;
	songs: string[];
	cover: string | null;
	background: string | null;
	/** 0 to 100. */
	blur: number;
	dim: number;
}

export interface MusicLibrary {
	revision: number;
	root: string[];
	/** Liked songs, newest first unless reordered. */
	liked: string[];
	playlist_order: string[];
	folders: MusicFolder[];
	playlists: MusicPlaylist[];
	songs: MusicSong[];
}

export const FOLDER_COLOURS = [
	'f59cb2', 'b48d71', 'a05c46', 'e49e91', 'd5708b', 'e9b4b3', 'f2dcbe', 'f0b6c6', 'eeceb6'
] as const;
export const FOLDER_GLYPHS = [
	'note', 'headphones', 'mic', 'vinyl', 'heart', 'star', 'moon', 'flame', 'cloud', 'bolt'
] as const;
