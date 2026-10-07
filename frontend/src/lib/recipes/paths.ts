// Where recipe screens live. A recipe's identity is its vault path
// ("recipes/carbonara.md"); its page is /recipes/ plus that path, the way a
// media item's page is /media/ plus its path, and its edit form is
// /recipes/edit/ plus the whole path.
import { encodePath } from '$lib/api';

export const RECIPES_HOME = '/recipes';
export const RECIPES_ADD = '/recipes/add';
export const RECIPES_REVIEW = '/recipes/review';

/** The recipe page for a vault path. */
export function recipeHref(path: string): string {
	return `/recipes/${encodePath(path)}`;
}

/** The edit form for a vault path. */
export function recipeEditHref(path: string): string {
	return `/recipes/edit/${encodePath(path)}`;
}

/** The vault path behind /recipes/recipes/[...file]: the route's literal
 * second segment is the folder, the rest param the file. */
export function recipePathFromParam(file: string): string {
	return `recipes/${file}`;
}

/** The vault path behind /recipes/edit/[...file], which carries all of it. */
export function editPathFromParam(file: string): string {
	return file;
}

/** The recipe a URL's pathname shows, on its page or its edit form; null for
 * any other screen. */
export function recipePathAt(pathname: string): string | null {
	const match = /^\/recipes\/(recipes|edit)\/(.+)$/.exec(pathname);
	if (!match) return null;
	let file: string;
	try {
		file = match[2].split('/').map(decodeURIComponent).join('/');
	} catch {
		return null; // a malformed escape names no recipe
	}
	return match[1] === 'edit' ? editPathFromParam(file) : recipePathFromParam(file);
}

/** The browser tab title for a recipes page; no title is the menu. */
export function pageTitle(title?: string | null): string {
	const name = title?.trim();
	return name ? `${name} — Recipes` : 'Recipes — EXSCALIA';
}
