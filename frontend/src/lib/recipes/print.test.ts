// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { collectionTitle, printRecipes, printRequest, recipeSheet } from './print';
import type { Recipe } from './types';

function recipe(over: Partial<Recipe> = {}): Recipe {
	return {
		path: 'recipes/noodles.md',
		revision: 'r1',
		created: '',
		modified: '',
		title: 'Garlic butter noodles',
		base_servings: 2,
		prep_minutes: 5,
		cook_minutes: 10,
		tags: ['pasta'],
		ingredients: [
			{ id: 'noodles', amount: 200, unit: 'g', item: 'noodles', prep: '', scaling: 'linear', estimated: false, group: null },
			{ id: 'butter', amount: 2, unit: 'tbsp', item: 'butter', prep: 'cold', scaling: 'linear', estimated: true, group: null },
			{ id: 'salt', amount: null, unit: '', item: 'salt', prep: '', scaling: 'to_taste', estimated: false, group: null }
		],
		steps: [
			{ text: 'Boil {noodles}.', timer_seconds: 480 },
			{ text: 'Toss with {butter}.', timer_seconds: null }
		],
		notes: 'Use *good* butter. <script>alert(1)</script>',
		source: { type: 'website', url: 'https://example.com/noodles', thumbnail: null },
		art: '  ~~~\n (___)',
		art_colors: '',
		art_palette: {},
		...over
	} as Recipe;
}

describe('recipeSheet', () => {
	it('lays out the recipe scaled to the servings', () => {
		const sheet = recipeSheet({ recipe: recipe(), servings: 4 });
		expect(sheet.querySelector('.rc-print-title')?.textContent).toBe('Garlic butter noodles');
		expect(sheet.querySelector('.rc-print-meta')?.textContent).toBe('5 min prep · 10 min cook · pasta · Serves 4');
		const rows = [...sheet.querySelectorAll('.rc-print-ingredients li')].map((li) => li.textContent);
		expect(rows[0]).toContain('400 g');
		expect(rows[1]).toMatch(/^4 tbsp\s*butter, cold estimated/);
		expect(rows[2]).toContain('to taste');
		const steps = [...sheet.querySelectorAll('.rc-print-steps li')];
		expect(steps[0].querySelector('strong')?.textContent).toContain('400 g');
		expect(steps[0].querySelector('.rc-print-timer')?.textContent).toBe('8 min');
		expect(sheet.querySelector('.rc-print-art')?.textContent).toBe('  ~~~\n (___)');
		expect(sheet.querySelector('.rc-print-source')?.textContent).toBe('From https://example.com/noodles');
	});

	it('keeps raw HTML in notes as text', () => {
		const notes = recipeSheet({ recipe: recipe(), servings: 2 }).querySelector('.rc-print-notes')!;
		expect(notes.querySelector('script')).toBeNull();
		expect(notes.querySelector('em')?.textContent).toBe('good');
	});

	it('leaves out what a recipe does not have', () => {
		const sheet = recipeSheet({
			recipe: recipe({ art: '', notes: '', source: null, steps: [], prep_minutes: null, cook_minutes: null, tags: [] }),
			servings: 2
		});
		expect(sheet.querySelector('.rc-print-art, .rc-print-notes, .rc-print-source, .rc-print-steps')).toBeNull();
		expect(sheet.querySelector('.rc-print-meta')?.textContent).toBe('Serves 2');
	});
});

describe('printRecipes', () => {
	afterEach(() => {
		document.body.innerHTML = '';
		document.documentElement.className = '';
	});

	it('shows the sheets for the print dialog, then takes them away', () => {
		const print = vi.spyOn(window, 'print').mockImplementation(() => {
			expect(document.documentElement.classList.contains('rc-printing')).toBe(true);
			expect(document.querySelectorAll('.rc-print .rc-print-sheet')).toHaveLength(2);
			expect(document.title).toBe('Recipes (2)');
		});
		document.title = 'Recipes — EXSCALIA';
		printRecipes(
			[
				{ recipe: recipe(), servings: 2 },
				{ recipe: recipe({ title: 'Shakshuka' }), servings: 3 }
			],
			'Recipes (2)'
		);
		expect(print).toHaveBeenCalledOnce();
		window.dispatchEvent(new Event('afterprint'));
		expect(document.querySelector('.rc-print')).toBeNull();
		expect(document.documentElement.classList.contains('rc-printing')).toBe(false);
		expect(document.title).toBe('Recipes — EXSCALIA');
		print.mockRestore();
	});

	it('clears a print the browser never closed before the next', () => {
		const print = vi.spyOn(window, 'print').mockImplementation(() => {});
		printRecipes([{ recipe: recipe(), servings: 2 }], 'One');
		printRecipes([{ recipe: recipe(), servings: 2 }], 'Two');
		expect(document.querySelectorAll('.rc-print')).toHaveLength(1);
		print.mockRestore();
	});
});

describe('printRequest', () => {
	it('reads the servings or all from ?print', () => {
		expect(printRequest(new URL('https://x/recipes/recipes/a.md?print=4'))).toBe(4);
		expect(printRequest(new URL('https://x/recipes?print=all'))).toBe('all');
		expect(printRequest(new URL('https://x/recipes/recipes/a.md?print='))).toBe(0);
		expect(printRequest(new URL('https://x/recipes/recipes/a.md'))).toBeNull();
	});

	it('names a print of many recipes', () => {
		expect(collectionTitle(3)).toBe('Recipes (3)');
	});
});
