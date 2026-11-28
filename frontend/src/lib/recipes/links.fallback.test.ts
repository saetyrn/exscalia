// A note in the recipes folder without the recipe mark is an ordinary note,
// but every list links it like a recipe (noteHref can only see the path).
// Its recipe page hands it to the editor instead of saying "not found".
import { beforeEach, describe, expect, it, vi } from 'vitest';

const server = vi.hoisted(() => ({ readNote: vi.fn() }));
vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, readNote: server.readNote } };
});

const { ApiError } = await import('$lib/api');
const { ordinaryNoteHref } = await import('./links');

beforeEach(() => server.readNote.mockReset());

describe('ordinaryNoteHref', () => {
	it('sends a note that is there to the editor', async () => {
		server.readNote.mockResolvedValue({ path: 'recipes/shopping list.md' });
		expect(await ordinaryNoteHref('recipes/shopping list.md')).toBe('/note/recipes/shopping%20list.md');
		expect(server.readNote).toHaveBeenCalledWith('recipes/shopping list.md');
	});

	it('answers null for a note that is not there, or a read that fails', async () => {
		server.readNote.mockRejectedValueOnce(new ApiError(404, 'Not found'));
		expect(await ordinaryNoteHref('recipes/gone.md')).toBeNull();
		server.readNote.mockRejectedValueOnce(new ApiError(0, 'Server unreachable'));
		expect(await ordinaryNoteHref('recipes/gone.md')).toBeNull();
	});
});
