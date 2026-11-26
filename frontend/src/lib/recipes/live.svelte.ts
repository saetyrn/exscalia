// Keeps the recipe cache in step with the vault while the user is in Recipes.
// A recipe can change behind an open page: a drawing lands, OMNIA edits it,
// another device saves. The server announces each write on the live stream,
// which the root layout re-broadcasts as `exscalia:live`; this refreshes what
// the cache holds for it, and the screens, which read the cache reactively,
// follow without knowing about it.
//
// - A write refreshes that recipe if it is cached, and every cached menu list.
// - The echo of this tab's own save leaves the recipe alone: the screen that
//   saved already remembered the server's answer. The lists still refresh,
//   since nothing else puts the new card in them.
// - A removal drops the recipe and its card at once; a write to it after that
//   (a restore from the Trash) fetches it back.
// - An OMNIA action that touched notes or recipes (`exscalia:data-changed`
//   from the chat) counts as a write to each path it names. A menu reorder
//   writes only the app config, so this is its only announcement.
// - A bare `exscalia:data-changed` is the root layout's resync after a gap in
//   the stream, whose events are lost: it refreshes the lists and the recipe
//   on screen (its page or its edit form). Not every cached recipe: the menu
//   prefetches each card it focuses, which can be dozens.
// - A burst (an OMNIA bulk edit) is one refresh, `delay` ms after its last
//   event, and each recipe in it is fetched once.
//
// The recipes layout installs it and the returned cleanup removes it, so it
// runs only while the user is in the section; the loaders revalidate on the
// way back in.
import { ApiError } from '$lib/api';
import { CLIENT_ID, type LiveNoteEvent } from '$lib/live';
import { cachedRecipe, forgetRecipe, loadRecipe, recipeGone, refreshLists } from './cache.svelte';
import { isRecipeNotePath } from './links';
import { recipePathAt } from './paths';

export interface RecipesLiveOptions {
	/** Where `exscalia:live` and `exscalia:data-changed` are dispatched
	 * (default `window`). */
	target?: EventTarget;
	/** Coalescing window in ms (default 500). */
	delay?: number;
	/** The pathname on screen (default the window's). */
	pathname?: () => string;
}

/** Start listening; returns the cleanup, which also drops a pending burst. */
export function watchRecipesLive(options: RecipesLiveOptions = {}): () => void {
	const target = options.target ?? (typeof window === 'undefined' ? null : window);
	if (!target) return () => {};
	const delay = options.delay ?? 500;
	const pathname =
		options.pathname ?? (() => (typeof location === 'undefined' ? '' : location.pathname));

	let timer: ReturnType<typeof setTimeout> | null = null;
	// Per path: the newest revision announced, and whether any write in the
	// burst came from somewhere other than this tab.
	let writes = new Map<string, { revision?: string | number; foreign: boolean }>();

	const flush = () => {
		timer = null;
		const due = writes;
		writes = new Map();
		for (const [path, write] of due) {
			const cached = cachedRecipe(path);
			// A removed recipe written again (restored) is fetched back, so a
			// page showing it as gone follows.
			if (cached ? !write.foreign || cached.revision === write.revision : !recipeGone(path))
				continue;
			void loadRecipe(path).catch((cause) => {
				// No longer a recipe (its mark removed, or moved out of the folder).
				if (cause instanceof ApiError && cause.status === 404) forgetRecipe(path);
				// Anything else keeps the cached copy on screen.
			});
		}
		void refreshLists();
	};

	const schedule = () => {
		if (timer) clearTimeout(timer);
		timer = setTimeout(flush, delay);
	};

	const onLive = (event: Event) => {
		const detail = (event as CustomEvent<LiveNoteEvent | undefined>).detail;
		if (!detail) return;
		if (detail.type === 'note' && isRecipeNotePath(detail.path)) {
			const before = writes.get(detail.path);
			writes.set(detail.path, {
				revision: detail.revision,
				foreign: (before?.foreign ?? false) || detail.origin !== CLIENT_ID
			});
			schedule();
		} else if (detail.type === 'note_removed' && isRecipeNotePath(detail.path)) {
			writes.delete(detail.path);
			forgetRecipe(detail.path);
			schedule();
		} else if (detail.type === 'note_moved') {
			// The server follows a move with note_removed and note, which do the
			// rest; the old path is dropped now so no screen shows it meanwhile.
			if (isRecipeNotePath(detail.path)) {
				writes.delete(detail.path);
				forgetRecipe(detail.path);
			}
			if (isRecipeNotePath(detail.path) || isRecipeNotePath(detail.new_path)) schedule();
		}
	};

	const onDataChanged = (event: Event) => {
		const detail = (event as CustomEvent<{ refresh?: string[]; paths?: string[] } | null>).detail;
		if (!detail) {
			// The resync after a gap in the live stream.
			const open = recipePathAt(pathname());
			if (open) writes.set(open, { revision: writes.get(open)?.revision, foreign: true });
			schedule();
			return;
		}
		const refresh = detail.refresh ?? [];
		if (!refresh.includes('recipes') && !refresh.includes('notes')) return;
		for (const path of detail.paths ?? []) {
			if (isRecipeNotePath(path))
				writes.set(path, { revision: writes.get(path)?.revision, foreign: true });
		}
		schedule();
	};

	target.addEventListener('exscalia:live', onLive);
	target.addEventListener('exscalia:data-changed', onDataChanged);
	return () => {
		target.removeEventListener('exscalia:live', onLive);
		target.removeEventListener('exscalia:data-changed', onDataChanged);
		if (timer) clearTimeout(timer);
		timer = null;
		writes = new Map();
	};
}
