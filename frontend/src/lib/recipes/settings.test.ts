// The Recipes effort select reads both the settings payload the final server
// sends and the older one, which carried no levels in effect.
import { describe, expect, it } from 'vitest';
import { recipesEffortTitle, recipesEffortValue } from './settings';

describe('recipesEffortValue', () => {
	it('reads the owner’s setting from the current payload', () => {
		const unset = {
			reasoning_levels: {},
			recipes_reasoning: 'inherit',
			recipes_reasoning_in_effect: { import: 'medium', drawing: 'low' }
		};
		expect(recipesEffortValue(unset)).toBe('inherit');
		const high = {
			reasoning_levels: { recipes: 'high' },
			recipes_reasoning: 'high',
			recipes_reasoning_in_effect: { import: 'high', drawing: 'high' }
		};
		expect(recipesEffortValue(high)).toBe('high');
	});

	it('reads the override list from an older payload, whose field was the level in effect', () => {
		expect(recipesEffortValue({ reasoning_levels: {}, recipes_reasoning: 'low' })).toBe('inherit');
		expect(
			recipesEffortValue({ reasoning_levels: { recipes: 'max' }, recipes_reasoning: 'max' })
		).toBe('max');
	});
});

describe('recipesEffortTitle', () => {
	it('names the level each job runs at', () => {
		expect(
			recipesEffortTitle({
				reasoning_levels: {},
				recipes_reasoning: 'inherit',
				recipes_reasoning_in_effect: { import: 'medium', drawing: 'low' }
			})
		).toBe('Import: Medium · Drawing: Low');
	});

	it('says nothing when the server does not', () => {
		expect(recipesEffortTitle({ reasoning_levels: {}, recipes_reasoning: 'low' })).toBe('');
	});
});
