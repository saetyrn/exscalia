// Signing in on desktop switches the cache to that server's file. When the
// file cannot open, the wrappers installed at startup must not be left over
// a closed cache: every note open and save threw 'sync db not opened' while
// the note list, which skips the cache online, still loaded.
import { describe, expect, it, vi } from 'vitest';

const cache = vi.hoisted(() => ({ open: true, fail: false }));

function must() {
	if (!cache.open) throw new Error('sync db not opened');
}

vi.mock('./db', () => ({
	openSyncDb: async () => {
		// Like switchTo: the open file closes before the next one opens.
		cache.open = false;
		if (cache.fail) throw new Error('cache file could not be migrated');
		cache.open = true;
	},
	isOpen: () => cache.open,
	getNote: async () => {
		must();
		return null;
	},
	adoptServer: async () => must(),
	insertNew: async () => must(),
	writeDirty: async () => must()
}));
vi.mock('./engine', () => ({
	isOffline: () => false,
	computeRevision: async () => 'rev',
	pushPath: async () => {},
	refreshPendingBadge: async () => {},
	schedulePush: () => {}
}));

const { api } = await import('../api');
const { installOfflineApi, netApi, openCacheFor } = await import('./offline-api');

describe('openCacheFor', () => {
	it('runs network-only after a sign-in whose cache cannot open', async () => {
		installOfflineApi();
		const readNote = vi.fn(async (path: string) => ({
			path,
			content: 'server text',
			meta: {},
			revision: 'r9'
		}));
		const saveNote = vi.fn(async (path: string, content: string) => ({
			path,
			content,
			meta: {},
			revision: 'r10',
			status: 'saved' as const
		}));
		netApi.readNote = readNote;
		netApi.saveNote = saveNote;

		cache.fail = true;
		await expect(openCacheFor('https://other.example')).rejects.toThrow('could not be migrated');

		await expect(api.readNote('plan.md')).resolves.toMatchObject({ content: 'server text' });
		await expect(api.saveNote('plan.md', 'mine', 'r9')).resolves.toMatchObject({
			status: 'saved',
			revision: 'r10'
		});
		expect(readNote).toHaveBeenCalledWith('plan.md');
		expect(saveNote).toHaveBeenCalledTimes(1);

		// A later sign-in that opens a cache puts the wrappers back.
		cache.fail = false;
		await openCacheFor('https://third.example');
		expect(api.readNote).not.toBe(netApi.readNote);
		await expect(api.readNote('plan.md')).resolves.toMatchObject({ content: 'server text' });
	});
});
