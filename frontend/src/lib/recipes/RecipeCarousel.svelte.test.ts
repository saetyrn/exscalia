// @vitest-environment jsdom
// The revolving menu's behaviour without a real layout: focus is a recipe,
// not a slot, so it survives the list changing under it; it is remembered for
// the next visit; keys turn and open it unless OMNIA or a control has them.
// The layout itself (arc, wheel, drag, slides) is checked in the browser.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RecipeCard } from './types';

const stub = vi.hoisted(() => ({
	forward: vi.fn(async (_href: string) => undefined),
	prefetchRecipe: vi.fn(async (_path: string) => undefined)
}));
vi.mock('./nav', () => ({ forward: stub.forward }));
vi.mock('./cache.svelte', () => ({ prefetchRecipe: stub.prefetchRecipe }));

const { default: RecipeCarousel } = await import('./RecipeCarousel.svelte');
const { recipesChrome } = await import('./chrome.svelte');
const { forgetFocus, rememberFocus } = await import('./menu/focus');

const card = (path: string): RecipeCard => ({
	path,
	title: path.replace(/^recipes\/|\.md$/g, ''),
	total_minutes: null,
	thumbnail: null,
	tags: [],
	modified: '2026-10-04T12:00:00Z',
	art: '',
	art_colors: '',
	art_palette: {}
});
const cards = (...names: string[]) => names.map((n) => card(`recipes/${n}.md`));

// jsdom lays nothing out: give every element a size, and a ResizeObserver
// that reports it at once, so the stage measures and draws its items.
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
let props: { cards: RecipeCard[]; active: number };

function render(list: RecipeCard[]) {
	const live = $state({ cards: list, active: 0 });
	props = live;
	instance = mount(RecipeCarousel, { target, props });
	flushSync();
}

const focused = () =>
	target.querySelector('.cz-item.is-active')?.getAttribute('aria-label') ?? null;

function press(key: string, from: EventTarget = document.body) {
	from.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
	flushSync();
}

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', Observer);
	Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 800 });
	Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 600 });
	sessionStorage.clear();
	forgetFocus();
	recipesChrome.omniaOpen = false;
	stub.forward.mockClear();
	target = document.createElement('div');
	document.body.append(target);
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = undefined;
	target.remove();
	vi.unstubAllGlobals();
});

describe('recipe menu', () => {
	it('starts on the first recipe, and turns one step per key', () => {
		render(cards('a', 'b', 'c'));
		expect(focused()).toBe('a');
		press('ArrowDown');
		expect(focused()).toBe('b');
		expect(props.active).toBe(1);
		press('ArrowDown');
		press('ArrowDown');
		expect(focused()).toBe('c');
		press('ArrowUp');
		expect(focused()).toBe('b');
	});

	it('comes back to the recipe that was in focus', () => {
		rememberFocus('recipes/c.md');
		render(cards('a', 'b', 'c', 'd'));
		expect(focused()).toBe('c');
		press('ArrowDown');
		unmount(instance!);
		instance = undefined;
		render(cards('a', 'b', 'c', 'd'));
		expect(focused()).toBe('d');
	});

	it('keeps the same recipe in focus when the list changes under it', () => {
		render(cards('a', 'b', 'c'));
		press('ArrowDown');
		props.cards = cards('new', 'a', 'b', 'c');
		flushSync();
		expect(focused()).toBe('b');
		expect(props.active).toBe(2);
	});

	it('hands focus to the recipe that takes a deleted one\'s place', () => {
		render(cards('a', 'b', 'c', 'd'));
		press('ArrowDown');
		props.cards = cards('a', 'c', 'd');
		flushSync();
		expect(focused()).toBe('c');
		// ...and follows that one from then on, not the slot.
		props.cards = cards('new', 'a', 'c', 'd');
		flushSync();
		expect(focused()).toBe('c');
	});

	it('opens the focused recipe with Enter or Right', () => {
		render(cards('a', 'b'));
		press('ArrowDown');
		press('Enter');
		expect(stub.forward).toHaveBeenCalledWith('/recipes/recipes/b.md');
	});

	it('opens with a click on the focused title, and focuses a neighbour on a click', () => {
		render(cards('a', 'b', 'c'));
		const option = (name: string) =>
			[...target.querySelectorAll<HTMLElement>('.cz-item')].find(
				(el) => el.getAttribute('aria-label') === name
			)!;
		option('b').click();
		flushSync();
		expect(focused()).toBe('b');
		expect(stub.forward).not.toHaveBeenCalled();
		option('b').click();
		expect(stub.forward).toHaveBeenCalledWith('/recipes/recipes/b.md');
	});

	it('leaves the keys to OMNIA while it is open', () => {
		render(cards('a', 'b'));
		recipesChrome.omniaOpen = true;
		press('ArrowDown');
		press('Enter');
		expect(focused()).toBe('a');
		expect(stub.forward).not.toHaveBeenCalled();
	});

	it('leaves Enter to a focused button, and keys to a field', () => {
		render(cards('a', 'b'));
		const button = document.createElement('button');
		const field = document.createElement('input');
		document.body.append(button, field);
		press('Enter', button);
		expect(stub.forward).not.toHaveBeenCalled();
		press('ArrowDown', field);
		expect(focused()).toBe('a');
		button.remove();
		field.remove();
	});

	it('names the focused option for assistive tech', () => {
		render(cards('a', 'b'));
		press('ArrowDown');
		const stage = target.querySelector('[role="listbox"]')!;
		const id = stage.getAttribute('aria-activedescendant')!;
		expect(document.getElementById(id)?.getAttribute('aria-selected')).toBe('true');
		expect(document.getElementById(id)?.getAttribute('aria-label')).toBe('b');
	});
});
