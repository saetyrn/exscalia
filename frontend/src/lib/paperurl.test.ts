import { describe, expect, it } from 'vitest';
import {
	folderHref,
	notebookHref,
	notebookTitleFromPath,
	pageHref,
	durablePageHref,
	paperFolderPath,
	paperLibraryTitle,
	paperRoutePath,
	paperVaultPath,
	parseFolderParam,
	parsePaperHash,
	parsePaperRegion,
	regionOnSheet
} from './paperurl';

describe('paperVaultPath', () => {
	it('adds the root prefix and keeps an existing one', () => {
		expect(paperVaultPath('Work/Ideas.paper')).toBe('paper/Work/Ideas.paper');
		expect(paperVaultPath('paper/Work/Ideas.paper')).toBe('paper/Work/Ideas.paper');
	});

	it('trims stray slashes so the API path never doubles them', () => {
		expect(paperVaultPath('/Work/Ideas.paper/')).toBe('paper/Work/Ideas.paper');
	});

	// A notebook literally named "papers" must not be mistaken for the root.
	it('only treats the exact root segment as a prefix', () => {
		expect(paperVaultPath('papers/Ideas.paper')).toBe('paper/papers/Ideas.paper');
	});
});

describe('paperRoutePath', () => {
	it('strips the root either way round', () => {
		expect(paperRoutePath('paper/Work/Ideas.paper')).toBe('Work/Ideas.paper');
		expect(paperRoutePath('Work/Ideas.paper')).toBe('Work/Ideas.paper');
	});
});

describe('notebookHref', () => {
	it('encodes each segment, keeping the separators', () => {
		expect(notebookHref('paper/Work Notes/Big Ideas.paper')).toBe(
			'/paper/Work%20Notes/Big%20Ideas.paper'
		);
	});

	it('encodes characters that would otherwise end the path', () => {
		expect(notebookHref('paper/A#B.paper')).toBe('/paper/A%23B.paper');
		expect(notebookHref('paper/A?B.paper')).toBe('/paper/A%3FB.paper');
	});

	it('falls back to the library for an empty path', () => {
		expect(notebookHref('paper')).toBe('/paper');
		expect(notebookHref('')).toBe('/paper');
	});
});

describe('pageHref', () => {
	it('hangs the page id off the notebook href', () => {
		expect(pageHref('paper/Work/Ideas.paper', 'a1b2')).toBe('/paper/Work/Ideas.paper#a1b2');
	});

	it('omits an empty page id', () => {
		expect(pageHref('paper/Work/Ideas.paper', '')).toBe('/paper/Work/Ideas.paper');
	});
});

describe('durable Paper links', () => {
	it('uses stable ids and preserves an optional region', () => {
		expect(durablePageHref('notebook-1', 'page-1', '1,2,30,40')).toBe(
			'/paper/id/notebook-1/page-1?region=1%2C2%2C30%2C40'
		);
	});

	it('parses only numeric four-value regions', () => {
		expect(parsePaperRegion('?region=1,2,30,40')).toBe('1,2,30,40');
		expect(parsePaperRegion('?region=bad')).toBeNull();
	});
});

describe('parsePaperHash', () => {
	it('reads the id with or without the leading hash', () => {
		expect(parsePaperHash('#a1b2')).toBe('a1b2');
		expect(parsePaperHash('a1b2')).toBe('a1b2');
	});

	it('decodes and rejects the empty cases', () => {
		expect(parsePaperHash('#a%20b')).toBe('a b');
		expect(parsePaperHash('#')).toBeNull();
		expect(parsePaperHash('')).toBeNull();
		expect(parsePaperHash(null)).toBeNull();
		expect(parsePaperHash(undefined)).toBeNull();
	});

	it('survives a malformed escape rather than throwing at the router', () => {
		expect(parsePaperHash('#a%zz')).toBe('a%zz');
	});
});

describe('notebookTitleFromPath', () => {
	it('takes the last segment without the package suffix', () => {
		expect(notebookTitleFromPath('paper/Work/Ideas.paper')).toBe('Ideas');
		expect(notebookTitleFromPath('Ideas.paper')).toBe('Ideas');
		expect(notebookTitleFromPath('paper/Work/Ideas.PAPER')).toBe('Ideas');
	});

	it('names the section itself when there is no notebook', () => {
		expect(notebookTitleFromPath('paper')).toBe('Paper');
		expect(notebookTitleFromPath('')).toBe('Paper');
	});
});

describe('paperFolderPath', () => {
	it('keeps the root prefix and names the top level `paper`', () => {
		expect(paperFolderPath('paper/Work/Accounting')).toBe('paper/Work/Accounting');
		expect(paperFolderPath('Work/Accounting')).toBe('paper/Work/Accounting');
		expect(paperFolderPath('paper')).toBe('paper');
		expect(paperFolderPath('')).toBe('paper');
		expect(paperFolderPath(null)).toBe('paper');
	});

	it('drops stray and doubled slashes', () => {
		expect(paperFolderPath('/paper//Work/')).toBe('paper/Work');
		expect(paperFolderPath('/')).toBe('paper');
	});
});

