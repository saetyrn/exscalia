import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	confirmIngredient,
	factorText,
	fitServings,
	groupIngredients,
	localThumbnail,
	metaLine,
	recipeInput,
	savedServings,
	saveServings,
	servingsMax,
	sourceLink
} from './recipepage';
import type { Ingredient, Recipe } from './types';

const ing = (over: Partial<Ingredient>): Ingredient => ({
	id: 'x',
	amount: 1,
	unit: '',
	item: 'thing',
	prep: '',
	scaling: 'linear',
	estimated: false,
	group: null,
	...over
});

const recipe = (over: Partial<Recipe> = {}): Recipe => ({
	path: 'recipes/noodles.md',
	revision: 'r1',
	created: '2026-10-04T12:00:00Z',
	modified: '2026-10-04T12:00:00Z',
	title: 'Noodles',
	base_servings: 2,
	prep_minutes: 10,
	cook_minutes: 75,
	ingredients: [ing({ id: 'noodles', amount: 200, unit: 'g', item: 'noodles' })],
	steps: [{ text: 'Boil {noodles}.', timer_seconds: 300 }],
	notes: 'Good cold.',
	tags: ['quick', 'noodles'],
	source: { type: 'website', url: 'https://www.example.com/n', thumbnail: null },
	art: ' /\\ ',
	art_colors: ' aa ',
	art_palette: { a: '#e8c070' },
	...over
});

describe('servings', () => {
	it('runs from 1 to at least 12', () => {
		expect(servingsMax(2)).toBe(12);
		expect(servingsMax(20)).toBe(20);
	});

	it('keeps a choice in range and falls back to the base otherwise', () => {
		expect(fitServings(null, 2)).toBe(2);
		expect(fitServings(3, 2)).toBe(3);
		expect(fitServings(12, 2)).toBe(12);
		expect(fitServings(13, 2)).toBe(2);
		expect(fitServings(0, 4)).toBe(4);
		expect(fitServings(2.5, 4)).toBe(4);
		expect(fitServings(18, 20)).toBe(18);
	});

	it('writes the factor the way the slider shows it', () => {
		expect(factorText(3, 2)).toBe('×1.5');
		expect(factorText(1, 3)).toBe('×0.33');
		expect(factorText(4, 2)).toBe('×2');
	});

	describe('remembered for the tab', () => {
		afterEach(() => vi.unstubAllGlobals());

		it('reads back what it saved, per recipe', () => {
			const store = new Map<string, string>();
			vi.stubGlobal('sessionStorage', {
				getItem: (k: string) => store.get(k) ?? null,
				setItem: (k: string, v: string) => void store.set(k, v)
			});
			expect(savedServings('recipes/a.md')).toBeNull();
			saveServings('recipes/a.md', 5);
			expect(savedServings('recipes/a.md')).toBe(5);
			expect(savedServings('recipes/b.md')).toBeNull();
		});

		it('survives storage that throws or holds junk', () => {
			vi.stubGlobal('sessionStorage', {
				getItem: () => {
					throw new Error('denied');
				},
				setItem: () => {
					throw new Error('denied');
				}
			});
			expect(() => saveServings('recipes/a.md', 3)).not.toThrow();
			expect(savedServings('recipes/a.md')).toBeNull();
			vi.stubGlobal('sessionStorage', { getItem: () => 'lots', setItem: () => {} });
			expect(savedServings('recipes/a.md')).toBeNull();
		});
	});
});

describe('the caption line', () => {
	it('puts time first, then tags', () => {
		expect(metaLine(recipe())).toBe('10 min prep · 1 h 15 min cook · quick · noodles');
	});

	it('leaves out what is missing', () => {
		expect(metaLine(recipe({ prep_minutes: null, cook_minutes: 0, tags: [] }))).toBe('0 min cook');
		expect(metaLine(recipe({ prep_minutes: null, cook_minutes: null, tags: [] }))).toBe('');
	});
});

describe('the source link', () => {
	it('links http and https by host, without www', () => {
		expect(sourceLink('https://www.example.com/pasta?x=1')).toEqual({
			href: 'https://www.example.com/pasta?x=1',
			host: 'example.com'
		});
		expect(sourceLink('http://cook.example.org/a')?.host).toBe('cook.example.org');
	});

	it('links nothing else', () => {
		expect(sourceLink(null)).toBeNull();
		expect(sourceLink('')).toBeNull();
		expect(sourceLink('javascript:alert(1)')).toBeNull();
		expect(sourceLink('data:text/html,<b>x</b>')).toBeNull();
		expect(sourceLink('ftp://example.com/a')).toBeNull();
		expect(sourceLink('example.com/no-scheme')).toBeNull();
		expect(sourceLink('not a url')).toBeNull();
	});
});

describe('ingredient groups', () => {
	it('keeps first-seen order and gathers each group', () => {
		const groups = groupIngredients([
			ing({ id: 'a', group: 'Sauce' }),
			ing({ id: 'b', group: null }),
			ing({ id: 'c', group: ' Sauce ' }),
			ing({ id: 'd', group: '' })
		]);
		expect(groups.map((g) => [g.name, g.items.map((i) => i.id)])).toEqual([
			['Sauce', ['a', 'c']],
			[null, ['b', 'd']]
		]);
	});
});

describe('what a save sends', () => {
	it('is the whole record without the drawing or the note’s bookkeeping', () => {
		const sent = recipeInput(recipe());
		expect(sent).not.toHaveProperty('art');
		expect(sent).not.toHaveProperty('art_colors');
		expect(sent).not.toHaveProperty('art_palette');
		expect(sent).not.toHaveProperty('path');
		expect(sent).not.toHaveProperty('revision');
		expect(sent.title).toBe('Noodles');
		expect(sent.steps).toHaveLength(1);
		expect(sent.source?.url).toBe('https://www.example.com/n');
	});

	it('confirms one estimate and leaves the rest', () => {
		const list = [ing({ id: 'a', estimated: true }), ing({ id: 'b', estimated: true })];
		const next = confirmIngredient(list, 'a');
		expect(next.map((i) => i.estimated)).toEqual([false, true]);
		expect(list[0].estimated).toBe(true);
	});
});

describe('the source picture', () => {
	it('draws attachments only', () => {
		expect(localThumbnail('/api/attachments/abc.jpg')).toBe('/api/attachments/abc.jpg');
		expect(localThumbnail('https://example.com/a.jpg')).toBeNull();
		expect(localThumbnail(null)).toBeNull();
	});
});
