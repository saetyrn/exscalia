// The Paper library as folders, kept pure so the rules can be tested without
// a browser: the folder tree built from `GET /api/paper`, the three sorts and
// four filters the iPad library uses (CONTRACT §3.6), and how a folder, a
// date or a board is presented.
//
// Everything here reads. The web viewer never creates, renames, moves or
// deletes anything; the iPad is the only writer.
import type { PaperFolderRecord, PaperLibrary, PaperNotebook, PaperPage } from './api';
import { notebookTitleFromPath, paperFolderPath } from './paperurl';

export type LibrarySort = 'date' | 'name' | 'type';
export type LibraryFilter = 'all' | 'folders' | 'notebooks' | 'whiteboards';

export const LIBRARY_SORTS: LibrarySort[] = ['date', 'name', 'type'];
export const LIBRARY_FILTERS: LibraryFilter[] = ['all', 'folders', 'notebooks', 'whiteboards'];

/** `localStorage` keys, the same names the iPad keeps them under. */
export const SORT_KEY = 'paper.library.sort';
export const FILTER_KEY = 'paper.library.filter';

export function parseLibrarySort(raw: unknown): LibrarySort {
	return LIBRARY_SORTS.includes(raw as LibrarySort) ? (raw as LibrarySort) : 'date';
}

export function parseLibraryFilter(raw: unknown): LibraryFilter {
	return LIBRARY_FILTERS.includes(raw as LibraryFilter) ? (raw as LibraryFilter) : 'all';
}

/** A row of the library: a folder or a document, keyed by its vault path. */
export type LibraryItem =
	| { kind: 'folder'; path: string; folder: PaperFolderRecord }
	| { kind: 'document'; path: string; notebook: PaperNotebook };

export interface LibraryTree {
	/** Every folder by vault path; the top level (`paper`) is not one. */
	folders: Map<string, PaperFolderRecord>;
	/** Every folder by id, to follow one that moved. */
	byId: Map<string, PaperFolderRecord>;
	/** What each folder holds, by the folder's vault path. */
	children: Map<string, LibraryItem[]>;
}

const parentOf = (path: string): string => path.split('/').slice(0, -1).join('/') || 'paper';

/** The vault path of the folder holding a folder; `paper` at the top. */
export function parentFolder(path: string): string {
	return parentOf(paperFolderPath(path));
}

/** The folder a summary sits in. `folder` is the vault path, `paper` at the
 * top level; a summary without one is placed by its own path. */
function folderOfNotebook(notebook: PaperNotebook): string {
	return typeof notebook.folder === 'string'
		? paperFolderPath(notebook.folder)
		: parentOf(paperFolderPath(notebook.path));
}

/** What a folder met without a record reads as: its directory name, blue,
 * no icon, no star. The server synthesises the same; this covers a summary
 * that names a folder the listing does not. */
function plainFolder(path: string): PaperFolderRecord {
	return {
		id: '',
		name: path.split('/').pop() ?? path,
		color: 'blue',
		icon: null,
		favorite: false,
		created: '',
		modified: '',
		seq: 0,
		path,
		parent: parentOf(path)
	};
}

export function buildLibraryTree(library: PaperLibrary | null | undefined): LibraryTree {
	const folders = new Map<string, PaperFolderRecord>();
	for (const record of library?.folder_records ?? []) {
		const path = paperFolderPath(record.path);
		if (path === 'paper') continue;
		folders.set(path, { ...record, path, parent: parentOf(path) });
	}
	// Every folder reachable from the top: a directory listed without a
	// record, or one a notebook names, stands in as a plain folder, and so
	// does every ancestor on the way down to it.
	const ensure = (path: string) => {
		for (let at = path; at !== 'paper' && !folders.has(at); at = parentOf(at)) {
			folders.set(at, plainFolder(at));
		}
	};
	for (const path of library?.folders ?? []) ensure(paperFolderPath(path));
	for (const notebook of library?.notebooks ?? []) ensure(folderOfNotebook(notebook));
	for (const path of [...folders.keys()]) ensure(parentOf(path));

	const children = new Map<string, LibraryItem[]>();
	const add = (parent: string, item: LibraryItem) => {
		const list = children.get(parent);
		if (list) list.push(item);
		else children.set(parent, [item]);
	};
	const byId = new Map<string, PaperFolderRecord>();
	for (const [path, folder] of folders) {
		add(folder.parent, { kind: 'folder', path, folder });
		if (folder.id) byId.set(folder.id, folder);
	}
	for (const notebook of library?.notebooks ?? []) {
		add(folderOfNotebook(notebook), { kind: 'document', path: notebook.path, notebook });
	}
	return { folders, byId, children };
}

