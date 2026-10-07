// @vitest-environment jsdom
// The recipe page's parts against the browser build of Svelte: the servings
// row reports the servings the factor is made from, the ingredients and the
// steps show amounts scaled by that factor, and the owner's acceptance test,
// the worked example written for 2, reads right at 3.
import { flushSync, mount, unmount, type Component } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import IngredientList from './IngredientList.svelte';
import ServingsSlider from './ServingsSlider.svelte';
import StepList from './StepList.svelte';
import type { Ingredient, Step } from './types';

let cleanups: (() => void)[] = [];
afterEach(() => {
	for (const c of cleanups) c();
	cleanups = [];
});

function render<P extends Record<string, unknown>>(component: Component<P>, props: P) {
	const target = document.body.appendChild(document.createElement('div'));
	const instance = mount(component, { target, props });
	flushSync();
	cleanups.push(() => {
		unmount(instance);
		target.remove();
	});
	return target;
}

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

// The README's worked example, written for 2 (scaling.test.ts has the numbers).
const noodles: Ingredient[] = [
	ing({ id: 'noodles', amount: 200, unit: 'g', item: 'noodles' }),
	ing({ id: 'soy', amount: 4, unit: 'tsp', item: 'soy sauce' }),
	ing({ id: 'stock', amount: 2 / 3, unit: 'cup', item: 'stock' }),
	ing({ id: 'egg', amount: 1, unit: '', item: 'egg', scaling: 'whole' }),
	ing({ id: 'salt', amount: 0.5, unit: 'tsp', item: 'salt', scaling: 'to_taste' })
];

const rows = (el: HTMLElement) =>
	[...el.querySelectorAll('li')].map((li) => ({
		amt: li.querySelector('.amt')?.textContent,
		item: li.querySelector('.item')?.textContent,
		markers: [...li.querySelectorAll('.marker')].map((m) => m.textContent)
	}));

describe('ServingsSlider', () => {
	function slider(base: number, value: number) {
		const onchange = vi.fn();
		const el = render(ServingsSlider, { base, value, onchange });
		return { el, onchange, input: el.querySelector('input[type="range"]') as HTMLInputElement };
	}

	it('runs from 1 to at least 12 and starts at the value', () => {
		const { el, input } = slider(2, 2);
		expect([input.min, input.max, input.step, input.value]).toEqual(['1', '12', '1', '2']);
		expect(el.querySelector('output')?.textContent).toBe('2');
		expect(el.querySelector('label')?.getAttribute('for')).toBe(input.id);
		expect(el.querySelector('.note')?.textContent?.trim()).toBe('Written for 2');
		expect(slider(20, 20).input.max).toBe('20');
	});

	it('reports each move as the new servings', () => {
		const { input, onchange } = slider(2, 2);
		input.value = '3';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		expect(onchange).toHaveBeenCalledExactlyOnceWith(3);
	});

	it('shows the factor away from the base, and resets to it', () => {
		const { el, onchange, input } = slider(2, 3);
		expect(input.value).toBe('3');
		expect(input.getAttribute('aria-valuetext')).toBe('3 servings');
		const note = el.querySelector('.note') as HTMLElement;
		expect(note.textContent).toContain('×1.5');
		const reset = note.querySelector('button') as HTMLButtonElement;
		expect(reset.textContent).toBe('Reset to 2');
		reset.click();
		expect(onchange).toHaveBeenCalledExactlyOnceWith(2);
	});

	it('hands focus to the slider when Reset goes away under it', () => {
		const { el, input } = slider(2, 3);
		const reset = el.querySelector('.note button') as HTMLButtonElement;
		reset.focus();
		reset.click();
		expect(document.activeElement).toBe(input);
	});
});

