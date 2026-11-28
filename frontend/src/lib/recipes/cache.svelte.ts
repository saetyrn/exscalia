// Read-through caches, after the recipe app's api.ts: a screen draws from them
// on its first frame, so a page slides in filled instead of as a skeleton, and
// still refreshes from the server right after. Reactive maps, so a page that
// reads cachedRecipe()/cachedList() in a $derived also picks up the refresh.
// Recipes are network-only (like media): nothing here survives a reload.
import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { api } from '$lib/api';
import type { Recipe, RecipeList } from './types';

const recipes = new SvelteMap<string, Recipe>();
const lists = new SvelteMap<string, RecipeList>();
const inflight = new Map<string, Promise<Recipe>>();
// Forgotten and not answered since: a page still holding its loader's copy
// reads this to know that copy is dead.
const gone = new SvelteSet<string>();
// Bumped per path by every remember and forget, so a fetch can tell that a
// save or a delete landed while it was on the way.
const stamps = new Map<string, number>();
let stamp = 0;
const inflightLists = new Map<string, Promise<RecipeList>>();

const listKey = (q: string, tag: string) => `${q}\u0000${tag}`;

export function cachedRecipe(path: string): Recipe | undefined {
	return recipes.get(path);
}

/** The menu as last fetched for this search and tag. */
export function cachedList(q = '', tag = ''): RecipeList | undefined {
	return lists.get(listKey(q, tag));
}

/** The recipe at `path` was forgotten (deleted, moved away) and nothing has
 * brought it back. Reactive. */
export function recipeGone(path: string): boolean {
	return gone.has(path);
}

/** The title the server last gave the recipe at `path`, from the recipe or a
 * menu card; undefined until this page load has seen it. A rename keeps the
 * file, so the tab strip asks here before it falls back to the file name. */
export function knownRecipeTitle(path: string): string | undefined {
	const recipe = recipes.get(path);
	if (recipe) return recipe.title;
	for (const list of lists.values()) {
		const card = list.recipes.find((c) => c.path === path);
		if (card) return card.title;
	}
	return undefined;
}

/** Keep a recipe the server just returned (a get, create or update). */
export function rememberRecipe(recipe: Recipe): Recipe {
	stamps.set(recipe.path, ++stamp);
	gone.delete(recipe.path);
	recipes.set(recipe.path, recipe);
	return recipe;
}

/** A recipe is gone (deleted, trashed): drop it and its card from every list. */
export function forgetRecipe(path: string): void {
	stamps.set(path, ++stamp);
	gone.add(path);
	recipes.delete(path);
	for (const [key, list] of lists) {
		if (list.recipes.some((card) => card.path === path))
			lists.set(key, { ...list, recipes: list.recipes.filter((card) => card.path !== path) });
	}
}

export function clearRecipeCache(): void {
	recipes.clear();
	lists.clear();
	inflight.clear();
	gone.clear();
	stamps.clear();
	inflightLists.clear();
}

/** Fetch a recipe and keep it. Concurrent calls for one path share a request.
 * A save or a delete that lands while the fetch is on the way is newer than
 * its answer, and stays. */
export function loadRecipe(path: string): Promise<Recipe> {
	const running = inflight.get(path);
	if (running) return running;
	const since = stamps.get(path);
	const request = api
		.recipesGet(path)
		.then((fresh) =>
			stamps.get(path) === since ? rememberRecipe(fresh) : (recipes.get(path) ?? fresh)
		)
		.finally(() => inflight.delete(path));
	inflight.set(path, request);
	return request;
}

/** Fetch the menu (for a search and tag) and keep it. Concurrent calls for one
 * search share a request: the loader's refresh and the page's, twice, since
 * the root layout mounts an arriving page twice. */
export function loadRecipes(q = '', tag = ''): Promise<RecipeList> {
	const key = listKey(q, tag);
	const running = inflightLists.get(key);
	if (running) return running;
	const request = api
		.recipesList(q, tag)
		.then((list) => {
			lists.set(key, list);
			return list;
		})
		.finally(() => inflightLists.delete(key));
	inflightLists.set(key, request);
	return request;
}

/** Refetch every menu list the cache holds (each search and tag seen), for a
 * change the server announced. One that fails keeps what it had. */
export async function refreshLists(): Promise<void> {
	await Promise.all(
		[...lists.keys()].map((key) => {
			const [q, tag] = key.split('\u0000');
			return loadRecipes(q, tag).then(
				() => undefined,
				() => undefined
			);
		})
	);
}

/** Warm the cache for a recipe the user is likely to open. Never throws. */
export function prefetchRecipe(path: string): Promise<void> {
	if (recipes.has(path)) return Promise.resolve();
	return loadRecipe(path).then(
		() => undefined,
		() => undefined
	);
}
