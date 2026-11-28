import { describe, expect, it } from 'vitest';
import type { PaperFolderRecord, PaperLibrary, PaperNotebook } from './api';
import {
	arrange,
	boardName,
	buildLibraryTree,
	childrenOf,
	coverAspect,
	findFolder,
	folderColorHex,
	formatLibraryDate,
	isDarkPaper,
	itemName,
	paperColorHex,
	parentFolder,
	parseFolderIcon,
	parseLibraryFilter,
	parseLibrarySort,
	sheetLabel,
	type LibraryItem
} from './paperlibrary';

function folder(path: string, extra: Partial<PaperFolderRecord> = {}): PaperFolderRecord {
	const parent = path.split('/').slice(0, -1).join('/') || 'paper';
	return {
		id: path.replace(/\W/g, '').padEnd(32, '0').slice(0, 32),
		name: path.split('/').pop() ?? path,
		color: 'blue',
		icon: null,
		favorite: false,
		created: '2026-09-01T10:00:00.000000Z',
		modified: '2026-09-01T10:00:00.000000Z',
		seq: 1,
		path,
		parent,
		...extra
	};
}

function notebook(path: string, extra: Partial<PaperNotebook> = {}): PaperNotebook {
	const folderPath = path.split('/').slice(0, -1).join('/');
	return {
		path,
		folder: folderPath,
		id: path,
		title: (path.split('/').pop() ?? '').replace(/\.paper$/, ''),
		modified: '2026-09-01T10:00:00Z',
		kind: 'notebook',
		pages: [],
		...extra
	};
}

const names = (items: LibraryItem[]) => items.map(itemName);

describe('buildLibraryTree', () => {
	const library: PaperLibrary = {
		folders: ['paper/Work', 'paper/Work/Accounting', 'paper/Empty'],
		folder_records: [
			folder('paper/Work'),
			folder('paper/Work/Accounting', { name: 'Accounting', color: 'red' }),
			folder('paper/Empty')
		],
		notebooks: [
			notebook('paper/Top.paper'),
			notebook('paper/Work/Plan.paper'),
			notebook('paper/Work/Accounting/Ledger.paper', { kind: 'whiteboard' })
		]
	};
	const tree = buildLibraryTree(library);

	it('puts folders and documents under their parents', () => {
		expect(names(childrenOf(tree, 'paper')).sort()).toEqual(['Empty', 'Top', 'Work']);
		expect(names(childrenOf(tree, 'paper/Work')).sort()).toEqual(['Accounting', 'Plan']);
		expect(names(childrenOf(tree, 'paper/Work/Accounting'))).toEqual(['Ledger']);
	});

	it('lists a folder with nothing in it, and finds nothing inside', () => {
		expect(childrenOf(tree, 'paper').some((item) => item.path === 'paper/Empty')).toBe(true);
		expect(childrenOf(tree, 'paper/Empty')).toEqual([]);
	});

	it('takes the folder path in any form, and an unknown one as empty', () => {
		expect(names(childrenOf(tree, 'Work/Accounting'))).toEqual(['Ledger']);
		expect(childrenOf(tree, 'paper/Nowhere')).toEqual([]);
	});

	it('finds a folder by path, ignoring case, and by id', () => {
		expect(findFolder(tree, 'paper/Work/Accounting')?.name).toBe('Accounting');
		expect(findFolder(tree, 'paper/work/accounting')?.path).toBe('paper/Work/Accounting');
		expect(findFolder(tree, 'paper')).toBeNull();
		expect(findFolder(tree, 'paper/Nowhere')).toBeNull();
		const id = findFolder(tree, 'paper/Work')!.id;
		expect(tree.byId.get(id)?.path).toBe('paper/Work');
	});

	// The released viewer documented `folder` as relative to `paper/`; the
	// server has always sent the vault path, `paper` at the top.
	it('reads a summary folder of `paper` as the top level', () => {
		const top = buildLibraryTree({
			folders: [],
			folder_records: [],
			notebooks: [notebook('paper/A.paper', { folder: 'paper' })]
		});
		expect(names(childrenOf(top, 'paper'))).toEqual(['A']);
	});

	it('stands in a plain folder for a directory that has no record', () => {
		const bare = buildLibraryTree({
			folders: ['paper/Old'],
			notebooks: [notebook('paper/Deep/Er/N.paper', { folder: undefined })]
		});
		expect(names(childrenOf(bare, 'paper')).sort()).toEqual(['Deep', 'Old']);
		expect(names(childrenOf(bare, 'paper/Deep'))).toEqual(['Er']);
		expect(names(childrenOf(bare, 'paper/Deep/Er'))).toEqual(['N']);
		const old = findFolder(bare, 'paper/Old');
		expect(old).toMatchObject({ name: 'Old', color: 'blue', icon: null, favorite: false });
	});

	it('is empty and safe before anything has loaded', () => {
		expect(childrenOf(buildLibraryTree(null), 'paper')).toEqual([]);
	});
});

