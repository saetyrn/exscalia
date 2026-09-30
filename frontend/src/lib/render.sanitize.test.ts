// @vitest-environment jsdom
// The real sanitizer. Notes, flashcards, media reviews and OMNIA's replies are
// injected with {@html}, and DOMPurify in render.ts is the only thing between
// their text and the page. render.test.ts stubs it out, because DOMPurify needs
// a DOM and that file runs under node, so a config change, or a marked or
// DOMPurify upgrade, that lets a handler, a script or a javascript: URL
// through would pass there. This file runs the same renders through the real
// DOMPurify on jsdom.
import DOMPurify from 'dompurify';
import { describe, expect, it } from 'vitest';
import { renderChatMarkdown } from './markdown';
import { renderMarkdown, renderNote } from './render';

/** The rendered HTML parsed the way {@html} inserts it. */
function parse(html: string): DocumentFragment {
	const template = document.createElement('template');
	template.innerHTML = html;
	return template.content;
}

const URL_ATTRIBUTES = ['href', 'xlink:href', 'src', 'srcset', 'action', 'formaction', 'poster'];

/** Nothing in `html` can run script: no script element, no event handler
 * attribute, no URL that executes when followed or loaded. */
function expectInert(html: string): void {
	const root = parse(html);
	expect(root.querySelector('script')).toBeNull();
	for (const element of root.querySelectorAll('*')) {
		for (const { name, value } of element.attributes) {
			const where = `${element.tagName} ${name}="${value}"`;
			expect(name.toLowerCase().startsWith('on'), where).toBe(false);
			if (!URL_ATTRIBUTES.includes(name.toLowerCase())) continue;
			// Browsers ignore whitespace and control characters inside a scheme.
			const url = value.replace(/[\s\u0000-\u001f]/g, '');
			expect(url, where).not.toMatch(/^(javascript|vbscript|data:text\/html)/i);
		}
	}
}

/** A resolver that knows `names`, recording every lookup. */
function vault(names: string[]) {
	const lookups: string[] = [];
	const resolve = (name: string) => {
		lookups.push(name);
		return names.includes(name) ? `notes/${name}.md` : null;
	};
	return { lookups, resolve };
}

const note = (source: string) => renderNote(source);
const chat = (source: string, names: string[] = []) =>
	renderChatMarkdown(source, vault(names).resolve);

const VECTORS = [
	'<img src=x onerror=alert(1)>',
	'<img src="x" onerror="alert(1)" srcset="javascript:alert(1)">',
	'<script>alert(1)</script>',
	'Text <script>alert(1)</script> inline',
	'[x](javascript:alert(1))',
	'[x](JaVaScRiPt:alert(1))',
	'[x](java&#115;cript:alert(1))',
	'![x](javascript:alert(1))',
	'<a href="javascript:alert(1)">x</a>',
	'<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a>',
	'[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
	'<svg><script>alert(1)</script></svg>',
	'<svg onload=alert(1)><circle r="1"/></svg>',
	'<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
	'<math><mi xlink:href="javascript:alert(1)">x</mi></math>',
	'<iframe src="javascript:alert(1)"></iframe>',
	'<form action="javascript:alert(1)"><button formaction="javascript:alert(1)">x</button></form>',
	'<details open ontoggle=alert(1)>x</details>',
	'# <img src=x onerror=alert(1)>',
	'$\\href{javascript:alert(1)}{x}$',
	'[[Note|<img src=x onerror=alert(1)>]]',
	'[[x"><img src=x onerror=alert(1)>]]'
];

describe('the sanitizer', () => {
	it('is the real DOMPurify, with the external link hook registered', () => {
		expect(DOMPurify.isSupported).toBe(true);
		const link = parse(note('[site](https://example.com/page)')).querySelector('a');
		expect(link?.getAttribute('href')).toBe('https://example.com/page');
		// Only the hook adds these; marked does not.
		expect(link?.getAttribute('target')).toBe('_blank');
		expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
	});

	it('leaves in-app links for the router', () => {
		const html = renderNote('[[Note]] and [top](#top)', { resolveNote: vault(['Note']).resolve });
		for (const link of parse(html).querySelectorAll('a')) {
			expect(link.hasAttribute('target')).toBe(false);
		}
	});

	// With every wikilink resolving too, so the splice before sanitizing is covered.
	const known = ['Note', 'x"><img src=x onerror=alert(1)>'];

	it.each(VECTORS)('keeps a note inert: %s', (source) => {
		expectInert(note(source));
		expectInert(renderNote(source, { resolveNote: vault(known).resolve }));
	});

	it.each(VECTORS)('keeps a chat reply inert: %s', (source) => {
		expectInert(chat(source));
		expectInert(chat(source, known));
	});
});

