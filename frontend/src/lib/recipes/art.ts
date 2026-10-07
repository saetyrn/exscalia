// Coloured ASCII art: the character grid, a same-shaped grid of palette keys
// ("." or a missing key is the default ink) and the palette. Runs of one
// colour become one span, so a drawing stays a few hundred nodes.

export interface ArtRun {
	text: string;
	/** A hex colour, or undefined for the default ink. */
	color?: string;
}

const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;

/** One array of runs per row. Palette entries that are not hex colours are
 * ignored: the palette comes from a model, by way of a note anyone can edit. */
export function artRows(
	art: string,
	colors = '',
	palette: Record<string, string> = {}
): ArtRun[][] {
	if (!art) return [];
	const keys = colors ? colors.split('\n') : [];
	return art.split('\n').map((row, r) => {
		const k = keys[r] ?? '';
		const runs: ArtRun[] = [];
		for (let i = 0; i < row.length; i++) {
			const value = palette[k[i] ?? '.'];
			const color = value && HEX.test(value) ? value : undefined;
			const last = runs[runs.length - 1];
			if (last && last.color === color) last.text += row[i];
			else runs.push(color ? { text: row[i], color } : { text: row[i] });
		}
		return runs;
	});
}