/** A folder's record by path; a path that differs only in case still finds
 * it. Null for the top level and for a folder that is not there. */
export function findFolder(tree: LibraryTree, path: string): PaperFolderRecord | null {
	const wanted = paperFolderPath(path);
	if (wanted === 'paper') return null;
	const exact = tree.folders.get(wanted);
	if (exact) return exact;
	const folded = wanted.toLocaleLowerCase();
	for (const [candidate, folder] of tree.folders) {
		if (candidate.toLocaleLowerCase() === folded) return folder;
	}
	return null;
}

/** What a folder holds, unsorted; nothing for a folder that is not there. */
export function childrenOf(tree: LibraryTree, path: string): LibraryItem[] {
	const folder = paperFolderPath(path);
	const canonical = folder === 'paper' ? folder : (findFolder(tree, folder)?.path ?? folder);
	return tree.children.get(canonical) ?? [];
}

export function isWhiteboard(notebook: PaperNotebook): boolean {
	return notebook.kind === 'whiteboard';
}

const SIZE_RATIOS: Record<string, number> = {
	a4: 595 / 842,
	letter: 612 / 792,
	ipad: 1024 / 1366,
	infinite: 1024 / 1366
};

/** A cover's width over its height before its render arrives, so the row
 * reserves a page-shaped box: the paper size, turned for landscape, or the
 * picture of an empty board for a whiteboard. */
export function coverAspect(notebook: PaperNotebook): number {
	if (isWhiteboard(notebook) || notebook.paperSize === 'board') return 1366 / 1024;
	const ratio = SIZE_RATIOS[(notebook.paperSize ?? '').toLowerCase()] ?? SIZE_RATIOS.a4;
	return notebook.orientation === 'landscape' ? 1 / ratio : ratio;
}

export function itemName(item: LibraryItem): string {
	return item.kind === 'folder'
		? item.folder.name
		: item.notebook.title || notebookTitleFromPath(item.notebook.path);
}

export function itemModified(item: LibraryItem): string | null | undefined {
	return item.kind === 'folder' ? item.folder.modified : item.notebook.modified;
}

/** A stamp as milliseconds. The server writes six fractional digits, which
 * not every engine's parser takes, so they are cut to three first. */
