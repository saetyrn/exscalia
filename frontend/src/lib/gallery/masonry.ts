// The Gallery grid: Pinterest's masonry, laid out from the size each picture
// was stored with, so every tile has its height before its image loads and
// nothing moves when it does.
//
// Each tile goes into the column that is shortest so far. Placement depends
// only on the tiles before it, so appending a page leaves every tile already
// on screen where it was.

/** The narrowest a column may get before the grid drops one. */
export const MIN_COLUMN = 220;
export const GAP = 16;
/** A tall strip shows its top this many widths high; the page shows it all. */
export const MAX_RATIO = 2.6;
export const MIN_RATIO = 0.35;
/** A picture with no stored size is drawn square until it loads. */
export const DEFAULT_RATIO = 1;
/** The caption under a titled tile, in px. */
export const CAPTION = 30;

export interface Sized {
	width: number | null;
	height: number | null;
	title?: string;
}

/** Columns for a container `width` px wide: at least two (a phone shows
 * two, as Pinterest does), at most eight. */
export function columnCount(width: number, min = MIN_COLUMN, gap = GAP): number {
	if (!Number.isFinite(width) || width <= 0) return 2;
	return Math.max(2, Math.min(8, Math.floor((width + gap) / (min + gap))));
}

/** Height over width, clamped for the grid. */
export function tileRatio(item: Sized): number {
	const { width, height } = item;
	if (!width || !height || width <= 0 || height <= 0) return DEFAULT_RATIO;
	return Math.min(MAX_RATIO, Math.max(MIN_RATIO, height / width));
}

/** Item indexes per column, each column in reading order. */
export function placeColumns(
	items: readonly Sized[],
	columns: number,
	columnWidth = MIN_COLUMN
): number[][] {
	const count = Math.max(1, Math.floor(columns));
	const out: number[][] = Array.from({ length: count }, () => []);
	const heights = new Array<number>(count).fill(0);
	items.forEach((item, i) => {
		let shortest = 0;
		for (let c = 1; c < count; c++) if (heights[c] < heights[shortest]) shortest = c;
		out[shortest].push(i);
		heights[shortest] += tileRatio(item) * columnWidth + (item.title ? CAPTION : 0) + GAP;
	});
	return out;
}
