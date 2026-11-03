// The edit form while the stored recipe moves on behind it: a drawing landing
// (which bumps the revision and nothing the form shows) must not turn the next
// save into a 409 that throws the typed edits away.
import { describe, expect, it } from 'vitest';
import type { Recipe } from '../types';
import { emptyRecipe } from '../types';
import { followStored } from './save';

const stored = (over: Partial<Recipe> = {}): Recipe => ({
	...emptyRecipe(),
	title: 'Dal',
	path: 'recipes/dal.md',
	revision: 'r1',
	created: '2026-10-04T12:00:00Z',
	modified: '2026-10-04T12:00:00Z',
	art: '',
	art_colors: '',
	art_palette: {},
	...over
});

describe('followStored', () => {
	it('adopts the new revision of a drawing landing, typed edits or not', () => {
		const drawn = stored({ revision: 'r2', art: ' (~~) ', art_colors: ' aaaa ', art_palette: { a: '#fff' } });
		expect(followStored(stored(), drawn, false)).toBe('adopt');
		expect(followStored(stored(), drawn, true)).toBe('adopt');
	});

	it('loads a real change into an untouched form, and leaves a typed one to the save', () => {
		const renamed = stored({ revision: 'r2', title: 'Red dal' });
		expect(followStored(stored(), renamed, true)).toBe('load');
		expect(followStored(stored(), renamed, false)).toBe('keep');
	});

	it('does nothing for the revision it already holds', () => {
		expect(followStored(stored(), stored(), false)).toBe('same');
		expect(followStored(stored(), stored({ title: 'x' }), true)).toBe('same');
	});
});
