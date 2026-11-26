// The form model under the browser build of Svelte: the derived ids and
// messages follow the rows as they change.
import { flushSync } from 'svelte';
import { describe, expect, it } from 'vitest';
import { RecipeFormModel } from './form.svelte';
import { emptyForm, fromRecipe } from './model';
import { emptyRecipe } from '../types';

describe('RecipeFormModel', () => {
	it('adds, reorders and removes rows by key', () => {
		const model = new RecipeFormModel(emptyForm());
		const first = model.form.ingredients[0].key;
		model.form.ingredients[0].item = 'flour';
		const second = model.addIngredient();
		model.form.ingredients[1].item = 'sugar';
		model.moveIngredient(second, -1);
		expect(model.form.ingredients.map((row) => row.item)).toEqual(['sugar', 'flour']);
		model.moveIngredient(second, -1);
		expect(model.form.ingredients.map((row) => row.key)).toEqual([second, first]);
		model.removeIngredient(second);
		expect(model.form.ingredients.map((row) => row.item)).toEqual(['flour']);

		const step = model.addStep();
		model.moveStep(step, -1);
		expect(model.form.steps[0].key).toBe(step);
		model.removeStep(step);
		expect(model.form.steps).toHaveLength(1);
	});

	it('derives ids as rows are typed', () => {
		const model = new RecipeFormModel(emptyForm());
		const key = model.form.ingredients[0].key;
		expect(model.ids.get(key)).toBeUndefined();
		model.form.ingredients[0].item = 'Brown sugar';
		flushSync();
		expect(model.ids.get(key)).toBe('brown-sugar');
	});

	it('shows the client’s messages only after a save was tried, then as you type', () => {
		const model = new RecipeFormModel(emptyForm());
		expect(model.errors).toEqual({});
		expect(model.submit()).toBeNull();
		expect(model.errors).toEqual({ title: 'Title required' });
		model.form.title = 'Bread';
		expect(model.errors).toEqual({});
		expect(model.submit()).toMatchObject({ title: 'Bread' });
	});

	it('keeps a server refusal until the next save, and a reload clears it', () => {
		const model = new RecipeFormModel(fromRecipe({ ...emptyRecipe(), title: 'Bread' }));
		model.refused('title: String should have at most 200 characters');
		expect(model.errors).toEqual({ title: 'String should have at most 200 characters' });
		expect(model.submit()).not.toBeNull();
		expect(model.errors).toEqual({});
		model.refused('steps: bad');
		model.load(fromRecipe({ ...emptyRecipe(), title: 'Rye' }));
		expect(model.errors).toEqual({});
	});

	it('knows an untouched form', () => {
		const model = new RecipeFormModel(fromRecipe({ ...emptyRecipe(), title: 'Bread' }));
		expect(model.pristine).toBe(true);
		model.form.title = 'Rye';
		expect(model.pristine).toBe(false);
	});
});
