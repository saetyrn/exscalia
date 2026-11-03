// What a recipe page hands its layout. The corner row is page content (each
// screen has its own buttons) but has to sit in a corner of the pane that does
// not scroll with the page, so CornerNav passes its row up as a snippet and
// routes/recipes/+layout.svelte renders it there. OMNIA is the layout's: one
// popover for every screen, opened by the corner's chat button or K, talking
// about whatever the page names.
import type { Snippet } from 'svelte';

export const recipesChrome: {
	/** The page's corner row, rendered by the layout. */
	corner: Snippet | null;
	/** OMNIA's subject: a recipe's vault path (that note's own conversation),
	 * true for the Recipes conversation, false for no OMNIA on this page. */
	omnia: string | boolean;
	/** The OMNIA popover is open. */
	omniaOpen: boolean;
} = $state({ corner: null, omnia: true, omniaOpen: false });
