// Opening today's daily note: the device's calendar names it, a note that is
// already in the list opens with no request in front of the navigation, and
// the first press of the day creates it without waiting on the note list.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const stub = vi.hoisted(() => ({
	openDaily: vi.fn(async (date?: string) => ({ path: `daily/${date}.md`, created: true })),
	listNotes: vi.fn(async () => []),
	listFolders: vi.fn(async () => [])
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

const goto = vi.hoisted(() => vi.fn(async () => {}));
vi.mock('$app/navigation', () => ({ goto }));

// The stores and tabs persist to localStorage, which node does not have.
const storage = new Map<string, string>();
vi.stubGlobal('localStorage', {
	getItem: (key: string) => storage.get(key) ?? null,
	setItem: (key: string, value: string) => void storage.set(key, value),
	removeItem: (key: string) => void storage.delete(key)
});

const { vault } = await import('./stores.svelte');
const { openToday, todayPath } = await import('./daily');

const note = (path: string) => ({ path, name: path.split('/').pop()!, pinned: false });

describe('todayPath', () => {
	it('names the note by the local calendar day, zero-padded', () => {
		expect(todayPath(new Date(2026, 0, 5, 9, 0))).toBe('daily/2026-01-05.md');
	});

	it('uses the local day, not UTC, late in the evening', () => {
		expect(todayPath(new Date(2026, 8, 23, 23, 59))).toBe('daily/2026-09-23.md');
	});
});

describe('openToday', () => {
	beforeEach(() => {
		vi.clearAllMocks();
		vi.useFakeTimers({ toFake: ['Date'] });
		vi.setSystemTime(new Date(2026, 8, 23, 23, 50));
		vault.notes = [];
	});

	afterEach(() => {
		vi.useRealTimers();
	});

	it('navigates straight to a note the list already has', async () => {
		vault.notes = [note('ideas.md'), note('daily/2026-09-23.md')];

		await expect(openToday()).resolves.toBe('daily/2026-09-23.md');

		expect(stub.openDaily).not.toHaveBeenCalled();
		expect(stub.listNotes).not.toHaveBeenCalled();
		expect(goto).toHaveBeenCalledWith('/note/daily/2026-09-23.md');
	});

	it("creates a missing note for the device's date before navigating", async () => {
		vault.notes = [note('daily/2026-09-22.md')];

		await expect(openToday()).resolves.toBe('daily/2026-09-23.md');

		expect(stub.openDaily).toHaveBeenCalledWith('2026-09-23');
		expect(goto).toHaveBeenCalledWith('/note/daily/2026-09-23.md');
		expect(stub.openDaily.mock.invocationCallOrder[0]).toBeLessThan(
			goto.mock.invocationCallOrder[0]
		);
		expect(stub.listNotes).toHaveBeenCalled();
	});

	it('does not hold the navigation for the note-list refresh', async () => {
		stub.listNotes.mockReturnValueOnce(new Promise(() => {}));

		await expect(openToday()).resolves.toBe('daily/2026-09-23.md');

		expect(goto).toHaveBeenCalledWith('/note/daily/2026-09-23.md');
	});
});
