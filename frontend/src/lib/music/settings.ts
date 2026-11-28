// Per-device Music settings, kept in this browser (MUSIC.md section 3).

const LOSSLESS_KEY = 'exscalia.music.lossless';

/** Stream originals (FLAC where browsers need it) instead of the MP3 copy. */
export function losslessOn(): boolean {
	try {
		return localStorage.getItem(LOSSLESS_KEY) === '1';
	} catch {
		return false;
	}
}

export function setLossless(on: boolean): void {
	try {
		if (on) localStorage.setItem(LOSSLESS_KEY, '1');
		else localStorage.removeItem(LOSSLESS_KEY);
	} catch {
		/* private mode: the switch just does not stick */
	}
}