describe('arrange', () => {
	const items: LibraryItem[] = [
		{ kind: 'document', path: 'paper/b.paper', notebook: notebook('paper/b.paper', { title: 'Chapter 10', modified: '2026-10-01T09:00:00Z' }) },
		{ kind: 'folder', path: 'paper/Accounting', folder: folder('paper/Accounting', { modified: '2026-09-09T11:20:00.000000Z' }) },
		{ kind: 'document', path: 'paper/w.paper', notebook: notebook('paper/w.paper', { title: 'Board', kind: 'whiteboard', modified: '2026-09-20T09:00:00Z' }) },
		{ kind: 'document', path: 'paper/a.paper', notebook: notebook('paper/a.paper', { title: 'chapter 9', modified: '2026-10-01T09:00:00Z' }) },
		{ kind: 'folder', path: 'paper/Econ', folder: folder('paper/Econ', { modified: '2026-09-30T08:25:00.000000Z' }) }
	];

	it('sorts by date newest first, folders and documents interleaved, ties by name', () => {
		expect(names(arrange(items, 'date', 'all'))).toEqual([
			'chapter 9',
			'Chapter 10',
			'Econ',
			'Board',
			'Accounting'
		]);
	});

	it('sorts by name, numbers as numbers and case aside, interleaved', () => {
		expect(names(arrange(items, 'name', 'all'))).toEqual([
			'Accounting',
			'Board',
			'chapter 9',
			'Chapter 10',
			'Econ'
		]);
	});

	it('breaks a name tie by date, newest first', () => {
		const twins: LibraryItem[] = [
			{ kind: 'document', path: 'paper/x.paper', notebook: notebook('paper/x.paper', { title: 'Same', modified: '2026-01-01T00:00:00Z' }) },
			{ kind: 'document', path: 'paper/y.paper', notebook: notebook('paper/y.paper', { title: 'Same', modified: '2026-02-01T00:00:00Z' }) }
		];
		expect(arrange(twins, 'name', 'all').map((item) => item.path)).toEqual([
			'paper/y.paper',
			'paper/x.paper'
		]);
	});

	it('sorts by type: folders, notebooks, whiteboards, each by name', () => {
		expect(names(arrange(items, 'type', 'all'))).toEqual([
			'Accounting',
			'Econ',
			'chapter 9',
			'Chapter 10',
			'Board'
		]);
	});

	it('filters to folders, notebooks or whiteboards', () => {
		expect(names(arrange(items, 'name', 'folders'))).toEqual(['Accounting', 'Econ']);
		expect(names(arrange(items, 'name', 'notebooks'))).toEqual(['chapter 9', 'Chapter 10']);
		expect(names(arrange(items, 'name', 'whiteboards'))).toEqual(['Board']);
	});

	it('leaves its input alone', () => {
		const before = items.map((item) => item.path);
		arrange(items, 'name', 'all');
		expect(items.map((item) => item.path)).toEqual(before);
	});

	it('puts an item with no date last', () => {
		const undated: LibraryItem[] = [
			{ kind: 'document', path: 'paper/u.paper', notebook: notebook('paper/u.paper', { title: 'A', modified: null }) },
			{ kind: 'document', path: 'paper/d.paper', notebook: notebook('paper/d.paper', { title: 'B' }) }
		];
		expect(names(arrange(undated, 'date', 'all'))).toEqual(['B', 'A']);
	});
});

describe('parentFolder', () => {
	it('goes up one folder, and stays at the top level', () => {
		expect(parentFolder('paper/Work/Accounting')).toBe('paper/Work');
		expect(parentFolder('paper/Work')).toBe('paper');
		expect(parentFolder('Work')).toBe('paper');
		expect(parentFolder('paper')).toBe('paper');
	});
});

describe('coverAspect', () => {
	it('reserves a page-shaped box for a notebook, turned for landscape', () => {
		expect(coverAspect(notebook('paper/a.paper', { paperSize: 'a4' }))).toBeCloseTo(595 / 842);
		expect(
			coverAspect(notebook('paper/a.paper', { paperSize: 'a4', orientation: 'landscape' }))
		).toBeCloseTo(842 / 595);
		expect(coverAspect(notebook('paper/a.paper', { paperSize: 'iPad' }))).toBeCloseTo(1024 / 1366);
		expect(coverAspect(notebook('paper/a.paper', { paperSize: 'scroll' }))).toBeCloseTo(595 / 842);
	});

	it('reserves the empty board picture for a whiteboard', () => {
		expect(
			coverAspect(notebook('paper/w.paper', { kind: 'whiteboard', paperSize: 'board' }))
		).toBeCloseTo(1366 / 1024);
	});
});

