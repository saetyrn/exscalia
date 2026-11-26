// @vitest-environment jsdom
// The line under the title that says a save met a newer copy or failed. A
// live region is announced when its text changes, not when it is inserted
// already holding its text, so the line is in the page from the start.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api';
import { emptyRecipe, type Recipe } from '$lib/recipes/types';

const stub = vi.hoisted(() => ({
	recipesUpdate: vi.fn(),
	recipesGet: vi.fn(),
	recipesStatus: vi.fn(),
	recipesArtStatus: vi.fn()
}));
vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});
vi.mock('$lib/recipes/nav', () => ({ forward: vi.fn(), back: vi.fn() }));

const { default: RecipePage } = await import('./+page.svelte');
const { clearRecipeCache } = await import('$lib/recipes/cache.svelte');

const PATH = 'recipes/pie.md';
const pie = (revision: string, title = 'Pie'): Recipe => ({
	...emptyRecipe(),
	title,
	path: PATH,
	revision,
	created: '2026-10-04T12:00:00Z',
	modified: '2026-10-04T12:00:00Z',
	art: '',
	art_colors: '',
	art_palette: {}
});

let target: HTMLElement;
let instance: ReturnType<typeof mount> | undefined;

async function settle() {
	for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
	flushSync();
}

/** Rename from the title, the way a double-click and Enter do. */
async function rename(title: string) {
	target.querySelector('.editable-title')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
	flushSync();
	const input = target.querySelector('input')!;
	input.value = title;
	input.dispatchEvent(new Event('input', { bubbles: true }));
	input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
	await settle();
}

const notice = () => target.querySelector('.notice');

beforeEach(() => {
	vi.stubGlobal(
		'ResizeObserver',
		class {
			observe() {}
			disconnect() {}
		}
	);
	clearRecipeCache();
	sessionStorage.clear();
	for (const fn of Object.values(stub)) fn.mockReset();
	stub.recipesStatus.mockResolvedValue({ ai: false });
	stub.recipesArtStatus.mockResolvedValue({ drawing: false, error: null });
	target = document.body.appendChild(document.createElement('div'));
	instance = mount(RecipePage, { target, props: { data: { path: PATH, recipe: pie('r1') } } });
	flushSync();
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = undefined;
	target.remove();
	vi.unstubAllGlobals();
});

describe('the recipe page notice', () => {
	it('is a region in the page before it has anything to say', async () => {
		const line = notice();
		expect(line?.getAttribute('role')).toBe('status');
		expect(line?.textContent).toBe('');
		stub.recipesUpdate.mockRejectedValue(new ApiError(409, 'Stale'));
		stub.recipesGet.mockResolvedValue(pie('r2', 'Apple pie'));
		await rename('Tart');
		expect(notice()).toBe(line);
		expect(line?.textContent).toBe('Changed elsewhere. Reloaded.');
		expect(line?.classList.contains('rc-error')).toBe(false);
	});

	it('says a failed save the same way, and clears on the next good one', async () => {
		const line = notice();
		stub.recipesUpdate.mockRejectedValueOnce(new Error('Offline'));
		await rename('Tart');
		expect(notice()).toBe(line);
		expect(line?.textContent).toBe('Offline');
		expect(line?.classList.contains('rc-error')).toBe(true);
		stub.recipesUpdate.mockResolvedValueOnce(pie('r2', 'Tart'));
		await rename('Tart');
		expect(notice()).toBe(line);
		expect(line?.textContent).toBe('');
	});
});
