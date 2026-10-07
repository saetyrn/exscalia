// The read-through caches: a page draws from them on its first frame, so a
// slide lands on content, and they stay in step with what the server answered.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Recipe, RecipeCard, RecipeList } from './types';
import { emptyRecipe } from './types';

const server = vi.hoisted(() => ({
	recipesGet: vi.fn(),
	recipesList: vi.fn()
}));
vi.mock('$lib/api', () => ({ api: server }));

const cache = await import('./cache.svelte');

const recipe = (path: string, title = 'Pie'): Recipe => ({
	...emptyRecipe(),
	title,
	path,
	revision: 'r1',
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

beforeEach(() => {
	cache.clearRecipeCache();
	server.recipesGet.mockReset();
	server.recipesList.mockReset();
});

describe('recipe cache', () => {
	it('keeps what a load fetched, and shares one request per path', async () => {
		server.recipesGet.mockImplementation(async (path: string) => recipe(path));
		expect(cache.cachedRecipe('recipes/pie.md')).toBeUndefined();
		const [a, b] = await Promise.all([
			cache.loadRecipe('recipes/pie.md'),
			cache.loadRecipe('recipes/pie.md')
		]);
		expect(a).toBe(b);
		expect(server.recipesGet).toHaveBeenCalledTimes(1);
		expect(cache.cachedRecipe('recipes/pie.md')?.title).toBe('Pie');
	});

	it('prefetches once, and never throws', async () => {
		server.recipesGet.mockRejectedValueOnce(new Error('offline'));
		await expect(cache.prefetchRecipe('recipes/pie.md')).resolves.toBeUndefined();
		server.recipesGet.mockImplementation(async (path: string) => recipe(path));
		await cache.prefetchRecipe('recipes/pie.md');
		await cache.prefetchRecipe('recipes/pie.md');
		expect(server.recipesGet).toHaveBeenCalledTimes(2);
	});

	it('remembers a saved recipe over the old copy', () => {
		cache.rememberRecipe(recipe('recipes/pie.md', 'Pie'));
		cache.rememberRecipe(recipe('recipes/pie.md', 'Apple pie'));
		expect(cache.cachedRecipe('recipes/pie.md')?.title).toBe('Apple pie');
	});

	it('keeps each search and tag of the menu apart', async () => {
		const all: RecipeList = { recipes: [card('recipes/a.md'), card('recipes/b.md')], tags: [] };
		const quick: RecipeList = { recipes: [card('recipes/b.md')], tags: [] };
		server.recipesList.mockImplementation(async (q: string, tag: string) => (tag ? quick : all));
		await cache.loadRecipes();
		await cache.loadRecipes('', 'quick');
		expect(cache.cachedList()).toEqual(all);
		expect(cache.cachedList('', 'quick')).toEqual(quick);
		expect(cache.cachedList('lemon')).toBeUndefined();
	});

	it('keeps a save over the answer of a fetch that left before it', async () => {
		let answer!: (r: Recipe) => void;
		server.recipesGet.mockReturnValueOnce(new Promise<Recipe>((resolve) => (answer = resolve)));
		cache.rememberRecipe(recipe('recipes/pie.md', 'Pie'));
		const loading = cache.loadRecipe('recipes/pie.md');
		// The page's save lands while the loader's revalidation is on the way.
		cache.rememberRecipe({ ...recipe('recipes/pie.md', 'Apple pie'), revision: 'r2' });
		answer(recipe('recipes/pie.md', 'Pie'));
		expect((await loading).revision).toBe('r2');
		expect(cache.cachedRecipe('recipes/pie.md')?.revision).toBe('r2');
	});

	it('does not bring back a recipe deleted while a fetch was on the way', async () => {
		let answer!: (r: Recipe) => void;
		server.recipesGet.mockReturnValueOnce(new Promise<Recipe>((resolve) => (answer = resolve)));
		const loading = cache.loadRecipe('recipes/pie.md');
		cache.forgetRecipe('recipes/pie.md');
		answer(recipe('recipes/pie.md'));
		await loading;
		expect(cache.cachedRecipe('recipes/pie.md')).toBeUndefined();
		expect(cache.recipeGone('recipes/pie.md')).toBe(true);
	});

	it('marks a forgotten recipe gone until the server answers it again', async () => {
		server.recipesGet.mockImplementation(async (path: string) => recipe(path));
		cache.rememberRecipe(recipe('recipes/pie.md'));
		expect(cache.recipeGone('recipes/pie.md')).toBe(false);
		cache.forgetRecipe('recipes/pie.md');
		expect(cache.recipeGone('recipes/pie.md')).toBe(true);
		// Restored from the Trash: a fresh load brings it back.
		await cache.loadRecipe('recipes/pie.md');
		expect(cache.recipeGone('recipes/pie.md')).toBe(false);
		expect(cache.cachedRecipe('recipes/pie.md')).toBeDefined();
	});

	it('shares one menu request between concurrent loads of one search', async () => {
		const all: RecipeList = { recipes: [card('recipes/a.md')], tags: [] };
		server.recipesList.mockResolvedValue(all);
		await Promise.all([cache.loadRecipes(), cache.loadRecipes(), cache.loadRecipes('', 'quick')]);
		expect(server.recipesList).toHaveBeenCalledTimes(2);
		await cache.loadRecipes();
		expect(server.recipesList).toHaveBeenCalledTimes(3);
	});

	it('forgets a deleted recipe and its card in every list', async () => {
		server.recipesList.mockResolvedValue({
			recipes: [card('recipes/a.md'), card('recipes/b.md')],
			tags: []
		});
		await cache.loadRecipes();
		cache.rememberRecipe(recipe('recipes/b.md'));
		cache.forgetRecipe('recipes/b.md');
		expect(cache.cachedRecipe('recipes/b.md')).toBeUndefined();
		expect(cache.cachedList()?.recipes.map((c) => c.path)).toEqual(['recipes/a.md']);
	});

	it('knows a recipe’s title from the recipe or from a menu card', async () => {
		server.recipesList.mockResolvedValue({ recipes: [card('recipes/a.md')], tags: [] });
		await cache.loadRecipes();
		expect(cache.knownRecipeTitle('recipes/a.md')).toBe('recipes/a.md');
		cache.rememberRecipe(recipe('recipes/a.md', 'Apple pie'));
		expect(cache.knownRecipeTitle('recipes/a.md')).toBe('Apple pie');
		expect(cache.knownRecipeTitle('recipes/b.md')).toBeUndefined();
	});
});
