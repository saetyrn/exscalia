// The Gallery is network-only on desktop, like media and recipes: the offline
// wrappers must leave every gallery call as the plain network method. A
// picture is still a note, so the sync engine carries the file itself.
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
	it('passes the gallery calls through untouched', () => {
		installOfflineApi();
		const names = Object.keys(api).filter((name) =>
			name.startsWith('gallery')
		) as (keyof typeof api)[];
		expect(names.length).toBeGreaterThan(10);
		for (const name of names) expect(api[name]).toBe(netApi[name]);
	});
});
