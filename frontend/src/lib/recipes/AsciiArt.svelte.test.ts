// @vitest-environment jsdom
// The drawing's states: art alone when the AI step cannot run (or its routes
// fail), Redraw and Draw when it can, a blinking "drawing_" while one runs,
// and the recipe reloaded when it lands.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '$lib/api';
import type { Recipe } from './types';

const stub = vi.hoisted(() => ({
	recipesStatus: vi.fn(),
	recipesArtStatus: vi.fn(),
	recipesStartArt: vi.fn(),
	recipesGet: vi.fn()
}));

vi.mock('$lib/api', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api')>();
	return { ...actual, api: { ...actual.api, ...stub } };
});

const { default: AsciiArt } = await import('./AsciiArt.svelte');
const { cachedRecipe, clearRecipeCache } = await import('./cache.svelte');

const PATH = 'recipes/noodles.md';
const notFound = () => Promise.reject(new ApiError(404, 'Not Found'));

let cleanup: (() => void) | undefined;
beforeEach(() => {
	vi.useFakeTimers();
	for (const fn of Object.values(stub)) fn.mockReset();
	clearRecipeCache();
});
afterEach(() => {
	cleanup?.();
	cleanup = undefined;
	vi.useRealTimers();
});

function render(art: string) {
	const target = document.body.appendChild(document.createElement('div'));
	const component = mount(AsciiArt, {
		target,
		props: { path: PATH, title: 'Noodles', art, colors: '', palette: {} }
	});
	flushSync();
	cleanup = () => {
		unmount(component);
		target.remove();
	};
	return target;
}

/** Let pending promises land, then the DOM. */
async function settle() {
	await vi.advanceTimersByTimeAsync(0);
	flushSync();
}

const button = (el: HTMLElement) => el.querySelector('button');
const status = (el: HTMLElement) => el.querySelector('.status')?.textContent ?? null;
/** What a screen reader is told. */
const region = (el: HTMLElement) => el.querySelector('[role="status"]');

