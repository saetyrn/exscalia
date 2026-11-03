// The recipe cache following the vault's live events: which events refresh a
// recipe, which only the menu lists, and how a burst becomes one refresh.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LiveNoteEvent } from '$lib/live';
import type { Recipe, RecipeCard, RecipeList } from './types';
import { emptyRecipe } from './types';

const server = vi.hoisted(() => ({
	recipesGet: vi.fn(),
	recipesList: vi.fn()
}));
vi.mock('$lib/api', () => {
	class ApiError extends Error {
		constructor(
			readonly status: number,
			detail: string
		) {
			super(detail);
		}
	}
	return { api: server, ApiError };
});

const { ApiError } = await import('$lib/api');
const { CLIENT_ID } = await import('$lib/live');
const cache = await import('./cache.svelte');
const { watchRecipesLive } = await import('./live.svelte');

const recipe = (path: string, revision = 'r1', title = 'Pie'): Recipe => ({
	...emptyRecipe(),
	title,
	path,
	revision,
	created: '2026-10-04T12:00:00Z',
	modified: '2026-10-04T12:00:00Z',
	art: '',
	art_colors: '',
	art_palette: {}
});

const card = (path: string): RecipeCard => ({
	path,
	title: path,
	total_minutes: null,
	thumbnail: null,
	tags: [],
	modified: '2026-10-04T12:00:00Z',
	art: '',
	art_colors: '',
	art_palette: {}
});

const DELAY = 500;
let target: EventTarget;
let stop: () => void;
/** The pathname on screen. */
let here = '/recipes';

function fire(detail: Partial<LiveNoteEvent>) {
	target.dispatchEvent(new CustomEvent('exscalia:live', { detail }));
}

/** What the OMNIA chat dispatches after an action. */
function omnia(detail: { refresh?: string[]; paths?: string[] } | null) {
	target.dispatchEvent(new CustomEvent('exscalia:data-changed', { detail }));
}

/** What the root layout dispatches when the live stream comes back. */
function resync() {
	target.dispatchEvent(new CustomEvent('exscalia:data-changed'));
}

/** Let the burst fall due and every refresh it started settle. */
async function settle() {
	await vi.advanceTimersByTimeAsync(DELAY);
	await vi.runAllTimersAsync();
}

beforeEach(async () => {
	vi.useFakeTimers();
	cache.clearRecipeCache();
	server.recipesGet.mockReset();
	server.recipesList.mockReset();
	const list: RecipeList = { recipes: [card('recipes/pie.md'), card('recipes/tart.md')], tags: [] };
	server.recipesList.mockResolvedValue(list);
	server.recipesGet.mockImplementation(async (path: string) => recipe(path, 'r2', 'Apple pie'));
	await cache.loadRecipes();
	cache.rememberRecipe(recipe('recipes/pie.md'));
	server.recipesList.mockClear();
	target = new EventTarget();
	here = '/recipes';
	stop = watchRecipesLive({ target, delay: DELAY, pathname: () => here });
});

afterEach(() => {
	stop();
	vi.useRealTimers();
});