// Read mode keeps the raw HTML a note contains, so here DOMPurify is what
// removes the dangerous part. These check it removes only that part.
describe('raw HTML in a note', () => {
	it('keeps an image and drops its handler', () => {
		const image = parse(note('<img src="x.png" onerror="alert(1)">')).querySelector('img');
		expect(image?.getAttribute('src')).toBe('x.png');
		expect(image?.hasAttribute('onerror')).toBe(false);
	});

	it('drops a script and keeps the text around it', () => {
		const root = parse(note('Before <script>alert(1)</script> after'));
		expect(root.querySelector('script')).toBeNull();
		expect(root.textContent).toContain('Before');
		expect(root.textContent).toContain('after');
		expect(root.textContent).not.toContain('alert');
	});

	it('drops a script inside SVG', () => {
		const root = parse(note('<svg><script>alert(1)</script><circle r="1"/></svg>'));
		expect(root.querySelector('script')).toBeNull();
		expect(root.querySelector('svg circle')).not.toBeNull();
	});

	it('keeps a javascript: or data:text/html link as text without its href', () => {
		for (const source of [
			'[x](javascript:alert(1))',
			'<a href="javascript:alert(1)">x</a>',
			'<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a>'
		]) {
			const link = parse(note(source)).querySelector('a');
			expect(link?.textContent).toBe('x');
			expect(link?.hasAttribute('href')).toBe(false);
		}
	});

	it('keeps a remote image outside chat', () => {
		const image = parse(note('![a](https://example.com/y.png)')).querySelector('img');
		expect(image?.getAttribute('src')).toBe('https://example.com/y.png');
	});
});

describe('math', () => {
	it('keeps KaTeX output, including the inline styles that lay it out', () => {
		const root = parse(note('Inline $x^2$ and\n\n$$\\frac{a}{b}$$'));
		expect(root.querySelector('.katex')).not.toBeNull();
		expect(root.querySelector('.katex-display')).not.toBeNull();
		expect(root.querySelector('.katex [style]')).not.toBeNull();
	});

	it('does not turn \\href into a link', () => {
		const root = parse(note('$\\href{javascript:alert(1)}{x}$'));
		expect(root.querySelector('a')).toBeNull();
	});
});

// A reply's remote image would load the moment it renders, carrying whatever
// the model put in its URL; the chat renders it as a link instead
// (omnia-trust-boundary#1). The sanitizer must not undo either side of that.
describe('chat images', () => {
	it('turns a remote image into an external link', () => {
		const root = parse(chat('![a](https://evil.test/?d=1)'));
		expect(root.querySelector('img')).toBeNull();
		const link = root.querySelector('a');
		expect(link?.getAttribute('href')).toBe('https://evil.test/?d=1');
		expect(link?.getAttribute('target')).toBe('_blank');
		expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
		expect(link?.textContent).toBe('a');
	});

	it('shows a raw <img> as text', () => {
		const root = parse(chat('<img src="https://evil.test/x">'));
		expect(root.querySelector('img')).toBeNull();
		expect(root.textContent).toContain('<img src="https://evil.test/x">');
	});

	it('keeps the vault’s own images as images', () => {
		for (const src of ['/api/attachments/x.png', '/api/paper/n/p/render.png']) {
			const image = parse(chat(`![a](${src})`)).querySelector('img');
			expect(image?.getAttribute('src')).toBe(src);
			expect(image?.getAttribute('alt')).toBe('a');
		}
	});
});

// Wikilinks are spliced in as HTML before the sanitizer runs, so the names
// that broke the old regex (gap-unicode-path-identity#8) must also come out
// of DOMPurify as the right link.
describe('wikilinks through the sanitizer', () => {
	it.each([
		["Mom's recipes", "/note/notes/Mom's%20recipes.md"],
		['Q&A', '/note/notes/Q%26A.md'],
		['The "quoted" note', '/note/notes/The%20%22quoted%22%20note.md'],
		['Café & “Ärger”', '/note/notes/Caf%C3%A9%20%26%20%E2%80%9C%C3%84rger%E2%80%9D.md']
	])('resolves %s', (name, href) => {
		const { lookups, resolve } = vault([name]);
		const link = parse(renderChatMarkdown(`See [[${name}]].`, resolve)).querySelector('a');
		expect(lookups).toEqual([name]);
		expect(link?.getAttribute('href')).toBe(href);
		expect(link?.textContent).toBe(name);
	});

	it('shows a label as text, markup and all', () => {
		const root = parse(chat('[[Note|<img src=x onerror=alert(1)>]]', ['Note']));
		expect(root.querySelector('img')).toBeNull();
		expect(root.querySelector('a')?.textContent).toBe('<img src=x onerror=alert(1)>');
	});

	it('keeps a hostile target inside the link and the dead-link title', () => {
		const target = 'x"><img src=x onerror=alert(1)>';
		for (const html of [chat(`[[${target}]]`, [target]), chat(`[[${target}]]`)]) {
			const root = parse(html);
			expect(root.querySelector('img')).toBeNull();
			expect(root.textContent?.trim()).toBe(target);
		}
		const dead = parse(renderMarkdown(`[[${target}]]`, { resolveNote: () => null })).querySelector(
			'.dead-link'
		);
		expect(dead?.getAttribute('title')).toBe(`No note named ${target}`);
	});
});