describe('AsciiArt', () => {
	it('shows the art and nothing else when the routes fail', async () => {
		const quiet = vi.spyOn(console, 'error').mockImplementation(() => {});
		stub.recipesStatus.mockImplementation(notFound);
		stub.recipesArtStatus.mockImplementation(notFound);
		const el = render(' /\\ ');
		await settle();
		expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('Noodles');
		expect(button(el)).toBeNull();
		expect(status(el)).toBeNull();
		await vi.advanceTimersByTimeAsync(10_000);
		expect(stub.recipesArtStatus).toHaveBeenCalledTimes(1);
		expect(quiet).not.toHaveBeenCalled();
		quiet.mockRestore();
	});

	it('draws nothing at all for a recipe with no art and no AI', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: false });
		stub.recipesArtStatus.mockResolvedValue({ drawing: false, error: null });
		const el = render('');
		await settle();
		expect(el.querySelector('.wrap')).toBeNull();
		expect(region(el)?.textContent).toBe('');
	});

	// A live region is announced when its text changes, not when it is
	// inserted already holding its text.
	it('tells a screen reader through one region that was there first', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: true });
		stub.recipesArtStatus
			.mockResolvedValueOnce({ drawing: true, error: null })
			.mockResolvedValueOnce({ drawing: false, error: 'The model refused' });
		const el = render('');
		const live = region(el);
		expect(live?.textContent).toBe('');
		await settle();
		expect(region(el)).toBe(live);
		expect(live?.textContent).toBe('drawing');
		await vi.advanceTimersByTimeAsync(3000);
		flushSync();
		expect(region(el)).toBe(live);
		expect(live?.textContent).toBe('The model refused');
		expect(el.querySelectorAll('[aria-live], [role="status"], [role="alert"]')).toHaveLength(1);
	});

	it('offers Redraw over art and Draw without it when the AI can run', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: true });
		stub.recipesArtStatus.mockResolvedValue({ drawing: false, error: null });
		let el = render(' /\\ ');
		await settle();
		expect(button(el)?.textContent?.trim()).toBe('Redraw');
		expect(button(el)?.classList.contains('redraw')).toBe(true);
		cleanup?.();
		el = render('');
		await settle();
		expect(button(el)?.textContent?.trim()).toBe('Draw ASCII art');
	});

	it('shows drawing_ while one runs, polls, and loads the recipe when it lands', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: true });
		stub.recipesArtStatus
			.mockResolvedValueOnce({ drawing: true, error: null })
			.mockResolvedValueOnce({ drawing: true, error: null })
			.mockResolvedValueOnce({ drawing: false, error: null });
		stub.recipesGet.mockResolvedValue({ path: PATH, title: 'Noodles', art: 'new' } as Recipe);
		const el = render('');
		await settle();
		expect(status(el)).toBe('drawing_');
		expect(button(el)).toBeNull();
		await vi.advanceTimersByTimeAsync(3000);
		expect(stub.recipesArtStatus).toHaveBeenCalledTimes(2);
		expect(status(el)).toBe('drawing_');
		await vi.advanceTimersByTimeAsync(3000);
		flushSync();
		expect(stub.recipesArtStatus).toHaveBeenCalledTimes(3);
		expect(stub.recipesGet).toHaveBeenCalledExactlyOnceWith(PATH);
		expect(cachedRecipe(PATH)?.art).toBe('new');
		expect(status(el)).toBeNull();
	});

	it('starts a redraw, then waits for it', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: true });
		stub.recipesArtStatus.mockResolvedValue({ drawing: false, error: null });
		stub.recipesStartArt.mockResolvedValue({ drawing: true });
		const el = render(' /\\ ');
		await settle();
		button(el)?.click();
		await settle();
		expect(stub.recipesStartArt).toHaveBeenCalledExactlyOnceWith(PATH);
		expect(status(el)).toBe('drawing_');
		expect(el.querySelector('pre')?.classList.contains('busy')).toBe(true);
	});

	// The root layout mounts an arriving page twice, one right after the other.
	it('asks once when two copies arrive together', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: true });
		stub.recipesArtStatus.mockResolvedValue({ drawing: true, error: null });
		const first = render('');
		const second = document.body.appendChild(document.createElement('div'));
		const twin = mount(AsciiArt, {
			target: second,
			props: { path: PATH, title: 'Noodles', art: '', colors: '', palette: {} }
		});
		flushSync();
		await settle();
		expect(stub.recipesStatus).toHaveBeenCalledTimes(1);
		expect(stub.recipesArtStatus).toHaveBeenCalledTimes(1);
		expect(status(first)).toBe('drawing_');
		expect(status(second)).toBe('drawing_');
		unmount(twin);
		second.remove();
	});

	it('says why a drawing failed', async () => {
		stub.recipesStatus.mockResolvedValue({ ai: true });
		stub.recipesArtStatus
			.mockResolvedValueOnce({ drawing: true, error: null })
			.mockResolvedValueOnce({ drawing: false, error: 'The model refused' });
		const el = render('');
		await settle();
		await vi.advanceTimersByTimeAsync(3000);
		flushSync();
		expect(el.querySelector('.rc-error')?.textContent).toBe('The model refused');
		expect(stub.recipesGet).not.toHaveBeenCalled();
		expect(button(el)?.textContent?.trim()).toBe('Draw ASCII art');
	});

	it('says why when the drawing failed before the page opened', async () => {
		// A rejected key or an empty account fails in under a second, so the
		// drawing a new recipe starts is often over by the time its page asks.
		stub.recipesStatus.mockResolvedValue({ ai: true });
		stub.recipesArtStatus.mockResolvedValue({
			drawing: false,
			error: 'The AI account is out of credit.'
		});
		const el = render('');
		await settle();
		expect(el.querySelector('.rc-error')?.textContent).toBe('The AI account is out of credit.');
		expect(status(el)).toBeNull();
		expect(button(el)?.textContent?.trim()).toBe('Draw ASCII art');
	});
});
