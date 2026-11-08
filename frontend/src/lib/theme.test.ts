// Text on an accent fill (a11y-mobile-design#7): --accent-contrast is white or
// near-black per theme, whichever reads on that accent. The preset blocks in
// app.css, custom themes (theme.ts) and the pre-render bootstrap in app.html
// each make that choice, so these pin all three to the same answer.
import { afterEach, describe, expect, it, vi } from 'vitest';
import appCss from '../app.css?raw';
import appHtml from '../app.html?raw';
import {
	accentContrast,
	applyCustomTheme,
	clearCustomTheme,
	contrastRatio,
	deriveTheme
} from './theme';

const NEAR_BLACK = '#10131a';
const WHITE = '#ffffff';

/** Innermost rules in app.css that declare a hex --accent, with their custom properties. */
function accentBlocks(): [string, Record<string, string>][] {
	const css = appCss.replace(/\/\*[\s\S]*?\*\//g, '');
	const blocks: [string, Record<string, string>][] = [];
	for (const m of css.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
		const vars: Record<string, string> = {};
		for (const d of m[2].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) vars[d[1]] = d[2].trim();
		if (!/^#[0-9a-f]{6}$/i.test(vars.accent ?? '')) continue;
		blocks.push([m[1].trim().replace(/\s+/g, ' '), vars]);
	}
	return blocks;
}

/** A stand-in for <html> that records the inline custom properties set on it. */
function fakeRoot() {
	const props = new Map<string, string>();
	const root = {
		dataset: {} as Record<string, string>,
		style: {
			colorScheme: '',
			setProperty: (name: string, value: string) => void props.set(name, value),
			removeProperty: (name: string) => void props.delete(name)
		}
	};
	return { root, props };
}

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('contrastRatio', () => {
	it('follows the WCAG formula', () => {
		expect(contrastRatio('#ffffff', '#000000')).toBeCloseTo(21, 5);
		expect(contrastRatio('#6c5ce7', '#6c5ce7')).toBe(1);
		expect(contrastRatio('#6fbf73', WHITE)).toBeCloseTo(contrastRatio(WHITE, '#6fbf73'), 10);
		expect(contrastRatio('#6fbf73', WHITE)).toBeCloseTo(2.24, 2);
	});
});

describe('accentContrast', () => {
	it('puts near-black on light accents and white on deep ones', () => {
		expect(accentContrast('#6fbf73')).toBe(NEAR_BLACK); // forest
		expect(accentContrast('#9d8cff')).toBe(NEAR_BLACK); // black
		expect(accentContrast('#8d7cf6')).toBe(NEAR_BLACK); // dark
		expect(accentContrast('#6c5ce7')).toBe(WHITE); // light
		expect(accentContrast('#a05e2c')).toBe(WHITE); // beige
	});

	it('clears AA on every built-in accent', () => {
		const pairs: [string, string][] = [
			['#6c5ce7', WHITE],
			['#8d7cf6', NEAR_BLACK],
			['#9d8cff', NEAR_BLACK],
			['#a05e2c', WHITE],
			['#6fbf73', NEAR_BLACK]
		];
		for (const [accent, text] of pairs) {
			expect(accentContrast(accent)).toBe(text);
			expect(contrastRatio(accent, text)).toBeGreaterThanOrEqual(4.5);
		}
	});
});

describe('app.css theme blocks', () => {
	const withAccent = accentBlocks();

	it('declares --accent-contrast beside every accent, as a custom theme would', () => {
		const selectors = withAccent.map(([selector]) => selector);
		for (const theme of ['light', 'dark', 'black', 'beige', 'forest']) {
			expect(selectors).toContain(`:root[data-theme='${theme}']`);
		}
		expect(selectors).toContain(':root');
		expect(selectors).toContain(":root:not([data-theme]), :root[data-theme='system']");
		for (const [selector, vars] of withAccent) {
			expect(vars['accent-contrast'], selector).toBe(accentContrast(vars.accent));
			const ratio = contrastRatio(vars.accent, vars['accent-contrast']);
			expect(ratio, selector).toBeGreaterThanOrEqual(4.5);
		}
	});

	it('keeps muted text at 4.5:1 on panels', () => {
		for (const [selector, vars] of withAccent) {
			const ratio = contrastRatio(vars['text-muted'], vars['bg-panel']);
			expect(ratio, selector).toBeGreaterThanOrEqual(4.5);
		}
	});
});

describe('custom themes', () => {
	it('derive --accent-contrast from the accent when applied, and clear it', () => {
		const { root, props } = fakeRoot();
		vi.stubGlobal('document', { documentElement: root });

		applyCustomTheme(deriveTheme('#0f1711', '#dfe8dc', '#6fbf73', 'dark'));
		expect(props.get('--accent')).toBe('#6fbf73');
		expect(props.get('--accent-contrast')).toBe(NEAR_BLACK);

		applyCustomTheme(deriveTheme('#faf9f7', '#24292f', '#6c5ce7', 'light'));
		expect(props.get('--accent-contrast')).toBe(WHITE);

		clearCustomTheme();
		expect(props.has('--accent')).toBe(false);
		expect(props.has('--accent-contrast')).toBe(false);
	});

	it('get the same --accent-contrast from the pre-render bootstrap in app.html', () => {
		const script = /<script>([\s\S]*?)<\/script>/.exec(appHtml)?.[1];
		expect(script).toBeTruthy();
		for (const accent of ['#6fbf73', '#9d8cff', '#6c5ce7', '#a05e2c', '#ffd400', '#1a1a2e']) {
			const { root, props } = fakeRoot();
			const stored: Record<string, string> = {
				'omnia.theme': 'custom',
				'omnia.customTheme': JSON.stringify(deriveTheme('#1e1e22', '#e6e6ea', accent, 'dark'))
			};
			const localStorage = { getItem: (key: string) => stored[key] ?? null };
			new Function('document', 'localStorage', script!)({ documentElement: root }, localStorage);
			expect(props.get('--accent'), accent).toBe(accent);
			expect(props.get('--accent-contrast'), accent).toBe(accentContrast(accent));
		}
	});
});