function timeOf(stamp: string | null | undefined): number {
	if (!stamp) return Number.NaN;
	return Date.parse(stamp.replace(/(\.\d{3})\d+/, '$1'));
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

const typeRank = (item: LibraryItem): number =>
	item.kind === 'folder' ? 0 : isWhiteboard(item.notebook) ? 2 : 1;

const byName = (a: LibraryItem, b: LibraryItem) => collator.compare(itemName(a), itemName(b));

/** Newest first; an item with no date goes last. */
function byDate(a: LibraryItem, b: LibraryItem): number {
	const ta = timeOf(itemModified(a));
	const tb = timeOf(itemModified(b));
	if (Number.isNaN(ta) || Number.isNaN(tb)) return Number(Number.isNaN(ta)) - Number(Number.isNaN(tb));
	return tb - ta;
}

/** One folder's rows as the iPad orders them (CONTRACT §3.6). Date and Name
 * interleave folders and documents; Type groups folders, then notebooks,
 * then whiteboards, each by name. The path settles anything still tied, so
 * the order never depends on the listing's. */
export function arrange(items: LibraryItem[], sort: LibrarySort, filter: LibraryFilter): LibraryItem[] {
	const kept = items.filter((item) => {
		switch (filter) {
			case 'folders':
				return item.kind === 'folder';
			case 'notebooks':
				return item.kind === 'document' && !isWhiteboard(item.notebook);
			case 'whiteboards':
				return item.kind === 'document' && isWhiteboard(item.notebook);
			default:
				return true;
		}
	});
	const last = (a: LibraryItem, b: LibraryItem) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
	const compare =
		sort === 'name'
			? (a: LibraryItem, b: LibraryItem) => byName(a, b) || byDate(a, b) || last(a, b)
			: sort === 'type'
				? (a: LibraryItem, b: LibraryItem) =>
						typeRank(a) - typeRank(b) || byName(a, b) || byDate(a, b) || last(a, b)
				: (a: LibraryItem, b: LibraryItem) => byDate(a, b) || byName(a, b) || last(a, b);
	return kept.sort(compare);
}

/** The twelve folder colours, as the web draws them (CONTRACT §2.1). */
export const FOLDER_COLORS: Record<string, string> = {
	red: '#FF3B30',
	orange: '#FF9500',
	yellow: '#FFCC00',
	green: '#34C759',
	mint: '#00C7BE',
	teal: '#30B0C7',
	blue: '#007AFF',
	indigo: '#5856D6',
	purple: '#AF52DE',
	pink: '#FF2D55',
	brown: '#A2845E',
	gray: '#8E8E93'
};

/** A folder colour's value; anything unknown is blue, as on every client. */
export function folderColorHex(color: string | null | undefined): string {
	return (color && Object.hasOwn(FOLDER_COLORS, color) && FOLDER_COLORS[color]) || FOLDER_COLORS.blue;
}

export type FolderIcon = { type: 'symbol'; name: string } | { type: 'emoji'; text: string };

/** A folder's icon. A symbol is an iPad system symbol the web cannot draw;
 * an emoji is the owner's own and the one emoji the product shows. Text that
 * is neither (ASCII, spaces, controls) is no icon at all. */
export function parseFolderIcon(icon: string | null | undefined): FolderIcon | null {
	if (!icon) return null;
	const at = icon.indexOf(':');
	const kind = icon.slice(0, at);
	const body = icon.slice(at + 1);
	if (kind === 'symbol') {
		return body.length <= 64 && /^[a-z0-9]+(\.[a-z0-9]+)*$/.test(body)
			? { type: 'symbol', name: body }
			: null;
	}
	if (kind === 'emoji') {
		const scalars = [...body];
		const ok =
			scalars.length >= 1 &&
			scalars.length <= 16 &&
			scalars.every((char) => (char.codePointAt(0) ?? 0) > 0x7f && !/[\s\p{Cc}]/u.test(char));
		return ok ? { type: 'emoji', text: body } : null;
	}
	return null;
}

/** The grey line under a row's name: "Sep 25, 2026 at 12:21 PM". */
export function formatLibraryDate(
	stamp: string | null | undefined,
	locale?: string,
	timeZone?: string
): string {
	const time = timeOf(stamp);
	if (Number.isNaN(time)) return '';
	const date = new Date(time);
	const day = date.toLocaleDateString(locale, {
		month: 'short',
		day: 'numeric',
		year: 'numeric',
		timeZone
	});
	const clock = date.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit', timeZone });
	return `${day} at ${clock}`;
}

/** A board's name: its title, or "Board N" by its place. */
export function boardName(page: Pick<PaperPage, 'title'>, index: number): string {
	return page.title?.trim() || `Board ${index + 1}`;
}

/** The caption under a sheet: "Page N" in a notebook, the board's name on a
 * whiteboard. */
export function sheetLabel(
	kind: string | null | undefined,
	page: Pick<PaperPage, 'title'>,
	index: number
): string {
	return kind === 'whiteboard' ? boardName(page, index) : `Page ${index + 1}`;
}

/** Paper colours (CONTRACT §3.4): what shows behind a render while it loads,
 * or instead of one. White is today's paper and stands in for any name this
 * build does not know. */
export const PAPER_COLORS: Record<string, string> = {
	white: '#FDFCFA',
	cream: '#F7F7EA',
	dark: '#2A2B2E',
	graphite: '#34373C',
	navy: '#23324A',
	forest: '#2E4636',
	oxblood: '#5B2A2E',
	ochre: '#B5852F',
	sage: '#8FA38A',
	sand: '#D9CBAE',
	slate: '#5C6B7A'
};

export function paperColorHex(name: string | null | undefined): string {
	return (name && Object.hasOwn(PAPER_COLORS, name) && PAPER_COLORS[name]) || PAPER_COLORS.white;
}

/** Relative luminance under 0.4, the iPad's rule for drawing a page dark. */
export function isDarkPaper(name: string | null | undefined): boolean {
	const hex = paperColorHex(name);
	const [r, g, b] = [1, 3, 5].map((at) => {
		const c = parseInt(hex.slice(at, at + 2), 16) / 255;
		return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
	});
	return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.4;
}
