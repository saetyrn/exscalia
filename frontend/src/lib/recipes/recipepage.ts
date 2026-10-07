// The recipe page's pure parts: the servings range, the caption line, the
// source link, ingredient groups, and the record a save sends back.
import { formatMinutes } from './format';
import type { Ingredient, Recipe, RecipeIn } from './types';

/** The slider runs from 1 to at least 12, further for a recipe written for more. */
export function servingsMax(base: number): number {
	return Math.max(12, base);
}

/** The servings to show: the cook's choice while it is a whole number in
 * range for this recipe, else the recipe's own (an edit that lowers the base
 * below a remembered choice puts the slider back). */
export function fitServings(chosen: number | null, base: number): number {
	if (chosen === null || !Number.isInteger(chosen)) return base;
	return chosen >= 1 && chosen <= servingsMax(base) ? chosen : base;
}

/** "×1.5": the factor every amount is multiplied by. */
export function factorText(servings: number, base: number): string {
	return `×${Number((servings / base).toFixed(2))}`;
}

/** Time and tags as one caption line: "10 min prep · 15 min cook · pasta". */
export function metaLine(
	recipe: Pick<RecipeIn, 'prep_minutes' | 'cook_minutes' | 'tags'>
): string {
	return [
		recipe.prep_minutes !== null ? `${formatMinutes(recipe.prep_minutes)} prep` : '',
		recipe.cook_minutes !== null ? `${formatMinutes(recipe.cook_minutes)} cook` : '',
		...recipe.tags
	]
		.filter(Boolean)
		.join(' · ');
}

/** The source as a link, for http and https URLs only; its host is the text. */
export function sourceLink(url: string | null | undefined): { href: string; host: string } | null {
	if (!url) return null;
	let parsed: URL;
	try {
		parsed = new URL(url.trim());
	} catch {
		return null;
	}
	if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null;
	const host = parsed.hostname.replace(/^www\./, '');
	return host ? { href: parsed.href, host } : null;
}

export interface IngredientGroup {
	name: string | null;
	items: Ingredient[];
}

/** Ingredients under their group names, in first-seen order. */
export function groupIngredients(ingredients: Ingredient[]): IngredientGroup[] {
	const groups: IngredientGroup[] = [];
	for (const ing of ingredients) {
		const name = ing.group?.trim() || null;
		let group = groups.find((g) => g.name === name);
		if (!group) {
			group = { name, items: [] };
			groups.push(group);
		}
		group.items.push(ing);
	}
	return groups;
}

/** The record a save sends: the whole recipe without its drawing (so the
 * stored one is kept) or the note's own bookkeeping. */
export function recipeInput(recipe: Recipe): RecipeIn {
	return {
		title: recipe.title,
		base_servings: recipe.base_servings,
		prep_minutes: recipe.prep_minutes,
		cook_minutes: recipe.cook_minutes,
		ingredients: recipe.ingredients,
		steps: recipe.steps,
		notes: recipe.notes,
		tags: recipe.tags,
		source: recipe.source
	};
}

/** The ingredients with one amount confirmed (no longer estimated). */
export function confirmIngredient(ingredients: Ingredient[], id: string): Ingredient[] {
	return ingredients.map((ing) => (ing.id === id ? { ...ing, estimated: false } : ing));
}

/** Only attachments draw as the source picture: a remote URL is the server's
 * to fetch and store, not the page's to load. */
export function localThumbnail(src: string | null | undefined): string | null {
	return src && src.startsWith('/api/attachments/') ? src : null;
}

// The servings the cook picked survive a reload of the tab, as in the recipe
// app, and no further. Storage can be missing or throw (private windows).
const servingsKey = (path: string) => `recipes:servings:${path}`;

export function savedServings(path: string): number | null {
	try {
		const raw = sessionStorage.getItem(servingsKey(path));
		const n = raw === null ? NaN : Number(raw);
		return Number.isInteger(n) ? n : null;
	} catch {
		return null;
	}
}

export function saveServings(path: string, servings: number): void {
	try {
		sessionStorage.setItem(servingsKey(path), String(servings));
	} catch {
		// Not remembered; the slider still works.
	}
}
