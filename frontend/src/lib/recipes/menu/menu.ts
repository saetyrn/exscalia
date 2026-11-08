// The revolving menu's small rules, apart from the geometry (carousel.ts):
// how a drag or a wheel turn maps to a position, which recipe a position
// lands on, what the focused title's caption says, and which images the menu
// may show.
import { clamp, rubberBand } from '../carousel';
import { formatMinutes } from '../format';
import type { RecipeCard } from '../types';

/** The menu is compact (one centred column, the phone geometry, no drawing
 * beside it) at or under this width, in px. Measured on the menu itself, so a
 * narrow pane beside the sidebar counts too. */
export const COMPACT_WIDTH = 600;
/** Items rendered each side of focus; the rest are not in the DOM. */
export const RENDER_WINDOW = 4;
/** How far, in px, the stage reaches left over the drawing's column so items
 * curving toward it stay inside the edge mask. */
export const REACH = 240;
/** Pointer travel, in px, before a press becomes a drag. */
export const DRAG_SLOP = 6;

/** Pointer travel per item while dragging. */
export function pxPerItem(compact: boolean): number {
	return compact ? 90 : 120;
}

/** Where the menu sits while a drag that started on item `from` has moved
 * `dy` px up (negative: down). Past either end it gives with resistance. */
export function dragPosition(from: number, dy: number, compact: boolean, count: number): number {
	return rubberBand(from + dy / pxPerItem(compact), count);
}

/** The item a position settles on: the nearest one, inside the list. */
export function landing(pos: number, count: number): number {
	return clamp(Math.round(pos), 0, Math.max(0, count - 1));
}

/** The first and last index in the DOM around the focused one. */
export function renderRange(active: number, count: number): [number, number] {
	return [Math.max(0, active - RENDER_WINDOW), Math.min(count - 1, active + RENDER_WINDOW)];
}

/** A wheel event's vertical travel in px: line deltas (Firefox with a mouse)
 * count 16 px a line, page deltas a page. */
export function wheelPixels(deltaY: number, deltaMode: number, page = 800): number {
	if (deltaMode === 1) return deltaY * 16;
	if (deltaMode === 2) return deltaY * page;
	return deltaY;
}

/** The focused title's caption: total time, then up to three tags. */
export function cardMeta(card: Pick<RecipeCard, 'total_minutes' | 'tags'>): string {
	return [formatMinutes(card.total_minutes), ...card.tags.slice(0, 3)]
		.filter(Boolean)
		.join(' · ');
}

/** A thumbnail the menu may show: only the app's own attachments. The CSP
 * allows same-origin images alone, and a remote URL must never be requested. */
export function localImage(url: string | null | undefined): string | null {
	return url && url.startsWith('/api/attachments/') ? url : null;
}

/** A DOM id for a recipe's option, for aria-activedescendant. Vault paths
 * may hold spaces, which an id may not. */
export function optionId(path: string): string {
	return `rc-menu-${encodeURIComponent(path)}`;
}

/** The counter under the menu: "03 / 12". */
export function counter(index: number, count: number): string {
	const pad = (n: number) => String(n).padStart(2, '0');
	return `${pad(index + 1)} / ${pad(count)}`;
}
