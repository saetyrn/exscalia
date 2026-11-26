// The Gallery's wire shapes, mirroring app/gallery.py (PinCard, Pin, Board)
// and the routes in app/api/gallery.py.

export interface PinCard {
	/** The picture's vault path, its identity: gallery/<board>/<digest>.md. */
	path: string;
	/** '' until the describer proposes one, or when the owner clears it. */
	title: string;
	/** '' for a picture on no board. */
	board: string;
	/** /api/attachments/<name>, the stored picture. */
	image: string;
	/** /api/gallery/thumb/<name>, a grid-sized WebP. */
	thumb: string;
	width: number | null;
	height: number | null;
	/** The picture's average colour, '#rrggbb': its placeholder. */
	color: string | null;
	created: string;
}

export interface PinStatus {
	/** Embedding or describing is running or queued for it. */
	working: boolean;
	/** The last attempt's failure, one sentence. */
	error: string | null;
}

export interface Pin extends PinCard {
	description: string;
	tags: string[];
	source_url: string | null;
	described: string | null;
	modified: string;
	revision: string;
	status: PinStatus;
}

export interface Board {
	name: string;
	count: number;
	updated: string | null;
	/** Up to three thumbnails, newest first. */
	covers: string[];
}

export interface GalleryStatus {
	working: number;
	describer: boolean;
	visual: boolean;
	visual_error: string | null;
}

export interface GalleryOverview {
	boards: Board[];
	total: number;
	/** The newest three thumbnails of all, for the All pictures card. */
	covers: string[];
	status: GalleryStatus;
}

export interface PinPage {
	pins: PinCard[];
	total: number;
	/** The offset of the next page, or null at the end (and for a search). */
	next: number | null;
}

export interface PinUpdate {
	title?: string;
	description?: string;
	tags?: string[];
	source_url?: string;
	/** Moves the picture; the answer carries its new path. */
	board?: string;
}
