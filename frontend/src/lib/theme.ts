// Custom theme support. A custom theme is the user's own palette, stored on
// this device (like the preset choice) and applied as inline CSS variables on
// <html>, which override the preset :root blocks in app.css. The pre-render
// bootstrap in app.html mirrors applyCustomTheme so reloads don't flash.

export const THEME_COLOR_VARS = [
	'bg',
	'bg-panel',
	'bg-hover',
	'bg-active',
	'border',
	'text',
	'text-muted',
	'accent',
	'accent-soft',
	'danger',
	'added',
	'removed'
] as const;

export type ThemeVar = (typeof THEME_COLOR_VARS)[number];

export interface CustomTheme {
	mode: 'light' | 'dark';
	colors: Record<ThemeVar, string>;
}

export const CUSTOM_THEME_KEY = 'omnia.customTheme';
export const SAVED_THEMES_KEY = 'omnia.savedThemes';

export interface SavedTheme {
	id: string;
	name: string;
	theme: CustomTheme;
}

export const THEME_IDS = ['system', 'light', 'dark', 'black', 'beige', 'forest', 'custom'] as const;
export type ThemeId = (typeof THEME_IDS)[number];
export const FONT_IDS = ['system', 'serif', 'rounded', 'mono'] as const;
export type FontId = (typeof FONT_IDS)[number];

export const ACTIVE_SAVED_THEME_KEY = 'omnia.activeSavedTheme';

export interface AppearanceSettings {
	configured: boolean;
	theme: ThemeId;
	font: FontId;
	intoxicate: boolean;
	custom_theme: CustomTheme | null;
	saved_themes: SavedTheme[];
	active_saved_theme_id: string | null;
}

export type AppearanceSettingsInput = Omit<AppearanceSettings, 'configured'>;

const HEX = /^#[0-9a-f]{6}$/i;

export function isHex(value: string): boolean {
	return HEX.test(value);
}

/** Blend two #rrggbb colors; t=0 → a, t=1 → b. */
export function mixHex(a: string, b: string, t: number): string {
	const ca = a.match(/[0-9a-f]{2}/gi)!.map((h) => parseInt(h, 16));
	const cb = b.match(/[0-9a-f]{2}/gi)!.map((h) => parseInt(h, 16));
	const mixed = ca.map((v, i) => Math.round(v + (cb[i] - v) * t));
	return '#' + mixed.map((v) => v.toString(16).padStart(2, '0')).join('');
}

/**
 * Build a full palette from three base colors. Surfaces step from the
 * background toward the text color, selection/washes toward the accent —
 * the same relationships the built-in themes use, so any reasonable
 * bg/text/accent trio comes out coherent.
 */
export function deriveTheme(
	bg: string,
	text: string,
	accent: string,
	mode: 'light' | 'dark'
): CustomTheme {
	const status =
		mode === 'dark'
			? { danger: '#e5534b', added: '#57ab5a', removed: '#e5534b' }
			: { danger: '#c0392b', added: '#1a7f37', removed: '#cf222e' };
	return {
		mode,
		colors: {
			bg,
			'bg-panel': mixHex(bg, text, 0.04),
			'bg-hover': mixHex(bg, text, 0.09),
			'bg-active': mixHex(bg, accent, 0.16),
			border: mixHex(bg, text, 0.14),
			text,
			'text-muted': mixHex(text, bg, 0.42),
			accent,
			'accent-soft': mixHex(bg, accent, 0.14),
			...status
		}
	};
}

export function loadCustomTheme(): CustomTheme | null {
	try {
		const raw = localStorage.getItem(CUSTOM_THEME_KEY);
		if (!raw) return null;
		const parsed = JSON.parse(raw);
		return validCustomTheme(parsed);
	} catch {
		return null;
	}
}

function validCustomTheme(value: unknown): CustomTheme | null {
	if (!value || typeof value !== 'object') return null;
	const candidate = value as { mode?: unknown; colors?: Record<string, unknown> };
	if (candidate.mode !== 'light' && candidate.mode !== 'dark') return null;
	const colors = candidate.colors ?? {};
	if (!THEME_COLOR_VARS.every((v) => typeof colors[v] === 'string' && isHex(colors[v] as string)))
		return null;
	return { mode: candidate.mode, colors: colors as CustomTheme['colors'] };
}

export function loadSavedThemes(): SavedTheme[] {
	try {
		const parsed = JSON.parse(localStorage.getItem(SAVED_THEMES_KEY) ?? '[]');
		if (!Array.isArray(parsed)) return [];
		const saved: SavedTheme[] = [];
		for (const entry of parsed.slice(0, 50)) {
			const theme = validCustomTheme(entry?.theme);
			const id = typeof entry?.id === 'string' ? entry.id : '';
			const name = typeof entry?.name === 'string' ? entry.name.trim().slice(0, 40) : '';
			if (id && name && theme) saved.push({ id, name, theme });
		}
		return saved;
	} catch {
		return [];
	}
}

