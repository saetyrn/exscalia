// Recipes are network-only on desktop, like media and flashcards: the offline
// wrappers must leave every recipes call as the plain network method. A
// recipe is still a note, so the sync engine carries the file itself.
import { describe, expect, it, vi } from 'vitest';

vi.mock('./db', () => ({
	openSyncDb: async () => {},
	isOpen: () => true
}));
vi.mock('./engine', () => ({
	isOffline: () => true,
	computeRevision: async () => 'rev',
	pushPath: async () => {},
	refreshPendingBadge: async () => {},
	schedulePush: () => {}
}));

const { api } = await import('../api');
const { installOfflineApi, netApi } = await import('./offline-api');

describe('offline api', () => {
	it('passes the recipes calls through untouched', () => {
		installOfflineApi();
		const names = Object.keys(api).filter((name) => name.startsWith('recipes')) as (keyof typeof api)[];
		expect(names.sort()).toEqual([
			'recipesArtStatus',
			'recipesCreate',
			'recipesDelete',
			'recipesGet',
			'recipesImport',
			'recipesImportFiles',
			'recipesList',
			'recipesOrder',
			'recipesStartArt',
			'recipesStatus',
			'recipesUpdate'
		]);
		for (const name of names) expect(api[name]).toBe(netApi[name]);
	});
});