describe('watchRecipesLive', () => {
	it('turns a burst of writes into one recipe fetch and one list refresh', async () => {
		for (let i = 0; i < 10; i += 1)
			fire({ type: 'note', path: 'recipes/pie.md', revision: `r${i + 2}`, origin: null });
		await vi.advanceTimersByTimeAsync(DELAY - 1);
		expect(server.recipesGet).not.toHaveBeenCalled();
		await settle();
		expect(server.recipesGet).toHaveBeenCalledTimes(1);
		expect(server.recipesGet).toHaveBeenCalledWith('recipes/pie.md');
		expect(server.recipesList).toHaveBeenCalledTimes(1);
		expect(cache.cachedRecipe('recipes/pie.md')?.title).toBe('Apple pie');
	});

	it('refreshes every cached list, each search and tag', async () => {
		await cache.loadRecipes('', 'quick');
		server.recipesList.mockClear();
		fire({ type: 'note', path: 'recipes/tart.md', revision: 'r9', origin: null });
		await settle();
		expect(server.recipesList.mock.calls).toEqual([
			['', ''],
			['', 'quick']
		]);
	});

	it('leaves a recipe it has not cached to its page, but still refreshes the lists', async () => {
		fire({ type: 'note', path: 'recipes/tart.md', revision: 'r1', origin: null });
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
		expect(server.recipesList).toHaveBeenCalledTimes(1);
	});

	it('takes the echo of this tab’s own save as already on screen', async () => {
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: CLIENT_ID });
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
		// The card may have a new title, and nothing else puts it in the menu.
		expect(server.recipesList).toHaveBeenCalledTimes(1);
	});

	it('still fetches when another writer joined the burst', async () => {
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: null });
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r3', origin: CLIENT_ID });
		await settle();
		expect(server.recipesGet).toHaveBeenCalledTimes(1);
	});

	it('skips a revision the cache already holds', async () => {
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r1', origin: 'other-device' });
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
	});

	it('ignores notes outside the recipes folder', async () => {
		fire({ type: 'note', path: 'media/pie.md', revision: 'r2', origin: null });
		fire({ type: 'note', path: 'recipes-old/pie.md', revision: 'r2', origin: null });
		fire({ type: 'note_removed', path: 'pie.md', origin: null });
		fire({ type: 'paper', path: 'recipes/pie.md' });
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
		expect(server.recipesList).not.toHaveBeenCalled();
		expect(cache.cachedRecipe('recipes/pie.md')).toBeDefined();
	});

	it('drops a removed recipe and its card at once', async () => {
		fire({ type: 'note_removed', path: 'recipes/pie.md', origin: 'other-device' });
		expect(cache.cachedRecipe('recipes/pie.md')).toBeUndefined();
		expect(cache.cachedList()?.recipes.map((c) => c.path)).toEqual(['recipes/tart.md']);
		await settle();
		expect(server.recipesList).toHaveBeenCalledTimes(1);
	});

	it('does not fetch a recipe removed later in the same burst', async () => {
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: null });
		fire({ type: 'note_removed', path: 'recipes/pie.md', origin: null });
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
	});

	it('brings back a removed recipe written again (restored from the Trash)', async () => {
		fire({ type: 'note_removed', path: 'recipes/pie.md', origin: 'other-device' });
		await settle();
		expect(cache.recipeGone('recipes/pie.md')).toBe(true);
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: 'other-device' });
		await settle();
		expect(server.recipesGet).toHaveBeenCalledExactlyOnceWith('recipes/pie.md');
		expect(cache.recipeGone('recipes/pie.md')).toBe(false);
		expect(cache.cachedRecipe('recipes/pie.md')?.revision).toBe('r2');
	});

	it('forgets a recipe the server no longer serves', async () => {
		server.recipesGet.mockRejectedValueOnce(new ApiError(404, 'Not a recipe'));
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: null });
		await settle();
		expect(cache.cachedRecipe('recipes/pie.md')).toBeUndefined();
	});

	it('keeps the cached copy when a refresh fails for another reason', async () => {
		server.recipesGet.mockRejectedValueOnce(new ApiError(503, 'Offline'));
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: null });
		await settle();
		expect(cache.cachedRecipe('recipes/pie.md')?.revision).toBe('r1');
	});

	it('drops the old path of a recipe moved away', async () => {
		fire({ type: 'note_moved', path: 'recipes/pie.md', new_path: 'desserts/pie.md', origin: null });
		expect(cache.cachedRecipe('recipes/pie.md')).toBeUndefined();
		await settle();
		expect(server.recipesList).toHaveBeenCalledTimes(1);
	});

	it('refreshes the lists for an OMNIA reorder, which writes no note', async () => {
		omnia({ refresh: ['recipes'], paths: [] });
		await settle();
		expect(server.recipesList).toHaveBeenCalledTimes(1);
		expect(server.recipesGet).not.toHaveBeenCalled();
	});

	it('refreshes a cached recipe an OMNIA action names, in the same burst as its write', async () => {
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: null });
		omnia({ refresh: ['notes'], paths: ['recipes/pie.md', 'recipes/tart.md', 'Welcome.md'] });
		omnia({ refresh: ['recipes', 'notes'], paths: ['recipes/pie.md'] });
		await settle();
		expect(server.recipesGet).toHaveBeenCalledTimes(1);
		expect(server.recipesGet).toHaveBeenCalledWith('recipes/pie.md');
		expect(server.recipesList).toHaveBeenCalledTimes(1);
	});

	it('ignores OMNIA actions that touched neither notes nor recipes', async () => {
		omnia({ refresh: ['tasks', 'calendar'], paths: ['recipes/pie.md'] });
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
		expect(server.recipesList).not.toHaveBeenCalled();
	});

	// The root layout's resync after a gap in the live stream is a bare
	// data-changed: whatever was announced meanwhile is lost.
	it('refetches the lists and the recipe on screen after a gap, and no other', async () => {
		cache.rememberRecipe(recipe('recipes/tart.md')); // prefetched by the menu
		here = '/recipes/recipes/pie.md';
		resync();
		await settle();
		expect(server.recipesGet).toHaveBeenCalledExactlyOnceWith('recipes/pie.md');
		expect(server.recipesList).toHaveBeenCalledTimes(1);
		expect(cache.cachedRecipe('recipes/pie.md')?.revision).toBe('r2');
	});

	it('refetches the recipe being edited after a gap', async () => {
		here = '/recipes/edit/recipes/pie.md';
		omnia(null);
		await settle();
		expect(server.recipesGet).toHaveBeenCalledExactlyOnceWith('recipes/pie.md');
	});

	it('refetches only the lists after a gap on the menu', async () => {
		resync();
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
		expect(server.recipesList).toHaveBeenCalledTimes(1);
	});

	it('stops on cleanup, a pending burst included', async () => {
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r2', origin: null });
		stop();
		fire({ type: 'note', path: 'recipes/pie.md', revision: 'r3', origin: null });
		fire({ type: 'note_removed', path: 'recipes/pie.md', origin: null });
		omnia({ refresh: ['recipes'], paths: ['recipes/pie.md'] });
		await settle();
		expect(server.recipesGet).not.toHaveBeenCalled();
		expect(server.recipesList).not.toHaveBeenCalled();
		expect(cache.cachedRecipe('recipes/pie.md')).toBeDefined();
	});
});