describe('stored choices', () => {
	it('reads the sort and filter keys, defaulting what it does not know', () => {
		expect(parseLibrarySort('name')).toBe('name');
		expect(parseLibrarySort('size')).toBe('date');
		expect(parseLibrarySort(null)).toBe('date');
		expect(parseLibraryFilter('whiteboards')).toBe('whiteboards');
		expect(parseLibraryFilter('tags')).toBe('all');
		expect(parseLibraryFilter(undefined)).toBe('all');
	});
});

describe('itemName', () => {
	it('falls back to the path when a notebook has no title', () => {
		const item: LibraryItem = {
			kind: 'document',
			path: 'paper/Untitled.paper',
			notebook: notebook('paper/Untitled.paper', { title: '' })
		};
		expect(itemName(item)).toBe('Untitled');
	});
});

describe('folder appearance', () => {
	it('gives every folder colour its web value, and blue for anything else', () => {
		expect(folderColorHex('red')).toBe('#FF3B30');
		expect(folderColorHex('orange')).toBe('#FF9500');
		expect(folderColorHex('gray')).toBe('#8E8E93');
		expect(folderColorHex('chartreuse')).toBe('#007AFF');
		expect(folderColorHex(undefined)).toBe('#007AFF');
	});

	it('parses an icon into a symbol or an emoji', () => {
		expect(parseFolderIcon('emoji:📒')).toEqual({ type: 'emoji', text: '📒' });
		expect(parseFolderIcon('symbol:book.closed')).toEqual({ type: 'symbol', name: 'book.closed' });
		expect(parseFolderIcon(null)).toBeNull();
		expect(parseFolderIcon('')).toBeNull();
	});

	// The emoji is the owner's content; anything that would put text on a
	// thumbnail instead is not one.
	it('refuses an icon that is not one', () => {
		expect(parseFolderIcon('emoji:hi')).toBeNull();
		expect(parseFolderIcon('emoji:')).toBeNull();
		expect(parseFolderIcon('emoji:📒 📒')).toBeNull();
		expect(parseFolderIcon('symbol:Book')).toBeNull();
		expect(parseFolderIcon('symbol:')).toBeNull();
		expect(parseFolderIcon('picture:cat')).toBeNull();
	});
});

describe('formatLibraryDate', () => {
	const plain = (s: string) => s.replace(/\s/g, ' ');

	it('reads like the iPad library: month, day, year at time', () => {
		expect(plain(formatLibraryDate('2026-09-25T12:21:00Z', 'en-US', 'UTC'))).toBe(
			'Sep 25, 2026 at 12:21 PM'
		);
	});

	it('takes the server stamps with microseconds', () => {
		expect(plain(formatLibraryDate('2026-09-09T11:20:00.000000Z', 'en-US', 'UTC'))).toBe(
			'Sep 9, 2026 at 11:20 AM'
		);
	});

	it('says nothing for no date', () => {
		expect(formatLibraryDate(null)).toBe('');
		expect(formatLibraryDate('')).toBe('');
		expect(formatLibraryDate('not a date')).toBe('');
	});
});

describe('sheets', () => {
	it('names a board by its title, or Board N', () => {
		expect(boardName({ title: 'Plan' }, 0)).toBe('Plan');
		expect(boardName({ title: null }, 2)).toBe('Board 3');
		expect(boardName({ title: '  ' }, 0)).toBe('Board 1');
	});

	it('labels a page by its number and a board by its name', () => {
		expect(sheetLabel('notebook', { title: 'Intro' }, 1)).toBe('Page 2');
		expect(sheetLabel('whiteboard', { title: 'Plan' }, 1)).toBe('Plan');
		expect(sheetLabel('whiteboard', {}, 1)).toBe('Board 2');
	});

	it('colours paper by name, white when unnamed or unknown', () => {
		expect(paperColorHex('cream')).toBe('#F7F7EA');
		expect(paperColorHex(undefined)).toBe('#FDFCFA');
		expect(paperColorHex('plaid')).toBe('#FDFCFA');
	});

	it('knows which papers are dark', () => {
		expect(isDarkPaper('dark')).toBe(true);
		expect(isDarkPaper('navy')).toBe(true);
		expect(isDarkPaper('white')).toBe(false);
		expect(isDarkPaper('cream')).toBe(false);
		expect(isDarkPaper('sand')).toBe(false);
		expect(isDarkPaper(undefined)).toBe(false);
	});
});
