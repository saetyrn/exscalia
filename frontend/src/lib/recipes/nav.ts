// Slide navigation between recipe screens, after the recipe app's nav.ts.
// Going deeper (open a recipe, the plus, edit) slides the new page in from the
// right; going back slides it away, and pops history when the page was
// entered with forward(), so the browser's Back stays in step.
import { goto } from '$app/navigation';
import { page } from '$app/state';
import { isSuperseded, requestSlide, type Direction } from './transition';

/** A goto a newer navigation superseded mid-slide never rendered; it settles
 * quietly, as SvelteKit settles any navigation a newer one aborted. */
function quietly(navigation: Promise<void>): Promise<void> {
	return navigation.catch((error: unknown) => {
		if (!isSuperseded(error)) throw error;
	});
}

/** Slide forward to `href`. The entry is marked so back() from there pops it.
 * `replace` swaps the current entry instead (a saved draft becoming its
 * recipe), and back() from there then goes to its fallback. */
export function forward(href: string, opts: { replace?: boolean } = {}): Promise<void> {
	requestSlide('forward', href);
	return quietly(
		goto(href, {
			replaceState: opts.replace ?? false,
			state: opts.replace ? {} : { recipesBack: true }
		})
	);
}

/** Slide back: history.back() when this page was entered with forward(),
 * otherwise a fresh navigation to `fallback`. */
export function back(fallback: string): Promise<void> {
	if (page.state.recipesBack) {
		requestSlide('back', null);
		history.back();
		return Promise.resolve();
	}
	requestSlide('back', fallback);
	return quietly(goto(fallback));
}

/** Navigate with a slide in either direction and no history bookkeeping, for
 * the odd case the two above do not cover (after a delete: slide back to the
 * menu, replacing the gone recipe's entry). */
export function slideTo(
	href: string,
	dir: Direction,
	opts: { replace?: boolean } = {}
): Promise<void> {
	requestSlide(dir, href);
	return quietly(goto(href, { replaceState: opts.replace ?? false }));
}
