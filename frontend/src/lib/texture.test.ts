// The Texture appearance setting: scanlines and grain over the whole window
// (app.css, .app-texture), off unless asked for and on dark surfaces only.
// theme.ts applies it, the pre-render bootstrap in app.html applies it before
// the first paint, and app.css decides where it shows; these pin the three.
import { afterEach, describe, expect, it, vi } from 'vitest';
import appCss from '../app.css?raw';
import appHtml from '../app.html?raw';
import layout from '../routes/+layout.svelte?raw';
import {
	applyAppearanceSettings,
	loadLocalAppearance,
	type AppearanceSettingsInput
} from './theme';

function fakeBrowser(stored: Record<string, string> = {}) {
	const dataset: Record<string, string> = {};
	const root = {
		dataset,
		style: { setProperty() {}, removeProperty() {}, colorScheme: '' }
	};
	const localStorage = {
		getItem: (key: string) => stored[key] ?? null,
		setItem: (key: string, value: string) => void (stored[key] = value),
		removeItem: (key: string) => void delete stored[key]
	};
	vi.stubGlobal('document', { documentElement: root });
	vi.stubGlobal('localStorage', localStorage);
	return { dataset, stored, root, localStorage };
}

const settings = (texture: boolean): AppearanceSettingsInput => ({
	theme: 'black',
	font: 'system',
	intoxicate: false,
	texture,
	custom_theme: null,
	saved_themes: [],
	active_saved_theme_id: null
});

afterEach(() => vi.unstubAllGlobals());

describe('the texture setting', () => {
	it('marks the root when on, clears it when off, and remembers either', () => {
		const { dataset, stored } = fakeBrowser();
		applyAppearanceSettings(settings(true));
		expect(dataset.texture).toBe('1');
		expect(stored['omnia.texture']).toBe('1');
		expect(loadLocalAppearance().texture).toBe(true);

		applyAppearanceSettings(settings(false));
		expect('texture' in dataset).toBe(false);
		expect(stored['omnia.texture']).toBe('0');
		expect(loadLocalAppearance().texture).toBe(false);
	});

	it('is off for a device that never set it, and for a server that predates it', () => {
		const { dataset } = fakeBrowser();
		expect(loadLocalAppearance().texture).toBe(false);
		const old = settings(false) as Partial<AppearanceSettingsInput>;
		delete old.texture;
		applyAppearanceSettings(old as AppearanceSettingsInput);
		expect('texture' in dataset).toBe(false);
	});

	it('is applied by the pre-render bootstrap, so a reload does not flash without it', () => {
		const script = /<script>([\s\S]*?)<\/script>/.exec(appHtml)?.[1];
		expect(script).toBeTruthy();
		for (const [value, on] of [['1', true], ['0', false], [undefined, false]] as const) {
			const stored: Record<string, string> = value === undefined ? {} : { 'omnia.texture': value };
			const { root, localStorage } = fakeBrowser(stored);
			new Function('document', 'localStorage', script!)({ documentElement: root }, localStorage);
			expect(root.dataset.texture === '1', String(value)).toBe(on);
		}
	});
});

describe('the texture overlay in app.css', () => {
	const css = appCss.replace(/\/\*[\s\S]*?\*\//g, '');

	it('is hidden unless the root asks for it', () => {
		expect(css).toMatch(/\.app-texture\s*\{\s*display:\s*none;\s*\}/);
		// Every rule that shows it or styles its layers hangs on the attribute.
		const shows = [...css.matchAll(/([^{}]*)\{[^{}]*display:\s*block[^{}]*\}/g)]
			.map((m) => m[1].trim())
			.filter((selector) => selector.includes('.app-texture'));
		expect(shows).toEqual([":root[data-texture='1'] .app-texture"]);
	});

	it('draws only on dark surfaces: its colours come from light-dark()', () => {
		expect(css).toMatch(/--tx-scan:\s*light-dark\(transparent,/);
		expect(css).toMatch(/--tx-grain:\s*light-dark\(transparent,/);
		expect(css).toContain('@supports (color: light-dark(black, white))');
		// The media look is black under any theme and declares no scheme.
		expect(css).toMatch(/:root\[data-texture='1'\]\[data-media-look\] \.app-texture\s*\{[^{}]*--tx-scan:\s*rgba/);
	});

	it('never takes a click, stops moving under reduced motion, and stays off paper', () => {
		expect(css).toMatch(/:root\[data-texture='1'\] \.app-texture\s*\{[^{}]*pointer-events:\s*none/);
		// app.css keeps one reduced-motion block, and it stops every animation.
		expect(css).toMatch(
			/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\*::after\s*\{[^{}]*animation-iteration-count:\s*1 !important/
		);
		expect(css).toMatch(/@media print\s*\{\s*\.app-texture\s*\{\s*display:\s*none !important/);
	});

	it('replaces the Recipes pane overlay rather than stacking on it', () => {
		expect(css).toMatch(/:root\[data-texture='1'\] \.rc-tv\s*\{\s*display:\s*none/);
	});

	it('sits above toasts', () => {
		const z = (name: string) => Number(new RegExp(`--z-${name}:\\s*(\\d+)`).exec(css)?.[1]);
		expect(z('texture')).toBeGreaterThan(z('toast'));
	});
});

describe('the root layout', () => {
	it('renders the overlay everywhere but Paper', () => {
		expect(layout).toMatch(
			/\{#if !page\.url\.pathname\.startsWith\('\/paper'\)\}\s*<div class="app-texture" aria-hidden="true">/
		);
	});
});
