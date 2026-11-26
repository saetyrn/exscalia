// Where a Paper notebook lives, in three coordinate systems, and the pure
// conversions between them:
//
//   vault path   `paper/Work/Ideas.paper`        — what the API takes and the
//                                                   index/OMNIA/LiveBus use
//   route param  `Work/Ideas.paper`              — `/paper/[...notebook]`
//   page anchor  `#<pageId>`                     — a page inside the notebook
//
// A page never gets a pathname of its own: the layout remounts the route per
// pathname, so scrolling from page to page would tear the scroller down each
// time. The hash carries the page instead, and hash changes are cheap.

/** Notebook path with the `paper/` root prefix, the form every `/api/paper`
 * route expects. Accepts either form so callers never have to check. */
export function paperVaultPath(path: string): string {
	const clean = path.replace(/^\/+/, '').replace(/\/+$/, '');
	return clean === 'paper' || clean.startsWith('paper/') ? clean : `paper/${clean}`;
}

/** Route param form: the vault path minus the `paper/` root. */
export function paperRoutePath(path: string): string {
	return paperVaultPath(path).slice('paper/'.length);
}

/** Link to a notebook's viewer page. Segments are encoded individually so
 * slashes stay separators and spaces/`#` in a title do not. */
export function notebookHref(path: string): string {
	const rest = paperRoutePath(path);
	if (!rest) return '/paper';
	return `/paper/${rest.split('/').map(encodeURIComponent).join('/')}`;
}

/** Link straight to one page of a notebook — what search hits and OMNIA use. */
export function pageHref(path: string, pageId: string): string {
	const href = notebookHref(path);
	return pageId ? `${href}#${encodeURIComponent(pageId)}` : href;
}

/** Rename-proof page link. Region is x,y,width,height in page points. */
export function durablePageHref(
	notebookId: string,
	pageId: string,
	region?: string
): string {
	const base = `/paper/id/${encodeURIComponent(notebookId)}/${encodeURIComponent(pageId)}`;
	return region ? `${base}?region=${encodeURIComponent(region)}` : base;
}

/** Read a lasso region from a durable Paper link. */
export function parsePaperRegion(search: string | null | undefined): string | null {
	if (!search) return null;
	const params = new URLSearchParams(search.startsWith('?') ? search : `?${search}`);
	const region = params.get('region');
	return region && /^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?$/.test(region)
		? region
		: null;
}

/** The page id a `location.hash` points at, or null for none. Tolerates a
 * bare id, a leading `#`, and percent-encoding. */
export function parsePaperHash(hash: string | null | undefined): string | null {
	if (!hash) return null;
	const raw = hash.startsWith('#') ? hash.slice(1) : hash;
	if (!raw) return null;
	try {
		return decodeURIComponent(raw) || null;
	} catch {
		return raw; // Malformed escape — better a wrong id than a thrown route.
	}
}

/** A folder's vault path, `paper/Work/Accounting`, with `paper` for the top
 * level. Accepts the path with or without the root, as the API does. */
export function paperFolderPath(path: string | null | undefined): string {
	const clean = (path ?? '').split('/').filter(Boolean).join('/');
	return clean ? paperVaultPath(clean) : 'paper';
}

/** The library opened at a folder. A query parameter, never a pathname, so
 * it cannot clash with `/paper/<notebook>` and opening a folder does not
 * remount the library. The value is `encodeURIComponent` of the vault path
 * with its `paper/` root, exactly what OMNIA builds: tabs dedupe on the
 * exact URL, so a form-encoded `+` for a space would open a twin. */
export function folderHref(path: string): string {
	const folder = paperFolderPath(path);
	return folder === 'paper' ? '/paper' : `/paper?folder=${encodeURIComponent(folder)}`;
}

/** The folder a library URL (or its search part) opens; `paper` for none. */
export function parseFolderParam(source: string | URLSearchParams | null | undefined): string {
	if (!source) return 'paper';
	let params: URLSearchParams;
	if (typeof source === 'string') {
		const query = source.split('#')[0];
		const at = query.indexOf('?');
		params = new URLSearchParams(at < 0 ? '' : query.slice(at));
	} else {
		params = source;
	}
	return paperFolderPath(params.get('folder'));
}

/** A folder's name inferred from its path: the directory name, a slug of
 * the folder's name. The placeholder for a tab title. */
export function folderTitleFromPath(path: string): string {
	const folder = paperFolderPath(path);
	return folder === 'paper' ? 'Paper' : (folder.split('/').pop() ?? folder);
}

/** The tab title of a library URL: the section, then the open folder. */
export function paperLibraryTitle(url: string): string {
	const folder = parseFolderParam(url);
	return folder === 'paper' ? 'Paper' : `Paper · ${folderTitleFromPath(folder)}`;
}

/** What `regionOnSheet` needs of a page: its presented size, and for a board
 * the rectangle of the canvas its picture shows. */
export interface RegionSheet {
	size?: { width: number; height: number } | null;
	size_points?: { width: number; height: number } | null;
	size_name?: string | null;
	board_extent?: { width: number; height: number } | null;
	render_rect?: { x: number; y: number; width: number; height: number } | null;
}

/** Where a region (x,y,width,height in page points, as a durable link or a
 * search hit carries it) sits on a page's picture, as fractions of that
 * picture, clipped to it; null when nothing of it is on the picture.
 *
 * A notebook page's picture is the whole page. A board's is its render
 * rectangle somewhere inside the canvas, so a point maps as
 * `(x - rect.x) / rect.width`; with no render yet, the whole extent. */
export function regionOnSheet(
	region: string | null | undefined,
	sheet: RegionSheet
): { left: number; top: number; width: number; height: number } | null {
	if (!region) return null;
	const values = region.split(',').map(Number);
	if (values.length !== 4 || values.some((value) => !Number.isFinite(value))) return null;
	const [x, y, w, h] = values;
	let origin = { x: 0, y: 0 };
	let size = sheet.size_points ?? sheet.size;
	if (sheet.size_name === 'board') {
		const rect = sheet.render_rect;
		if (rect && rect.width > 0 && rect.height > 0) {
			origin = rect;
			size = rect;
		} else {
			size = sheet.board_extent ?? sheet.size;
		}
	}
	const width = size?.width || 595;
	const height = size?.height || 842;
	const left = Math.max(0, (x - origin.x) / width);
	const top = Math.max(0, (y - origin.y) / height);
	const right = Math.min(1, (x + w - origin.x) / width);
	const bottom = Math.min(1, (y + h - origin.y) / height);
	if (right <= left || bottom <= top) return null;
	return { left, top, width: right - left, height: bottom - top };
}

/** Display title inferred from the path. The directory name is a
 * Windows-safe slug and `notebook.json` holds the verbatim title, so this is
 * the placeholder used before (or instead of) a loaded manifest — the tab
 * title, and the notebook header while the fetch is in flight. */
export function notebookTitleFromPath(path: string): string {
	const rest = paperRoutePath(path);
	const name = rest.split('/').filter(Boolean).pop() ?? '';
	const title = name.replace(/\.paper$/i, '');
	return title || 'Paper';
}