describe('IngredientList', () => {
	it('reads the worked example as written at factor 1', () => {
		const el = render(IngredientList, { ingredients: noodles, factor: 1 });
		expect(rows(el)).toEqual([
			{ amt: '200 g', item: 'noodles', markers: [] },
			{ amt: '4 tsp', item: 'soy sauce', markers: [] },
			{ amt: '⅔ cup', item: 'stock', markers: [] },
			{ amt: '1', item: 'egg', markers: [] },
			{ amt: '½ tsp', item: 'salt', markers: [] }
		]);
	});

	it('scales the worked example from 2 to 3 servings', () => {
		const el = render(IngredientList, { ingredients: noodles, factor: 3 / 2 });
		expect(rows(el)).toEqual([
			{ amt: '300 g', item: 'noodles', markers: [] },
			{ amt: '2 tbsp', item: 'soy sauce', markers: [] },
			{ amt: '1 cup', item: 'stock', markers: [] },
			{ amt: '2', item: 'eggs', markers: ['rounded'] },
			{ amt: '¾ tsp', item: 'salt', markers: ['starting point'] }
		]);
	});

	it('groups under their names, adds prep, and writes "to taste" for no amount', () => {
		const el = render(IngredientList, {
			ingredients: [
				ing({ id: 'p', amount: 400, unit: 'g', item: 'spaghetti', group: 'Pasta' }),
				ing({ id: 'b', amount: 2, unit: 'tbsp', item: 'butter', prep: 'softened', group: 'Sauce' }),
				ing({ id: 'pep', amount: null, item: 'black pepper', scaling: 'to_taste', group: 'Sauce' })
			],
			factor: 2
		});
		expect([...el.querySelectorAll('h3')].map((h) => h.textContent)).toEqual(['Pasta', 'Sauce']);
		expect(rows(el)).toEqual([
			{ amt: '800 g', item: 'spaghetti', markers: [] },
			{ amt: '4 tbsp', item: 'butter, softened', markers: [] },
			{ amt: 'to taste', item: 'black pepper', markers: ['starting point'] }
		]);
	});

	it('confirms an estimated amount by its id', () => {
		const onconfirm = vi.fn();
		const el = render(IngredientList, {
			ingredients: [ing({ id: 'oil', amount: 2, unit: 'tbsp', item: 'olive oil', estimated: true })],
			factor: 1,
			onconfirm
		});
		const button = el.querySelector('button.marker') as HTMLButtonElement;
		expect(button.textContent).toBe('estimated');
		button.click();
		expect(onconfirm).toHaveBeenCalledExactlyOnceWith('oil');
	});
});

describe('StepList', () => {
	const steps: Step[] = [
		{ text: 'Boil {noodles}, then toss with {soy} and {stock}.', timer_seconds: 480 },
		{ text: 'Beat {egg} with {salt}; add {chili}.', timer_seconds: null }
	];

	it('shows each reference as the amount for the factor', () => {
		const el = render(StepList, { steps, ingredients: noodles, factor: 1.5 });
		const items = [...el.querySelectorAll('li')];
		expect(items.map((li) => li.querySelector('p')?.textContent)).toEqual([
			'Boil 300 g noodles, then toss with 2 tbsp soy sauce and 1 cup stock.',
			'Beat 2 eggs with ¾ tsp salt; add chili.'
		]);
		expect([...el.querySelectorAll('.ref:not(.missing)')].map((r) => r.textContent)).toEqual([
			'300 g noodles',
			'2 tbsp soy sauce',
			'1 cup stock',
			'2 eggs',
			'¾ tsp salt'
		]);
		// A reference to no ingredient is marked, not dropped.
		expect(el.querySelector('.ref.missing')?.textContent).toBe('chili');
	});

	it('shows the stored amounts at factor 1', () => {
		const el = render(StepList, { steps, ingredients: noodles, factor: 1 });
		expect(el.querySelector('li p')?.textContent).toBe(
			'Boil 200 g noodles, then toss with 4 tsp soy sauce and ⅔ cup stock.'
		);
	});

	it('shows a timer and does not run it', () => {
		vi.useFakeTimers();
		try {
			const el = render(StepList, { steps, ingredients: noodles, factor: 1 });
			const timers = [...el.querySelectorAll('.timer')];
			expect(timers.map((t) => t.textContent)).toEqual(['8 min']);
			vi.advanceTimersByTime(10 * 60 * 1000);
			flushSync();
			expect(el.querySelector('.timer')?.textContent).toBe('8 min');
		} finally {
			vi.useRealTimers();
		}
	});

	it('draws nothing without steps', () => {
		const el = render(StepList, { steps: [], ingredients: noodles, factor: 1 });
		expect(el.querySelector('section')).toBeNull();
	});
});
