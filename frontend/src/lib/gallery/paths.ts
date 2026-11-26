// Where Gallery screens live. A picture's identity is its vault path
// ("gallery/art/1f2e....md"); its page is /gallery/pin/ plus that path. A
// board is a query parameter on /gallery/pins, never a pathname: the layout
// remounts the page per pathname, and switching boards in the strip should
// keep the grid component and only refetch.
import { encodePath } from '$lib/api';

export const GALLERY_HOME = '/gallery';
export const GALLERY_PINS = '/gallery/pins';
const PIN_PREFIX = '/gallery/pin/';

/** A note in the gallery folder, nested folders included. */
export function isGalleryNotePath(path: string | null | undefined): path is string {
	return !!path && path.startsWith('gallery/') && path.endsWith('.md');
}

/** The picture page for a vault path. */
export function pinHref(path: string): string {
	return `${PIN_PREFIX}${encodePath(path)}`;
}

/** The grid: every picture, one board ('' is no board), or a search. */
export function pinsHref(board: string | null = null, q = ''): string {
	const params = new URLSearchParams();
	if (board !== null) params.set('board', board);
	if (q.trim()) params.set('q', q.trim());
	const qs = params.toString();
	return qs ? `${GALLERY_PINS}?${qs}` : GALLERY_PINS;
}

/** The board a /gallery/pins URL names: null for All. */
export function boardParam(params: URLSearchParams): string | null {
	return params.has('board') ? (params.get('board') ?? '') : null;
}
