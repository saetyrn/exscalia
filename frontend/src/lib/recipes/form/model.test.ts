import { describe, expect, it } from 'vitest';
import type { Recipe, RecipeIn } from '../types';
import {
	assignIds,
	blankIngredient,
	emptyForm,
	formatAmount,
	fromRecipe,
	move,
	parseAmount,
	parseTags,
	serverErrors,
	slug,
	toRecipeIn,
	type FormIngredient
} from './model';

const recipe = (over: Partial<RecipeIn> = {}): RecipeIn => ({
	title: 'Garlic butter noodles',
	base_servings: 2,
	prep_minutes: 5,
	cook_minutes: 10,
	ingredients: [
		{ id: 'noodles', amount: 200, unit: 'g', item: 'noodles', prep: '', scaling: 'linear', estimated: false, group: null },
		{ id: 'butter', amount: 2, unit: 'tbsp', item: 'butter', prep: 'softened', scaling: 'linear', estimated: true, group: 'Sauce' },
		{ id: 'garlic', amount: 1 / 3, unit: 'clove', item: 'garlic', prep: '', scaling: 'whole', estimated: false, group: null }
	],
	steps: [
		{ text: 'Boil the {noodles}.', timer_seconds: 100 },
		{ text: 'Melt {butter} with {garlic}.', timer_seconds: null }
	],
	notes: 'Good with chili.',
	tags: ['pasta', 'quick'],
	source: { type: 'website', url: 'https://example.com/noodles', thumbnail: 'https://example.com/n.jpg' },
	...over
});

const row = (item: string, id = ''): FormIngredient => ({ ...blankIngredient(), item, id });

describe('ingredient ids', () => {
	it('takes an id from the first three words of an item', () => {
		expect(slug('Unsalted butter, softened and cold')).toBe('unsalted-butter-softened');
		expect(slug('Crème fraîche')).toBe('creme-fraiche');
		expect(slug('  ')).toBe('item');
		expect(slug('x'.repeat(80)).length).toBeLessThanOrEqual(48);
	});

	it('keeps the ids rows have and gives new rows free ones', () => {
		const rows = [row('butter', 'butter'), row('Butter'), row('salted butter', 'butter'), row(''), row('flour')];
		const ids = assignIds(rows);
		expect(rows.map((r) => ids.get(r.key))).toEqual(['butter', 'butter-3', 'butter-2', undefined, 'flour']);
		expect(new Set(ids.values()).size).toBe(ids.size);
	});

	it('keeps an id when the item is renamed', () => {
		const form = fromRecipe(recipe());
		form.ingredients[1].item = 'unsalted butter';
		const { recipe: out } = toRecipeIn(form);
		expect(out.ingredients[1]).toMatchObject({ id: 'butter', item: 'unsalted butter' });
		expect(out.steps[1].text).toBe('Melt {butter} with {garlic}.');
	});

	it('leaves the steps alone when an ingredient is removed', () => {
		const form = fromRecipe(recipe());
		form.ingredients.splice(1, 1);
		const { recipe: out, errors } = toRecipeIn(form);
		expect(errors).toEqual({});
		expect(out.ingredients.map((i) => i.id)).toEqual(['noodles', 'garlic']);
		expect(out.steps[1].text).toBe('Melt {butter} with {garlic}.');
	});
});

describe('amounts', () => {
	it('reads decimals, commas and fractions', () => {
		expect(parseAmount('')).toBeNull();
		expect(parseAmount(' 2 ')).toBe(2);
		expect(parseAmount('2.5')).toBe(2.5);
		expect(parseAmount('2,5')).toBe(2.5);
		expect(parseAmount('.5')).toBe(0.5);
		expect(parseAmount('1/2')).toBe(0.5);
		expect(parseAmount('1 1/2')).toBe(1.5);
		expect(parseAmount('1½')).toBe(1.5);
		expect(parseAmount('¾')).toBe(0.75);
		expect(parseAmount('a pinch')).toBeNaN();
		expect(parseAmount('1/0')).toBeNaN();
		expect(parseAmount('-2')).toBeNaN();
	});

	it('shows a stored amount short and saves it exactly when untouched', () => {
		expect(formatAmount(1 / 3)).toBe('0.333');
		expect(formatAmount(null)).toBe('');
		const form = fromRecipe(recipe());
		expect(form.ingredients[2].amount).toBe('0.333');
		expect(toRecipeIn(form).recipe.ingredients[2].amount).toBe(1 / 3);
		form.ingredients[2].amount = '1/4';
		expect(toRecipeIn(form).recipe.ingredients[2].amount).toBe(0.25);
	});
});

