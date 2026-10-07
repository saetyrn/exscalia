// The menu's focus memory: coming back lands on the recipe that was in focus,
// wherever the list has moved it, or on whatever took its place.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { focusIndex, forgetFocus, recalledFocus, rememberFocus } from './focus';

const cards = (...paths: string[]) => paths.map((path) => ({ path }));

describe('recipe menu focus', () => {
	const storage = new Map<string, string>();

	beforeEach(() => {
		storage.clear();
		vi.stubGlobal('sessionStorage', {
			getItem: (key: string) => storage.get(key) ?? null,
			setItem: (key: string, value: string) => void storage.set(key, value),
			removeItem: (key: string) => void storage.delete(key)
		});
		forgetFocus();
	});

	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('remembers the focused recipe, and keeps it in the tab for a reload', () => {
		expect(recalledFocus()).toBeNull();
		rememberFocus('recipes/pie.md');
		expect(recalledFocus()).toBe('recipes/pie.md');
		expect([...storage.values()]).toEqual(['recipes/pie.md']);
	});

	it('reads the tab copy after a reload', async () => {
		rememberFocus('recipes/pie.md');
		vi.resetModules();
		const reloaded = await import('./focus');
		expect(reloaded.recalledFocus()).toBe('recipes/pie.md');
	});

	it('still remembers when storage refuses', () => {
		vi.stubGlobal('sessionStorage', {
			getItem: () => {
				throw new Error('denied');
			},
			setItem: () => {
				throw new Error('denied');
			},
			removeItem: () => {
				throw new Error('denied');
			}
		});
		forgetFocus();
		expect(recalledFocus()).toBeNull();
		rememberFocus('recipes/pie.md');
		expect(recalledFocus()).toBe('recipes/pie.md');
	});

	it('finds the remembered recipe wherever the list moved it', () => {
		expect(focusIndex(cards('a', 'b', 'c'), 'c')).toBe(2);
		expect(focusIndex(cards('new', 'a', 'b', 'c'), 'c', 2)).toBe(3);
	});

	it('starts at the top with nothing remembered', () => {
		expect(focusIndex(cards('a', 'b'), null)).toBe(0);
		expect(focusIndex(cards('a', 'b'), 'gone')).toBe(0);
		expect(focusIndex([], 'a', 3)).toBe(0);
	});

	it('takes the recipe that moved into a deleted one\'s place', () => {
		expect(focusIndex(cards('a', 'c', 'd'), 'b', 1)).toBe(1);
		expect(focusIndex(cards('a', 'b'), 'c', 2)).toBe(1);
	});
});
