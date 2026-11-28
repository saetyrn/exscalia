// Opening the media hub's OMNIA popover after a reload. The conversation is
// remembered by a localStorage pointer; opening must load it, and must never
// mint a thread of its own — that only happens when a message is sent.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const stub = vi.hoisted(() => ({
	getThread: vi.fn(),
	createThread: vi.fn()
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

const POINTER = 'exscalia.omnia.noteThread:media:hub';

const storage = new Map<string, string>();
vi.stubGlobal('localStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

/** A fresh store module per test, so the hub's cached chat starts empty. */
async function load() {
	vi.resetModules();
	return await import('./stores.svelte');
}

const THREAD = {
	id: 'thread-media',
	title: 'Media',
	created_at: '2026-09-20T18:00:00Z',
	updated_at: '2026-09-20T18:05:00Z',
	messages: [
		{
			id: 'm1',
			role: 'user' as const,
			text: 'What should I watch tonight?',
			created_at: '2026-09-20T18:00:00Z',
			status: 'complete' as const
		},
		{
			id: 'm2',
			role: 'assistant' as const,
			text: 'Something short.',
			created_at: '2026-09-20T18:00:05Z',
			status: 'complete' as const
		}
	]
};

describe('restoreMediaHubThread', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		storage.clear();
	});

	it('loads the remembered conversation', async () => {
		storage.set(POINTER, THREAD.id);
		stub.getThread.mockResolvedValue(THREAD);
		const { mediaHubChat, restoreMediaHubThread } = await load();

		await restoreMediaHubThread();
		expect(stub.getThread).toHaveBeenCalledWith(THREAD.id);
		expect(mediaHubChat().sessionId).toBe(THREAD.id);
		expect(mediaHubChat().messages.map((m) => m.text)).toEqual([
			'What should I watch tonight?',
			'Something short.'
		]);
		expect(stub.createThread).not.toHaveBeenCalled();
	});

	it('does nothing when no conversation was remembered', async () => {
		const { mediaHubChat, restoreMediaHubThread } = await load();

		await restoreMediaHubThread();
		expect(stub.getThread).not.toHaveBeenCalled();
		expect(stub.createThread).not.toHaveBeenCalled();
		expect(mediaHubChat().sessionId).toBe(null);
		expect(mediaHubChat().messages).toEqual([]);
	});

	it('does not reload once the conversation is showing', async () => {
		storage.set(POINTER, THREAD.id);
		stub.getThread.mockResolvedValue(THREAD);
		const { restoreMediaHubThread } = await load();

		await restoreMediaHubThread();
		await restoreMediaHubThread();
		expect(stub.getThread).toHaveBeenCalledTimes(1);
	});

	it('drops a pointer to a thread deleted elsewhere without making a new one', async () => {
		storage.set(POINTER, 'gone');
		const { mediaHubChat, restoreMediaHubThread } = await load();
		const { ApiError } = await import('$lib/api');
		stub.getThread.mockRejectedValue(new ApiError(404, 'Thread not found'));

		await restoreMediaHubThread();
		expect(storage.has(POINTER)).toBe(false);
		expect(stub.createThread).not.toHaveBeenCalled();
		expect(mediaHubChat().messages).toEqual([]);
	});

	it('keeps the pointer when the thread cannot be loaded right now', async () => {
		storage.set(POINTER, THREAD.id);
		const { restoreMediaHubThread } = await load();
		const { ApiError } = await import('$lib/api');
		stub.getThread.mockRejectedValue(new ApiError(503, 'Service Unavailable'));

		await restoreMediaHubThread();
		expect(storage.get(POINTER)).toBe(THREAD.id);
		expect(stub.createThread).not.toHaveBeenCalled();
	});
});
