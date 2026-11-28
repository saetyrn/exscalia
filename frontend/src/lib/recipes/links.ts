// Where the app sends a note it lists by name. A recipe note opens its recipe
// page, as the palette sends a media item to its media page; any other note
// opens in the editor. The editor's own tools (version history, conflict
// copies, staged edits, a task's source line) keep /note/: there the text is
// the point.
import { api, encodePath } from '$lib/api';
import { isGalleryNotePath, pinHref } from '$lib/gallery/paths';
import { recipeHref } from './paths';

/** A note in the recipes folder, nested folders included. */
export function isRecipeNotePath(path: string | null | undefined): path is string {
	return !!path && path.startsWith('recipes/') && path.endsWith('.md');
}

/** The page a listed note opens: its recipe page, a Gallery picture's page,
 * or the note editor. */
export function noteHref(path: string): string {
	if (isRecipeNotePath(path)) return recipeHref(path);
	if (isGalleryNotePath(path)) return pinHref(path);
	return `/note/${encodePath(path)}`;
}

/** Where a recipe page sends a path the recipe routes do not know: an
 * ordinary note in the recipes folder (no `recipe: 1`), which every list links
 * as a recipe, opens in the editor. Null when there is no such note. */
export async function ordinaryNoteHref(path: string): Promise<string | null> {
	try {
		await api.readNote(path);
		return `/note/${encodePath(path)}`;
	} catch {
		return null;
	}
}
