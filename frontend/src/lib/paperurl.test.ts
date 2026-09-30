import { describe, expect, it } from 'vitest';
import {
	notebookHref,
	notebookTitleFromPath,
	pageHref,
	durablePageHref,
	paperRoutePath,
	paperVaultPath,
	parsePaperHash,
	parsePaperRegion
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