export function saveThemeLibrary(themes: SavedTheme[]) {
	try {
		localStorage.setItem(SAVED_THEMES_KEY, JSON.stringify(themes.slice(0, 50)));
	} catch {
		// The in-memory library still works for this page.
	}
}

export function saveCustomTheme(theme: CustomTheme) {
	try {
		localStorage.setItem(CUSTOM_THEME_KEY, JSON.stringify(theme));
	} catch {
		// Still applied for this page when storage is unavailable.
	}
}

export function loadLocalAppearance(): AppearanceSettingsInput {
	let theme: ThemeId = 'system';
	let font: FontId = 'system';
	let intoxicate = false;
	let activeSavedThemeId: string | null = null;
	try {
		const savedTheme = localStorage.getItem('omnia.theme');
		if (THEME_IDS.includes(savedTheme as ThemeId)) theme = savedTheme as ThemeId;
		const savedFont = localStorage.getItem('omnia.font');
		if (FONT_IDS.includes(savedFont as FontId)) font = savedFont as FontId;
		intoxicate = localStorage.getItem('omnia.intoxicate') === '1';
		activeSavedThemeId = localStorage.getItem(ACTIVE_SAVED_THEME_KEY);
	} catch {
		// Use the safe defaults when storage is unavailable.
	}
	const savedThemes = loadSavedThemes();
	if (!savedThemes.some((saved) => saved.id === activeSavedThemeId)) {
		activeSavedThemeId = null;
	}
	return {
		theme,
		font,
		intoxicate,
		custom_theme: loadCustomTheme(),
		saved_themes: savedThemes,
		active_saved_theme_id: activeSavedThemeId
	};
}

export function storeLocalAppearance(settings: AppearanceSettingsInput) {
	try {
		localStorage.setItem('omnia.theme', settings.theme);
		if (settings.font === 'system') localStorage.removeItem('omnia.font');
		else localStorage.setItem('omnia.font', settings.font);
		localStorage.setItem('omnia.intoxicate', settings.intoxicate ? '1' : '0');
		if (settings.custom_theme) {
			localStorage.setItem(CUSTOM_THEME_KEY, JSON.stringify(settings.custom_theme));
		} else {
			localStorage.removeItem(CUSTOM_THEME_KEY);
		}
		localStorage.setItem(SAVED_THEMES_KEY, JSON.stringify(settings.saved_themes.slice(0, 50)));
		if (settings.active_saved_theme_id) {
			localStorage.setItem(ACTIVE_SAVED_THEME_KEY, settings.active_saved_theme_id);
		} else {
			localStorage.removeItem(ACTIVE_SAVED_THEME_KEY);
		}
	} catch {
		// The applied in-memory appearance still works for this page.
	}
}

export function applyAppearanceSettings(settings: AppearanceSettingsInput) {
	const root = document.documentElement;
	root.dataset.theme = settings.theme;
	if (settings.font === 'system') delete root.dataset.font;
	else root.dataset.font = settings.font;
	if (settings.intoxicate) root.dataset.intoxicate = '1';
	else delete root.dataset.intoxicate;
	if (settings.theme === 'custom' && settings.custom_theme) {
		applyCustomTheme(settings.custom_theme);
	} else {
		clearCustomTheme();
	}
	storeLocalAppearance(settings);
}

export function applyCustomTheme(theme: CustomTheme) {
	const root = document.documentElement;
	for (const v of THEME_COLOR_VARS) root.style.setProperty(`--${v}`, theme.colors[v]);
	root.style.colorScheme = theme.mode;
}

export function clearCustomTheme() {
	const root = document.documentElement;
	for (const v of THEME_COLOR_VARS) root.style.removeProperty(`--${v}`);
	root.style.colorScheme = '';
}

/** Snapshot whatever palette is currently rendered — the seed for a new
 * custom theme, so editing starts from the look the user already has. */
export function themeFromCurrent(): CustomTheme {
	const style = getComputedStyle(document.documentElement);
	const read = (v: ThemeVar) => style.getPropertyValue(`--${v}`).trim();
	const mode = style.colorScheme.includes('dark') ? 'dark' : 'light';
	const colors = {} as CustomTheme['colors'];
	for (const v of THEME_COLOR_VARS) colors[v] = isHex(read(v)) ? read(v).toLowerCase() : '#888888';
	return { mode, colors };
}
