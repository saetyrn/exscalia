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
