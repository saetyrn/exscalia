// A saved palette that lacks a colour (a variable added after it was saved)
// is backfilled, not dropped: the layout's seed PUT sends this device's
// library to the server, and dropping would send it empty.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { loadSavedThemes, SAVED_THEMES_KEY, THEME_COLOR_VARS, validCustomTheme } from './theme';

function palette(without: string[] = []) {
	const colors: Record<string, string> = {};
	for (const v of THEME_COLOR_VARS) if (!without.includes(v)) colors[v] = '#123456';
	return { mode: 'dark', colors };
}

describe('validCustomTheme', () => {
	it('backfills a missing or invalid colour', () => {
		const theme = validCustomTheme({
			mode: 'dark',
			colors: { ...palette(['removed']).colors, bg: 'red' }
		});
		expect(theme?.mode).toBe('dark');
		expect(theme?.colors.removed).toBe('#888888');
		expect(theme?.colors.bg).toBe('#888888');
		expect(theme?.colors.text).toBe('#123456');
	});

	it('rejects values with no usable mode or colour map', () => {
		expect(validCustomTheme(null)).toBeNull();
		expect(validCustomTheme('dark')).toBeNull();
		expect(validCustomTheme({ mode: 'dim', colors: palette().colors })).toBeNull();
		expect(validCustomTheme({ mode: 'light' })).toBeNull();
		expect(validCustomTheme({ mode: 'light', colors: ['#123456'] })).toBeNull();
	});
});

describe('loadSavedThemes', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('keeps a saved theme that predates a colour variable', () => {
		const saved = [
			{ id: 'old', name: 'Old', theme: palette(['removed']) },
			{ id: 'broken', name: 'Broken', theme: 'not a palette' }
		];
		vi.stubGlobal('localStorage', {
			getItem: (key: string) => (key === SAVED_THEMES_KEY ? JSON.stringify(saved) : null)
		});
		const themes = loadSavedThemes();
		expect(themes.map((t) => t.id)).toEqual(['old']);
		expect(themes[0].theme.colors.removed).toBe('#888888');
	});
});
