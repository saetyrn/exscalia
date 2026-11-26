// Mirrors backend/app/recipe_models.py. One format for every recipe, whatever
// its source. A recipe is a vault note, so its identity is the note's path and
// its version is the note's revision.

export type Unit =
	| ''
	| 'tsp'
	| 'tbsp'
	| 'cup'
	| 'ml'
	| 'l'
	| 'g'
	| 'kg'
	| 'oz'
	| 'lb'
	| 'pinch'
	| 'clove'
	| 'slice'
	| 'can'
	| 'bunch'
	| 'stick'
	| 'sprig';

export type Scaling = 'linear' | 'whole' | 'to_taste';

export type SourceType =
	| 'typed'
	| 'text'
	| 'website'
	| 'photo'
	| 'dish_photo'
	| 'youtube'
	| 'reel'
	| 'tiktok'
	| 'video';

export interface Ingredient {
	id: string;
	amount: number | null;
	unit: Unit;
	item: string;
	prep: string;
	scaling: Scaling;
	estimated: boolean;
	group: string | null;
}

export interface Step {
	/** Names ingredients as {id}; the recipe page shows the scaled amount. */
	text: string;
	timer_seconds: number | null;
}

export interface Source {
	type: SourceType;
	url: string | null;
	/** An /api/attachments/... URL once stored. */
	thumbnail: string | null;
}

/** What the client sends. An update without `art` keeps the stored drawing. */
export interface RecipeIn {
	title: string;
	base_servings: number;
	prep_minutes: number | null;
	cook_minutes: number | null;
	ingredients: Ingredient[];
	steps: Step[];
	/** The note body: free markdown. */
	notes: string;
	tags: string[];
	source: Source | null;
	/** ASCII art of the dish, rows joined with "\n". Drawn by the server. */
	art?: string;
	/** Same shape as `art`: each character is a palette key, "." for default ink. */
	art_colors?: string;
	art_palette?: Record<string, string>;
}

export interface Recipe extends RecipeIn {
	/** The note's vault path, e.g. "recipes/carbonara.md". */
	path: string;
	/** Content hash of the note, sent back as `base_revision` on update. */
	revision: string;
	created: string;
	modified: string;
	art: string;
	art_colors: string;
	art_palette: Record<string, string>;
}

export interface RecipeCard {
	path: string;
	title: string;
	total_minutes: number | null;
	thumbnail: string | null;
	tags: string[];
	modified: string;
	art: string;
	art_colors: string;
	art_palette: Record<string, string>;
}

/** What every import route returns: a recipe to review, plus what is missing. */
export interface Draft {
	recipe: RecipeIn;
	missing: string[];
	method: 'ai' | 'local' | 'schema_org';
}

export interface RecipeTagCount {
	tag: string;
	count: number;
}

/** GET /api/recipes: cards in menu order; tag counts cover the whole library. */
export interface RecipeList {
	recipes: RecipeCard[];
	tags: RecipeTagCount[];
}

export interface ArtStatus {
	drawing: boolean;
	error: string | null;
}

export interface RecipesStatus {
	/** Whether the AI step (photo and PDF reading, drawings) can run. */
	ai: boolean;
}

export const emptyRecipe = (): RecipeIn => ({
	title: '',
	base_servings: 2,
	prep_minutes: null,
	cook_minutes: null,
	ingredients: [],
	steps: [],
	notes: '',
	tags: [],
	source: { type: 'typed', url: null, thumbnail: null }
});