describe('folderHref', () => {
	// OMNIA opens a folder at exactly this string, and the tab strip dedupes
	// on exact URLs: `encodeURIComponent` of the vault path, `paper/` kept.
	it('is the library with the vault path as one encoded component', () => {
		expect(folderHref('paper/Work/Accounting')).toBe('/paper?folder=paper%2FWork%2FAccounting');
		expect(folderHref('Work/Accounting')).toBe('/paper?folder=paper%2FWork%2FAccounting');
	});

	it('encodes as encodeURIComponent does, never as a form', () => {
		expect(folderHref('paper/Tax & Fees 2026+')).toBe(
			'/paper?folder=paper%2FTax%20%26%20Fees%202026%2B'
		);
		expect(folderHref('paper/Ideen 📒')).toBe(`/paper?folder=${encodeURIComponent('paper/Ideen 📒')}`);
	});

	it('is the bare library at the top level', () => {
		expect(folderHref('paper')).toBe('/paper');
		expect(folderHref('')).toBe('/paper');
	});
});

describe('parseFolderParam', () => {
	it('reads the folder back out of a search string or a whole URL', () => {
		expect(parseFolderParam('?folder=paper%2FWork%2FAccounting')).toBe('paper/Work/Accounting');
		expect(parseFolderParam('/paper?folder=paper%2FWork#x')).toBe('paper/Work');
		expect(parseFolderParam(new URLSearchParams('folder=paper%2FWork'))).toBe('paper/Work');
	});

	it('round-trips every href folderHref builds', () => {
		for (const path of ['paper/Work', 'paper/Tax & Fees 2026+', 'paper/a/b/c', 'paper/Ideen 📒']) {
			expect(parseFolderParam(folderHref(path))).toBe(path);
		}
	});

	it('accepts a path without the root, and means the top level otherwise', () => {
		expect(parseFolderParam('?folder=Work')).toBe('paper/Work');
		expect(parseFolderParam('?folder=')).toBe('paper');
		expect(parseFolderParam('')).toBe('paper');
		expect(parseFolderParam(null)).toBe('paper');
		expect(parseFolderParam('?other=1')).toBe('paper');
	});

	it('never throws on a malformed escape', () => {
		expect(() => parseFolderParam('?folder=paper%2FA%zz')).not.toThrow();
	});
});

describe('paperLibraryTitle', () => {
	it('names the open folder after the section', () => {
		expect(paperLibraryTitle('/paper?folder=paper%2FWork%2FAccounting')).toBe('Paper · Accounting');
		expect(paperLibraryTitle('/paper')).toBe('Paper');
		expect(paperLibraryTitle('/paper?folder=paper')).toBe('Paper');
	});
});

describe('regionOnSheet', () => {
	it('places a region on a page as fractions of its size', () => {
		const box = regionOnSheet('59.5,84.2,119,168.4', { size: { width: 595, height: 842 } });
		expect(box?.left).toBeCloseTo(0.1);
		expect(box?.top).toBeCloseTo(0.1);
		expect(box?.width).toBeCloseTo(0.2);
		expect(box?.height).toBeCloseTo(0.2);
	});

	it('prefers the exact extent of a grown or imported page', () => {
		const box = regionOnSheet('0,1000,512,500', {
			size: { width: 1024, height: 1366 },
			size_points: { width: 1024, height: 2000 }
		});
		expect(box?.top).toBeCloseTo(0.5);
		expect(box?.width).toBeCloseTo(0.5);
	});

	// A board's picture shows only its render rectangle, somewhere inside a
	// 16,384-point canvas: left = (x - rect.x) / rect.width.
	it('maps a board region through the render rectangle', () => {
		const board = {
			size_name: 'board',
			size: { width: 2000, height: 1000 },
			board_extent: { width: 16384, height: 16384 },
			render_rect: { x: 7000, y: 7500, width: 2000, height: 1000 }
		};
		const box = regionOnSheet('7500,7750,500,250', board);
		expect(box?.left).toBeCloseTo(0.25);
		expect(box?.top).toBeCloseTo(0.25);
		expect(box?.width).toBeCloseTo(0.25);
		expect(box?.height).toBeCloseTo(0.25);
	});

	it('uses the whole extent for a board with no render yet', () => {
		const box = regionOnSheet('8192,4096,1024,1024', {
			size_name: 'board',
			size: { width: 16384, height: 16384 },
			board_extent: { width: 16384, height: 16384 }
		});
		expect(box?.left).toBeCloseTo(0.5);
		expect(box?.top).toBeCloseTo(0.25);
	});

	it('clips to the picture, and drops a region wholly outside it', () => {
		const board = {
			size_name: 'board',
			render_rect: { x: 1000, y: 1000, width: 1000, height: 1000 }
		};
		const clipped = regionOnSheet('500,1500,1000,100', board);
		expect(clipped?.left).toBe(0);
		expect(clipped?.width).toBeCloseTo(0.5);
		expect(regionOnSheet('0,0,100,100', board)).toBeNull();
	});

	it('rejects what is not a region', () => {
		expect(regionOnSheet(null, { size: { width: 595, height: 842 } })).toBeNull();
		expect(regionOnSheet('1,2,3', { size: { width: 595, height: 842 } })).toBeNull();
		expect(regionOnSheet('1,2,0,0', { size: { width: 595, height: 842 } })).toBeNull();
	});
});
