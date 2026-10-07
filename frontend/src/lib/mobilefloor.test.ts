// The phone floors in app.css (a11y-mobile-design#6/#10). An element-selector
// floor lost to every scoped class rule, so a dozen fields crept back under
// 16px and iOS zoomed the page on focus; these pin the undefeatable version
// and the one thing it can now get wrong — shrinking a large title input
// that was never added to its opt-out list.
import { describe, expect, it } from 'vitest';
import appCss from '../app.css?raw';

const components = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/** Body of the first `@media (max-width: 760px) {` block, braces balanced. */
function mobileBlock(css: string): string {
	const start = css.indexOf('@media (max-width: 760px) {');
	expect(start).toBeGreaterThanOrEqual(0);
	let depth = 0;
	for (let i = css.indexOf('{', start); i < css.length; i++) {
		if (css[i] === '{') depth++;
		else if (css[i] === '}' && --depth === 0) return css.slice(css.indexOf('{', start) + 1, i);
	}
	throw new Error('unbalanced @media block');
}

const mobile = mobileBlock(appCss);
const fieldRule = /([^{}]*)\{([^{}]*font-size:\s*16px\s*!important[^{}]*)\}/.exec(mobile);
const optOuts = [...(/\):not\(([^)]*)\)\s*$/.exec(fieldRule?.[1] ?? '')?.[1] ?? '').matchAll(/\.([\w-]+)/g)].map(
	(m) => m[1]
);

/** Opening tags of text fields, walked with brace depth so `=>` in a handler doesn't end the tag. */
function textFields(markup: string): string[] {
	const tags: string[] = [];
	for (const m of markup.matchAll(/<(input|textarea|select)\b/g)) {
		let depth = 0;
		let i = m.index + m[0].length;
		for (; i < markup.length; i++) {
			const c = markup[i];
			if (c === '{') depth++;
			else if (c === '}') depth--;
			else if (c === '>' && depth === 0) break;
		}
		const tag = markup.slice(m.index, i);
		if (!/\btype="(checkbox|radio|range|file|color|submit|button|hidden)"/.test(tag)) tags.push(tag);
	}
	return tags;
}

/** Smallest px a font-size value can resolve to, or null when it isn't fixed. */
function minPx(value: string): number | null {
	if (value.includes('--m-title')) return 22;
	const px = [...value.matchAll(/(\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
	return px.length ? Math.min(...px) : null;
}

describe('mobile floors (app.css)', () => {
	it('floors every text field, bare inputs included, at 16px with !important', () => {
		expect(fieldRule).not.toBeNull();
		const selector = fieldRule![1];
		// A negative filter on `input` — not a list of types — so a bare
		// `<input>` or an email/url field is covered too.
		expect(selector).toMatch(/input:not\(/);
		expect(selector).toMatch(/\btextarea\b/);
		expect(selector).toMatch(/\bselect\b/);
		expect(fieldRule![2]).toMatch(/min-height:\s*44px;/);
		// min-height must stay overridable for inline chips.
		expect(fieldRule![2]).not.toMatch(/min-height:[^;]*!important/);
	});

	it('floors buttons at the documented 44px', () => {
		expect(mobile).toMatch(/(^|[;}\s])button\s*\{\s*min-height:\s*44px;/);
	});

	it('opts out every text field whose own size is above 16px', () => {
		expect(optOuts.length).toBeGreaterThan(0);
		const large: string[] = [];
		for (const [file, source] of Object.entries(components)) {
			const styleAt = source.indexOf('<style');
			if (styleAt < 0) continue;
			const css = source.slice(styleAt);
			for (const tag of textFields(source.slice(0, styleAt))) {
				const classes = /\bclass="([^"{]*)"/.exec(tag)?.[1].split(/\s+/).filter(Boolean) ?? [];
				for (const cls of classes) {
					const rule = new RegExp(`\\.${cls}(?![\\w-])[^{}]*\\{([^{}]*)\\}`, 'g');
					for (const m of css.matchAll(rule)) {
						const size = /(?:^|[;\s])font-size:\s*([^;]+);/.exec(m[1])?.[1];
						const px = size ? minPx(size) : null;
						if (px !== null && px > 16 && !optOuts.includes(cls)) large.push(`${file} .${cls} ${size}`);
					}
				}
			}
		}
		expect(large).toEqual([]);
	});

	it('names only opt-out classes that a text field still uses', () => {
		const markup = Object.values(components)
			.map((s) => s.slice(0, s.includes('<style') ? s.indexOf('<style') : undefined))
			.flatMap(textFields)
			.join('\n');
		for (const cls of optOuts) expect(markup).toMatch(new RegExp(`class="[^"]*\\b${cls}\\b`));
	});
});
