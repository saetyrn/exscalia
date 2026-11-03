import { describe, expect, it } from 'vitest';
import { isRecipeNotePath, noteHref } from './links';

describe('noteHref', () => {
	it('sends a recipe note to its recipe page', () => {
		expect(noteHref('recipes/carbonara.md')).toBe('/recipes/recipes/carbonara.md');
		expect(noteHref('recipes/weeknight/dal #2.md')).toBe('/recipes/recipes/weeknight/dal%20%232.md');
	});

	it('sends any other note to the editor', () => {
		expect(noteHref('notes/recipes.md')).toBe('/note/notes/recipes.md');
		expect(noteHref('recipes-old/pie.md')).toBe('/note/recipes-old/pie.md');
		expect(noteHref('media/Dune.md')).toBe('/note/media/Dune.md');
		expect(noteHref('50% done.md')).toBe('/note/50%25%20done.md');
	});
});

describe('isRecipeNotePath', () => {
	it('takes notes in the folder and anything nested in it', () => {
		expect(isRecipeNotePath('recipes/pie.md')).toBe(true);
		expect(isRecipeNotePath('recipes/weeknight/dal.md')).toBe(true);
		expect(isRecipeNotePath('recipes/photo.jpg')).toBe(false);
		expect(isRecipeNotePath('recipes')).toBe(false);
		expect(isRecipeNotePath('recipes-old/pie.md')).toBe(false);
		expect(isRecipeNotePath(undefined)).toBe(false);
	});
});
