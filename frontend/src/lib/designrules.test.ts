// Standing design rules (HANDOFF §2 rule 3), checked mechanically over every
// stylesheet in the app: app.css and each component's <style> block. The Aug 12
// sweep that removed hover/press movement missed nine rules in less-visited
// corners (a11y-mobile-design#11); this keeps the next one from landing, and
// keeps text on an accent fill on the per-theme --accent-contrast token
// (a11y-mobile-design#7) instead of a white that fails on light accents.
import { describe, expect, it } from 'vitest';
import appCss from '../app.css?raw';

const components = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

// Plain stylesheets a section imports for itself (lib/recipes/recipes.css).
const libSheets = import.meta.glob('/src/lib/**/*.css', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const sheets: [string, string][] = [
	['/src/app.css', appCss],
	...Object.entries(libSheets),
	...Object.entries(components).flatMap(([file, source]) =>
		[...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map(
			(m): [string, string] => [file, m[1]]
		)
	)
];

/** Drop comments and whole @keyframes blocks (an animation is a state, not hover feedback). */
function clean(css: string): string {
	let out = css.replace(/\/\*[\s\S]*?\*\//g, '');
	let at: number;
	while ((at = out.search(/@(-webkit-)?keyframes\b/)) >= 0) {
		let depth = 0;
		let i = out.indexOf('{', at);
		for (; i < out.length; i++) {
			if (out[i] === '{') depth++;
			else if (out[i] === '}' && --depth === 0) break;
		}
		out = out.slice(0, at) + out.slice(i + 1);
	}
	return out;
}

/** Innermost style rules; enclosing at-rules like @media fall away. */
function rules(css: string): { selector: string; body: string }[] {
	return [...clean(css).matchAll(/([^{}]*)\{([^{}]*)\}/g)].map((m) => ({
		selector: m[1].trim().replace(/\s+/g, ' '),
		body: m[2]
	}));
}

/** Hover/press rules that move something: a transform (or its longhands) other than none. */
function movingFeedback(css: string): string[] {
	const found: string[] = [];
	for (const { selector, body } of rules(css)) {
		if (!/:(hover|active)\b/.test(selector)) continue;
		for (const m of body.matchAll(/(?:^|[;\s])(transform|translate|scale|rotate)\s*:\s*([^;]+)/g)) {
			const value = m[2].trim();
			if (value.replace(/\s*!important$/, '') === 'none') continue;
			found.push(`${selector} { ${m[1]}: ${value} }`);
		}
	}
	return found;
}

/** Rules that fill with the accent and set a text color other than --accent-contrast. */
function hardTextOnAccent(css: string): string[] {
	const found: string[] = [];
	for (const { selector, body } of rules(css)) {
		if (!/(?:^|[;\s])background(-color)?\s*:\s*var\(--accent\)\s*(;|$)/.test(body)) continue;
		const color = /(?:^|[;\s])color\s*:\s*([^;]+)/.exec(body)?.[1].trim();
		if (color && !color.startsWith('var(--accent-contrast'))
			found.push(`${selector} { color: ${color} }`);
	}
	return found;
}

/** The style blocks of the recipes section: its screens, components and sheet. */
const recipeSheets = sheets.filter(([file]) => /^\/src\/(lib|routes)\/recipes\//.test(file));

/** The bodies of the phone media blocks (max-width up to 760px). */
function phoneBlocks(css: string): string[] {
	const out: string[] = [];
	const text = css.replace(/\/\*[\s\S]*?\*\//g, '');
	const head = /@media[^{]*max-width:\s*(\d+)px[^{]*\{/g;
	let m: RegExpExecArray | null;
	while ((m = head.exec(text))) {
		let depth = 1;
		let i = head.lastIndex;
		for (; i < text.length && depth; i++) {
			if (text[i] === '{') depth++;
			else if (text[i] === '}') depth--;
		}
		if (Number(m[1]) <= 760) out.push(text.slice(head.lastIndex, i - 1));
		head.lastIndex = i;
	}
	return out;
}

/** Phone font sizes given as a fixed length instead of an --m-* token (or a
 * recipes token, which recipes.css maps onto them at phone widths). A display
 * size that scales with the viewport (vw) is not a text size. */
function fixedPhoneFontSizes(css: string): string[] {
	const found: string[] = [];
	for (const block of phoneBlocks(css))
		for (const { selector, body } of rules(block))
			for (const m of body.matchAll(/(?:^|[;\s])font-size\s*:\s*([^;]+)/g)) {
				const value = m[1].trim();
				if (/var\(--(m|rc)-/.test(value) || /vw/.test(value) || !/\d/.test(value)) continue;
				found.push(`${selector} { font-size: ${value} }`);
			}
	return found;
}

/** A sheet that animates or transitions something and never stops it for
 * prefers-reduced-motion. */
function motionWithoutReduce(css: string): boolean {
	const moves = /(?:^|[;\s{])(animation|transition)\s*:(?!\s*none\b)/.test(clean(css));
	return moves && !/prefers-reduced-motion:\s*reduce/.test(css);
}

describe('design rules', () => {
	it('reads every stylesheet', () => {
		expect(sheets.length).toBeGreaterThan(40);
		expect(sheets.some(([file]) => file.endsWith('/Sidebar.svelte'))).toBe(true);
		expect(sheets.some(([file]) => file.endsWith('/recipes/recipes.css'))).toBe(true);
	});

	it('flags hover/press movement and nothing else', () => {
		const flagged = (css: string) => movingFeedback(css).length;
		expect(flagged('.a:hover { color: red; transform: translateY(-2px); }')).toBe(1);
		expect(flagged('@media (x) { .a:not(:disabled):active { scale: 0.98 } }')).toBe(1);
		expect(flagged('.a:hover, .b:focus-visible { transform: none !important; }')).toBe(0);
		expect(flagged('.a:hover { text-transform: uppercase; }')).toBe(0);
		expect(flagged('.a.open { transform: rotate(90deg); }')).toBe(0);
		expect(flagged('@keyframes k { from { transform: scale(0.9) } to { transform: none } }')).toBe(0);
		expect(flagged('/* .a:hover { transform: scale(2) } */')).toBe(0);
	});

	it('gives no hover or press feedback by movement', () => {
		const offenders = sheets.flatMap(([file, css]) =>
			movingFeedback(css).map((rule) => `${file}: ${rule}`)
		);
		expect(offenders).toEqual([]);
	});

	it('sizes recipes text on phones with the mobile tokens', () => {
		const flagged = (css: string) => fixedPhoneFontSizes(css).length;
		expect(flagged('@media (max-width: 760px) { .a { font-size: 0.66rem } }')).toBe(1);
		expect(flagged('@media (max-width: 760px) { .a { font-size: var(--m-label) } }')).toBe(0);
		expect(flagged('@media (max-width: 760px) { .a { font-size: calc(var(--m-label) * 0.9) } }')).toBe(0);
		expect(flagged('@media (max-width: 760px) { .a { font-size: clamp(2rem, 6.5vw, 3rem) } }')).toBe(0);
		expect(flagged('@media (min-width: 761px) { .a { font-size: 11px } }')).toBe(0);
		expect(recipeSheets.length).toBeGreaterThan(10);
		const offenders = recipeSheets.flatMap(([file, css]) =>
			fixedPhoneFontSizes(css).map((rule) => `${file}: ${rule}`)
		);
		expect(offenders).toEqual([]);
	});

	it('stops recipes motion under reduced motion', () => {
		expect(motionWithoutReduce('.a { transition: color 0.2s }')).toBe(true);
		expect(
			motionWithoutReduce(
				'.a { transition: color 0.2s } @media (prefers-reduced-motion: reduce) { .a { transition: none } }'
			)
		).toBe(false);
		expect(motionWithoutReduce('.a { transition: none }')).toBe(false);
		const offenders = recipeSheets.filter(([, css]) => motionWithoutReduce(css)).map(([f]) => f);
		expect(offenders).toEqual([]);
	});

	it('colors text on an accent fill with --accent-contrast', () => {
		const flagged = (css: string) => hardTextOnAccent(css).length;
		expect(flagged('.a { background: var(--accent); color: #fff; }')).toBe(1);
		expect(flagged('.a { background: var(--accent); color: var(--accent-contrast); }')).toBe(0);
		expect(flagged('.a { background: var(--accent-soft); color: var(--accent); }')).toBe(0);
		const offenders = sheets.flatMap(([file, css]) =>
			hardTextOnAccent(css).map((rule) => `${file}: ${rule}`)
		);
		expect(offenders).toEqual([]);
	});
});