describe('form and record', () => {
	it('round-trips a recipe and leaves the art out', () => {
		const stored: Recipe = {
			...recipe(),
			path: 'recipes/garlic-butter-noodles.md',
			revision: 'abc',
			created: '2026-10-04T12:00:00Z',
			modified: '2026-10-04T12:00:00Z',
			art: ' .-. ',
			art_colors: ' aaa ',
			art_palette: { a: '#ffffff' }
		};
		const { recipe: out, errors } = toRecipeIn(fromRecipe(stored));
		expect(errors).toEqual({});
		expect(out).toEqual(recipe());
		expect('art' in out).toBe(false);
	});

	it('cleans the record: blank rows dropped, text trimmed, tags parsed, timers in seconds', () => {
		const form = emptyForm();
		form.title = '  Toast ';
		form.servings = '1';
		form.tags = 'Breakfast, quick,, breakfast ';
		form.ingredients[0].item = ' bread ';
		form.ingredients[0].group = '  ';
		form.ingredients.push(blankIngredient());
		form.steps[0].text = ' Toast the {bread}. ';
		form.steps[0].timer = '1.5';
		form.notes = '\n Crisp. \n';
		const { recipe: out, errors } = toRecipeIn(form);
		expect(errors).toEqual({});
		expect(out).toMatchObject({
			title: 'Toast',
			base_servings: 1,
			tags: ['breakfast', 'quick'],
			notes: 'Crisp.',
			ingredients: [{ id: 'bread', item: 'bread', amount: null, group: null }],
			steps: [{ text: 'Toast the {bread}.', timer_seconds: 90 }],
			source: { type: 'typed', url: null, thumbnail: null }
		});
	});

	it('keeps a loaded timer exact', () => {
		const form = fromRecipe(recipe());
		expect(form.steps[0].timer).toBe('1.67');
		expect(toRecipeIn(form).recipe.steps[0].timer_seconds).toBe(100);
	});

	it('names what the server would refuse', () => {
		const form = fromRecipe(recipe());
		form.title = ' ';
		form.servings = '0';
		form.prep = '2.5';
		form.cook = 'ten';
		form.ingredients[0].amount = 'some';
		form.steps[0].timer = 'soon';
		const { errors } = toRecipeIn(form);
		expect(errors).toEqual({
			title: 'Title required',
			servings: '1 to 100',
			prep: 'Whole minutes',
			cook: 'Whole minutes',
			[`amount:${form.ingredients[0].key}`]: 'Not an amount',
			[`timer:${form.steps[0].key}`]: 'Not a number'
		});
		form.title = 'x'.repeat(201);
		form.servings = '101';
		expect(toRecipeIn(form).errors).toMatchObject({ title: 'Up to 200 characters', servings: '1 to 100' });
	});

	it('reads a forgiving stored draft', () => {
		const form = fromRecipe({ title: 'Soup', ingredients: [{ item: 'leek' } as never] });
		expect(form.servings).toBe('2');
		expect(form.ingredients[0]).toMatchObject({ id: '', unit: '', scaling: 'linear', group: '' });
		expect(toRecipeIn(form).recipe.ingredients[0].id).toBe('leek');
	});
});

describe('helpers', () => {
	it('moves within bounds only', () => {
		expect(move([1, 2, 3], 0, 1)).toEqual([2, 1, 3]);
		expect(move([1, 2, 3], 2, 0)).toEqual([3, 1, 2]);
		expect(move([1, 2, 3], 0, -1)).toEqual([1, 2, 3]);
		expect(move([1, 2, 3], 2, 3)).toEqual([1, 2, 3]);
	});

	it('parses tags', () => {
		expect(parseTags('')).toEqual([]);
		expect(parseTags('A, b ,a')).toEqual(['a', 'b']);
	});

	it('turns a 422 detail into field messages', () => {
		expect(
			serverErrors(
				'title: String should have at least 1 character; item: String should have at least 1 character; timer_seconds: Input should be greater than or equal to 1; Invalid request'
			)
		).toEqual({
			title: 'String should have at least 1 character',
			ingredients: 'String should have at least 1 character',
			steps: 'Input should be greater than or equal to 1',
			form: 'Invalid request'
		});
		expect(serverErrors('base_servings: Input should be less than or equal to 100')).toEqual({
			servings: 'Input should be less than or equal to 100'
		});
	});
});
