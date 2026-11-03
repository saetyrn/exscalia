// @vitest-environment jsdom
// The menu's arrival: one request for the list, the loader's, whether the
// menu waits for it (a first visit) or draws from the cache and refreshes
// behind it (Back). The page itself starts none.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RecipeCard, RecipeList } from '$lib/recipes/types';

const stub = vi.hoisted(() => ({ recipesList: vi.fn(), recipesGet: vi.fn() }));
vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});
vi.mock('$lib/recipes/nav', () => ({ forward: vi.fn(), back: vi.fn() }));

const { load } = await import('./+page');
const { default: Menu } = await import('./+page.svelte');
const { clearRecipeCache, loadRecipes } = await import('$lib/recipes/cache.svelte');

type MenuData = { list: RecipeList | null; error: string };

const card = (name: string): RecipeCard => ({
	path: `recipes/${name}.md`,
	title: name,
	total_minutes: null,
	thumbnail: null,
	tags: [],
	modified: '2026-10-04T12:00:00Z',
	art: '',
	art_colors: '',
	art_palette: {}
});
const list = (...names: string[]): RecipeList => ({ recipes: names.map(card), tags: [] });

class Observer {
	constructor(private callback: ResizeObserverCallback) {}
	observe(target: Element) {
		this.callback([{ target } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver);
	}
	unobserve() {}
	disconnect() {}
}

let target: HTMLElement;
let instance: ReturnType<typeof mount> | undefined;

/** Let every request settle, then the DOM. */
async function settle() {
	await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

/** Mount the page on what the loader answered, once its requests settled:
 * on Back the slide holds the page while the old one is captured. */
async function show(data: MenuData) {
	await settle();
	instance = mount(Menu, { target, props: { data } });
	await settle();
}

/** How many recipes the menu turns through. */
const shown = () =>
	Number(target.querySelector<HTMLElement>('.rc-menu-track')?.style.getPropertyValue('--n') || 0);

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', Observer);
	clearRecipeCache();
	sessionStorage.clear();
	stub.recipesList.mockReset();
	stub.recipesGet.mockReset();
	stub.recipesGet.mockReturnValue(new Promise(() => {}));
	target = document.body.appendChild(document.createElement('div'));
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = undefined;
	target.remove();
	vi.unstubAllGlobals();
});

describe('the menu arriving', () => {
	it('asks for the list once on a first visit', async () => {
		stub.recipesList.mockResolvedValue(list('Pie', 'Tart'));
		await show((await load({} as never)) as MenuData);
		expect(stub.recipesList).toHaveBeenCalledTimes(1);
		expect(shown()).toBe(2);
	});

	it('draws the cached menu at once on the way back, and refreshes it once behind it', async () => {
		stub.recipesList.mockResolvedValue(list('Pie'));
		await loadRecipes();
		stub.recipesList.mockReset();
		stub.recipesList.mockResolvedValue(list('Pie', 'Tart'));
		const data = (await load({} as never)) as MenuData;
		expect(data.list?.recipes).toHaveLength(1);
		await show(data);
		expect(shown()).toBe(2);
		expect(stub.recipesList).toHaveBeenCalledTimes(1);
	});

	it('says why when the first visit cannot load', async () => {
		stub.recipesList.mockRejectedValue(new Error('Offline'));
		await show((await load({} as never)) as MenuData);
		expect(stub.recipesList).toHaveBeenCalledTimes(1);
		expect(target.querySelector('.rc-error')?.textContent).toBe('Offline');
	});
});
