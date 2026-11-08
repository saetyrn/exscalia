// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { plainKey } from './keys';
import { requestSlide, SLIDE_ATTR, takeSlide } from './transition';
import {
	editPathFromParam,
	pageTitle,
	recipeEditHref,
	recipeHref,
	recipePathAt,
	recipePathFromParam
} from './paths';

describe('recipe paths', () => {
	it('puts the vault path after /recipes/, and reads it back from the route', () => {
		expect(recipeHref('recipes/crème brûlée.md')).toBe('/recipes/recipes/cr%C3%A8me%20br%C3%BBl%C3%A9e.md');
		expect(recipePathFromParam('crème brûlée.md')).toBe('recipes/crème brûlée.md');
		expect(recipeEditHref('recipes/pie.md')).toBe('/recipes/edit/recipes/pie.md');
		expect(editPathFromParam('recipes/pie.md')).toBe('recipes/pie.md');
	});

	it('reads the recipe a pathname shows back from it', () => {
		expect(recipePathAt(recipeHref('recipes/crème brûlée.md'))).toBe('recipes/crème brûlée.md');
		expect(recipePathAt(recipeHref('recipes/mains/pie.md'))).toBe('recipes/mains/pie.md');
		expect(recipePathAt(recipeEditHref('recipes/pie.md'))).toBe('recipes/pie.md');
		expect(recipePathAt('/recipes')).toBeNull();
		expect(recipePathAt('/recipes/add')).toBeNull();
		expect(recipePathAt('/recipes/review')).toBeNull();
		expect(recipePathAt('/note/recipes/pie.md')).toBeNull();
		expect(recipePathAt('/recipes/recipes/%E0%A4%A.md')).toBeNull();
	});

	it('titles the browser tab', () => {
		expect(pageTitle()).toBe('Recipes — EXSCALIA');
		expect(pageTitle('  ')).toBe('Recipes — EXSCALIA');
		expect(pageTitle('Carbonara')).toBe('Carbonara — Recipes');
	});
});

describe('plainKey', () => {
	const press = (init: KeyboardEventInit, target: Element = document.body) => {
		const event = new KeyboardEvent('keydown', { key: 'k', cancelable: true, ...init });
		Object.defineProperty(event, 'target', { value: target });
		return event;
	};

	it('takes a bare key on the page', () => {
		expect(plainKey(press({}))).toBe(true);
		expect(plainKey(press({ shiftKey: true }))).toBe(true);
	});

	it('leaves chords, handled keys and typing alone', () => {
		expect(plainKey(press({ metaKey: true }))).toBe(false);
		expect(plainKey(press({ ctrlKey: true }))).toBe(false);
		expect(plainKey(press({ altKey: true }))).toBe(false);
		const handled = press({});
		handled.preventDefault();
		expect(plainKey(handled)).toBe(false);
		const field = document.createElement('div');
		field.innerHTML = '<label><input type="range"></label><div contenteditable="true"><b>x</b></div>';
		expect(plainKey(press({}, field.querySelector('input')!))).toBe(false);
		expect(plainKey(press({}, field.querySelector('b')!))).toBe(false);
	});

	// The page a slide leaves stays mounted while the router loads the next
	// one and while its pane is captured: E then a quick Escape went back to
	// the menu from the recipe page instead of opening the edit form.
	it('gives no key to any page from a slide’s request to its end', () => {
		requestSlide('forward', '/recipes/edit/recipes/pie.md');
		expect(plainKey(press({ key: 'Escape' }))).toBe(false);
		takeSlide({ type: 'goto', to: { url: new URL('http://app.test/recipes/edit/recipes/pie.md') } } as never);
		document.documentElement.setAttribute(SLIDE_ATTR, 'forward');
		try {
			expect(plainKey(press({ key: 'Escape' }))).toBe(false);
			expect(plainKey(press({ key: 'ArrowLeft' }))).toBe(false);
		} finally {
			document.documentElement.removeAttribute(SLIDE_ATTR);
		}
		expect(plainKey(press({ key: 'Escape' }))).toBe(true);
	});

	it('takes keys again when a request no navigation took has lapsed', () => {
		requestSlide('forward', '/recipes/add', Date.now() - 1000);
		expect(plainKey(press({ key: 'Escape' }))).toBe(true);
		takeSlide({ type: 'goto', to: null } as never);
	});
});
