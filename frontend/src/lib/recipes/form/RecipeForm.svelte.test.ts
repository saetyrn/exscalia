// @vitest-environment jsdom
// The shared form, mounted: the rows the user adds, moves and removes, the
// references steps keep, and the title it will not save without.
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RecipeIn } from '../types';
import { RecipeFormModel } from './form.svelte';
import { emptyForm, fromRecipe } from './model';
import RecipeForm from './RecipeForm.svelte';

const stored: RecipeIn = {
	title: 'Noodles',
	base_servings: 2,
	prep_minutes: null,
	cook_minutes: null,
	ingredients: [
		{ id: 'noodles', amount: 200, unit: 'g', item: 'noodles', prep: '', scaling: 'linear', estimated: false, group: null },
		{ id: 'butter', amount: 2, unit: 'tbsp', item: 'butter', prep: '', scaling: 'linear', estimated: false, group: null }
	],
	steps: [{ text: 'Toss the {noodles} in {butter}.', timer_seconds: null }],
	notes: '',
	tags: [],
	source: null
};

// jsdom has no Web Animations; animate:flip only needs them to exist.
Object.assign(Element.prototype, {
	getAnimations: () => [],
	animate: () => ({ cancel() {}, finished: Promise.resolve(), onfinish: null })
});

let app: ReturnType<typeof mount> | null = null;

afterEach(() => {
	if (app) unmount(app);
	app = null;
	document.body.innerHTML = '';
});

function render(model: RecipeFormModel, onsave = vi.fn()) {
	app = mount(RecipeForm, {
		target: document.body,
		props: { model, caption: 'Edit recipe', saveLabel: 'Save', onsave }
	});
	flushSync();
	return onsave;
}

const one = <T extends HTMLElement = HTMLElement>(selector: string) => document.querySelector<T>(selector)!;
const all = (selector: string) => [...document.querySelectorAll<HTMLElement>(selector)];

function type(field: HTMLInputElement | HTMLTextAreaElement, value: string) {
	field.value = value;
	field.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

function click(el: HTMLElement) {
	el.click();
	flushSync();
}

const items = () => all('.ing-item').map((el) => (el as HTMLInputElement).value);
const saveButton = () => all('button').find((b) => b.textContent?.trim() === 'Save')!;

describe('RecipeForm', () => {
	it('adds, moves and removes ingredient rows', () => {
		const model = new RecipeFormModel(fromRecipe(stored));
		render(model);
		expect(items()).toEqual(['noodles', 'butter']);

		click(one('[data-action="add-ing"]'));
		type(all('.ing-item')[2] as HTMLInputElement, 'garlic');
		expect(items()).toEqual(['noodles', 'butter', 'garlic']);
		expect(all('.ing-id').map((el) => el.textContent)).toEqual(['{noodles}', '{butter}', '{garlic}']);

		const garlicRow = one(`[data-ing="${model.form.ingredients[2].key}"]`);
		click(garlicRow.querySelector<HTMLElement>('[data-action="up"]')!);
		expect(items()).toEqual(['noodles', 'garlic', 'butter']);
		expect(one('[data-ing] [data-action="up"]').hasAttribute('disabled')).toBe(true);

		click(all('[data-action="remove"]')[0]);
		expect(items()).toEqual(['garlic', 'butter']);
		expect(model.form.ingredients.map((row) => row.item)).toEqual(['garlic', 'butter']);
	});

	it('keeps a step’s reference when the item is renamed', async () => {
		const onsave = vi.fn();
		const model = new RecipeFormModel(fromRecipe(stored));
		render(model, onsave);
		type(all('.ing-item')[1] as HTMLInputElement, 'salted butter');
		expect(all('.ing-id')[1].textContent).toBe('{butter}');
		click(saveButton());
		await Promise.resolve();
		const saved: RecipeIn = onsave.mock.calls[0][0];
		expect(saved.ingredients[1]).toMatchObject({ id: 'butter', item: 'salted butter' });
		expect(saved.steps[0].text).toBe('Toss the {noodles} in {butter}.');
	});

	it('will not save without a title, and says so on the field', async () => {
		const onsave = vi.fn();
		const model = new RecipeFormModel(emptyForm());
		render(model, onsave);
		click(saveButton());
		await Promise.resolve();
		expect(onsave).not.toHaveBeenCalled();
		const title = one<HTMLInputElement>('.rc-title-input');
		expect(title.getAttribute('aria-invalid')).toBe('true');
		expect(one('#rf-title-error').textContent).toBe('Title required');

		type(title, 'Toast');
		expect(document.querySelector('#rf-title-error')).toBeNull();
		click(saveButton());
		await Promise.resolve();
		expect(onsave).toHaveBeenCalledTimes(1);
		expect(onsave.mock.calls[0][0]).toMatchObject({ title: 'Toast', ingredients: [], steps: [] });
	});

	it('shows a server refusal by field', () => {
		const model = new RecipeFormModel(fromRecipe(stored));
		render(model);
		model.refused('base_servings: Input should be less than or equal to 100; text: Field required');
		flushSync();
		expect(all('.field-error').map((el) => el.textContent)).toEqual([
			'Input should be less than or equal to 100',
			'Field required'
		]);
	});
});
