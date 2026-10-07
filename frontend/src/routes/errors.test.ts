// What the error page gets to show. The desktop release build and the iPad
// shell have no console, so a failed init step has to name itself in the
// message, and an unexpected error has to keep its own message rather than
// SvelteKit's 'Internal Error'. The offline cache is the exception: without
// it the app still runs network-only, and the reason goes to Settings > Sync
// (syncState.lastError) instead of an error page.
import { isHttpError } from '@sveltejs/kit';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const stub = vi.hoisted(() => ({
	initRuntime: vi.fn(),
	openSyncDb: vi.fn(),
	installOfflineApi: vi.fn()
}));

vi.mock('$lib/runtime', () => ({
	initRuntime: stub.initRuntime,
	isDesktop: true,
	apiBase: () => 'http://127.0.0.1:8600'
}));
vi.mock('$lib/sync/db', () => ({ openSyncDb: stub.openSyncDb }));
vi.mock('$lib/sync/offline-api', () => ({ installOfflineApi: stub.installOfflineApi }));

const { load } = await import('./+layout');
const { handleError } = await import('../hooks.client');
const { syncState } = await import('$lib/stores.svelte');

/** The 500 load() rejected with, or a failure if it resolved or threw anything else. */
async function loadFailure() {
	try {
		await load();
	} catch (e) {
		if (isHttpError(e)) return { status: e.status, message: e.body.message };
		throw e;
	}
	throw new Error('load() resolved');
}

describe('root load on desktop', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.spyOn(console, 'error').mockImplementation(() => {});
		stub.initRuntime.mockResolvedValue(undefined);
		stub.openSyncDb.mockResolvedValue(undefined);
		stub.installOfflineApi.mockReturnValue(undefined);
		syncState.lastError = '';
	});

	it('runs every step in order when nothing fails', async () => {
		await load();
		expect(stub.openSyncDb).toHaveBeenCalledWith('http://127.0.0.1:8600');
		expect(syncState.lastError).toBe('');
		expect(stub.initRuntime).toHaveBeenCalledOnce();
		expect(stub.openSyncDb).toHaveBeenCalledOnce();
		expect(stub.installOfflineApi).toHaveBeenCalledOnce();
		expect(stub.initRuntime.mock.invocationCallOrder[0]).toBeLessThan(
			stub.openSyncDb.mock.invocationCallOrder[0]
		);
		expect(stub.openSyncDb.mock.invocationCallOrder[0]).toBeLessThan(
			stub.installOfflineApi.mock.invocationCallOrder[0]
		);
	});

	it('names the settings store and stops there', async () => {
		stub.initRuntime.mockRejectedValue(new Error('settings.json: permission denied'));
		expect(await loadFailure()).toEqual({
			status: 500,
			message: 'settings store: settings.json: permission denied'
		});
		expect(stub.openSyncDb).not.toHaveBeenCalled();
		expect(stub.installOfflineApi).not.toHaveBeenCalled();
	});

	it('runs network-only when the offline database does not open', async () => {
		const failure = new Error('database is locked');
		stub.openSyncDb.mockRejectedValue(failure);
		await load();
		expect(stub.installOfflineApi).not.toHaveBeenCalled();
		expect(syncState.lastError).toBe('database is locked');
		expect(console.error).toHaveBeenCalledWith(failure);
	});

	it('runs network-only when the offline api does not install, including a synchronous throw', async () => {
		stub.installOfflineApi.mockImplementation(() => {
			throw new Error('fetch is not writable');
		});
		await load();
		expect(syncState.lastError).toBe('fetch is not writable');
	});

	it('keeps a non-Error cache rejection readable and logs the original', async () => {
		stub.openSyncDb.mockRejectedValue('no such table: notes');
		await load();
		expect(syncState.lastError).toBe('no such table: notes');
		expect(console.error).toHaveBeenCalledWith('no such table: notes');
	});
});

describe('client handleError', () => {
	const event = {} as Parameters<typeof handleError>[0]['event'];

	beforeEach(() => {
		vi.spyOn(console, 'error').mockImplementation(() => {});
	});

	it('passes an Error message through instead of Internal Error', () => {
		const error = new TypeError("Cannot read properties of undefined (reading 'path')");
		expect(handleError({ error, event, status: 500, message: 'Internal Error' })).toEqual({
			message: "Cannot read properties of undefined (reading 'path')"
		});
		expect(console.error).toHaveBeenCalledWith(error);
	});

	it("falls back to SvelteKit's message for a non-Error throw", () => {
		expect(handleError({ error: 42, event, status: 500, message: 'Internal Error' })).toEqual({
			message: 'Internal Error'
		});
	});
});
