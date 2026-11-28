// Pure queue arithmetic for the player, kept apart so tests can pin it.

export type Repeat = 'off' | 'all' | 'one';

/** Where playback goes when a song ends on its own. Null means stop. Repeat
 * one is handled by the player (it replays the same song). */
export function nextAfterEnd(index: number, length: number, repeat: Repeat): number | null {
	if (length === 0) return null;
	if (index + 1 < length) return index + 1;
	return repeat === 'all' ? 0 : null;
}

/** The Next button: always moves, wrapping to the start at the end. */
export function nextByHand(index: number, length: number): number | null {
	if (length === 0) return null;
	return index + 1 < length ? index + 1 : 0;
}

/** The Previous button: back to the start of the song after the first three
 * seconds, otherwise the song before (wrapping only under repeat all). */
export function previous(
	index: number,
	length: number,
	position: number,
	repeat: Repeat
): { index: number; restart: boolean } {
	if (position > 3 || length === 0) return { index, restart: true };
	if (index > 0) return { index: index - 1, restart: false };
	return repeat === 'all' ? { index: length - 1, restart: false } : { index, restart: true };
}

/** Shuffle everything after the current song; the current one stays put so
 * turning shuffle on never interrupts what is playing. */
export function shuffleRest<T>(items: T[], index: number, random: () => number = Math.random): T[] {
	const head = items.slice(0, index + 1);
	const rest = items.slice(index + 1);
	for (let i = rest.length - 1; i > 0; i--) {
		const j = Math.floor(random() * (i + 1));
		[rest[i], rest[j]] = [rest[j], rest[i]];
	}
	return [...head, ...rest];
}
