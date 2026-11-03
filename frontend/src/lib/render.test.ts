// The chat re-renders its whole transcript on every streamed token, so
// renderMarkdown keeps finished HTML. These tests watch the one call every
// uncached render makes — the sanitizer — to tell a cache hit from a re-parse.
// DOMPurify needs a DOM and these tests run under 'node', so it is stubbed;
// what is under test here is the caching, not the sanitizing. The external
// link hook is a plain function, so it is exercised directly on a fake node.
import { afterEach, describe, expect, it, vi } from 'vitest';

const purify = vi.hoisted(() => ({ calls: 0, hooks: {} as Record<string, unknown> }));

vi.mock('dompurify', () => ({
	default: {
		isSupported: true,
		sanitize: (html: string) => {
			purify.calls += 1;
			return html;
		},
		addHook: (name: string, fn: unknown) => {
			purify.hooks[name] = fn;
		}
	}
}));

const { renderMarkdown, decorateExternalAnchor, preserveIndentation, extractHeadings, noteStats } =
	await import('./render');

/** Renders inside `run`, and how many of them actually parsed. */
function parses(run: () => void): number {
	const before = purify.calls;
	run();
	return purify.calls - before;
}

describe('renderMarkdown caching', () => {
	it('parses the same source once', () => {
		const source = `# Streaming reply ${Math.random()}\n\nWith **bold** text.`;
		let first = '';
		let second = '';
		expect(parses(() => (first = renderMarkdown(source)))).toBe(1);
		expect(parses(() => (second = renderMarkdown(source)))).toBe(0);
		expect(second).toBe(first);
	});

	it('keys on the options that change the output', () => {
		const source = `# Heading ${Math.random()}`;
		const plain = renderMarkdown(source);
		let demoted = '';
		// Chat demotes headings by three; a cache blind to that would hand the
		// chat the page-level h1 the note view just rendered.
		expect(parses(() => (demoted = renderMarkdown(source, { headingOffset: 3 })))).toBe(1);
		expect(demoted).not.toBe(plain);
		expect(demoted).toContain('<h4');
	});

	it('re-renders wikilinks, whose targets resolve against the live vault', () => {
		const source = `See [[Note ${Math.random()}]]`;
		const resolveNote = () => null;
		expect(parses(() => renderMarkdown(source, { resolveNote }))).toBe(1);
		expect(parses(() => renderMarkdown(source, { resolveNote }))).toBe(1);
	});

	it('neither reads nor fills the cache with cache: false', () => {
		const source = `A reply still streaming ${Math.random()}`;
		expect(parses(() => renderMarkdown(source, { cache: false }))).toBe(1);
		expect(parses(() => renderMarkdown(source, { cache: false }))).toBe(1);
		// Nothing was stored: the first cached render still parses, then hits.
		expect(parses(() => renderMarkdown(source))).toBe(1);
		expect(parses(() => renderMarkdown(source))).toBe(0);
		// And a stored entry is not handed back to an uncached render.
		expect(parses(() => renderMarkdown(source, { cache: false }))).toBe(1);
	});

	it('keeps `cache` out of the key', () => {
		const source = `Finished reply ${Math.random()}`;
		const first = renderMarkdown(source);
		let again = '';
		expect(parses(() => (again = renderMarkdown(source, { cache: true })))).toBe(0);
		expect(again).toBe(first);
	});

	it('keeps finished messages through a long streamed reply', () => {
		// Each streamed prefix used to become an entry of its own, so a reply of
		// more than 200 flushes evicted every finished message on screen.
		const finished = `Finished reply ${Math.random()}`;
		renderMarkdown(finished, { chat: true, headingOffset: 3 });
		let reply = '';
		for (let i = 0; i < 250; i++) {
			reply += `token${i} `;
			renderMarkdown(reply, { chat: true, headingOffset: 3, cache: false });
		}
		expect(parses(() => renderMarkdown(finished, { chat: true, headingOffset: 3 }))).toBe(0);
	});
});

// Everything below happens before the (stubbed) sanitizer, so the stub hides
// nothing: this is the HTML DOMPurify would be handed.
describe('chat replies', () => {
	const chat = (source: string) => renderMarkdown(source, { chat: true, headingOffset: 3 });

	it('turns a remote image into a link instead of loading it', () => {
		const html = chat('![a](https://evil.test/?d=1)');
		expect(html).not.toContain('<img');
		expect(html).toContain('href="https://evil.test/?d=1"');
		expect(html).toContain('rel="noopener noreferrer"');
		expect(html).toContain('>a</a>');
		// No alt text: the URL is the label.
		expect(chat('![](https://evil.test/p)')).toContain('>https://evil.test/p</a>');
	});

	it('shows raw HTML as text, so a typed <img> fetches nothing', () => {
		const html = chat('<img src="https://evil.test/x">');
		expect(html).not.toContain('<img');
		expect(html).toContain('&lt;img');
		expect(chat('<p style="background:url(https://evil.test/y)">x</p>')).not.toContain('<p style');
		// A bare line break loads nothing and tables lean on it.
		expect(chat('| a |\n|---|\n| x<br>y |')).toContain('x<br>y');
	});

	it('keeps the vault’s own images and inline data', () => {
		expect(chat('![a](/api/attachments/x.png)')).toContain('<img src="/api/attachments/x.png"');
		expect(chat('![a](/api/paper/n/p/render.png)')).toContain('<img src="/api/paper/n/p/render.png"');
		expect(chat('![a](api/attachments/x.png)')).toContain('<img src="api/attachments/x.png"');
		expect(chat('![a](data:image/png;base64,AAAA)')).toContain('<img src="data:image/png;base64,AAAA"');
		expect(chat('![a](javascript:alert(1))')).not.toContain('<img');
		expect(chat('![a](//evil.test/x.png)')).not.toContain('<img');
		expect(chat('![a](/api/attachments/../../elsewhere)')).not.toContain('<img');
		expect(chat('![a](/api/attachments/%2e%2e/%2e%2e/elsewhere)')).not.toContain('<img');
	});

	it('leaves images and HTML alone outside chat', () => {
		expect(renderMarkdown('![a](https://x/y.png)')).toContain('<img src="https://x/y.png"');
		expect(renderMarkdown('<img src="https://x/z.png">')).toContain('<img src="https://x/z.png">');
	});

	it('keys the cache on chat mode', () => {
		const source = `![a](https://x/${Math.random()}.png)`;
		let plain = '';
		let inChat = '';
		expect(parses(() => (plain = renderMarkdown(source)))).toBe(1);
		expect(parses(() => (inChat = renderMarkdown(source, { chat: true })))).toBe(1);
		expect(parses(() => renderMarkdown(source))).toBe(0);
		expect(parses(() => renderMarkdown(source, { chat: true }))).toBe(0);
		expect(plain).toContain('<img');
		expect(inChat).not.toContain('<img');
	});
});

describe('wikilinks', () => {
	/** Renders with a resolver that knows `names`, recording every lookup. */
	function withNotes(source: string, names: string[]) {
		const lookups: string[] = [];
		const html = renderMarkdown(source, {
			chat: true,
			resolveNote: (name) => {
				lookups.push(name);
				return names.includes(name) ? `notes/${name}.md` : null;
			}
		});
		return { html, lookups };
	}

	it('looks up the name as written, not marked’s escaped HTML of it', () => {
		const { html, lookups } = withNotes("[[Mom's recipes]]", ["Mom's recipes"]);
		expect(lookups).toEqual(["Mom's recipes"]);
		expect(html).toContain(`<a href="/note/notes/Mom's%20recipes.md">Mom's recipes</a>`);
	});

	it('escapes the label exactly once', () => {
		const { html, lookups } = withNotes('[[Q&A]]', ['Q&A']);
		expect(lookups).toEqual(['Q&A']);
		expect(html).toContain('>Q&amp;A</a>');
		expect(html.match(/&amp;/g)).toHaveLength(1);
	});

	it('does not read emphasis inside a link', () => {
		const { html, lookups } = withNotes('[[x*y*z]] and *around [[a_b_c]] it*', ['x*y*z', 'a_b_c']);
		expect(lookups).toEqual(['x*y*z', 'a_b_c']);
		expect(html).toContain('>x*y*z</a>');
		expect(html).toContain('<em>around <a href="/note/notes/a_b_c.md">a_b_c</a> it</em>');
		expect(html).not.toContain('<em>y');
	});

	it('shows the label and marks a missing note as dead', () => {
		const labelled = withNotes("[[Don't forget|label]]", ["Don't forget"]);
		expect(labelled.lookups).toEqual(["Don't forget"]);
		expect(labelled.html).toContain('>label</a>');
		const missing = withNotes('[[Missing]]', []);
		expect(missing.html).toContain('<span class="dead-link" title="No note named Missing">Missing</span>');
	});

	it('stays code inside code spans and fences', () => {
		for (const source of ['`[[x]]`', '```\n[[x]]\n```']) {
			const { html, lookups } = withNotes(source, ['x']);
			expect(html).not.toContain('<a');
			expect(html).toContain('[[x]]');
			expect(lookups).toEqual([]);
		}
	});

	it('stays literal text without a resolver', () => {
		expect(renderMarkdown('See [[Note]]')).toBe('<p>See [[Note]]</p>\n');
	});
});

/** Just enough of an Element for the hook: tag name plus attributes. */
function anchor(href: string | null, tagName = 'A') {
	const attrs = new Map<string, string>();
	if (href !== null) attrs.set('href', href);
	return {
		tagName,
		attrs,
		getAttribute: (name: string) => attrs.get(name) ?? null,
		setAttribute: (name: string, value: string) => void attrs.set(name, value)
	};
}

describe('decorateExternalAnchor', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('is registered on the sanitizer after attributes are cleaned', () => {
		expect(purify.hooks.afterSanitizeAttributes).toBe(decorateExternalAnchor);
	});

	it.each(['https://example.com', 'http://example.com/page', 'mailto:someone@example.com'])(
		'sends %s outside the app',
		(href) => {
			vi.stubGlobal('location', new URL('https://notes.exscalia.art/omnia'));
			const node = anchor(href);
			decorateExternalAnchor(node);
			expect(node.attrs.get('target')).toBe('_blank');
			expect(node.attrs.get('rel')).toBe('noopener noreferrer');
		}
	);

	it.each([
		'/note/Some%20Note.md',
		'#heading',
		'https://notes.exscalia.art/note/x.md',
		'javascript:alert(1)'
	])('leaves %s alone', (href) => {
		vi.stubGlobal('location', new URL('https://notes.exscalia.art/omnia'));
		const node = anchor(href);
		decorateExternalAnchor(node);
		expect(node.attrs.has('target')).toBe(false);
		expect(node.attrs.has('rel')).toBe(false);
	});

	it('leaves relative links alone under the desktop tauri:// origin', () => {
		vi.stubGlobal('location', new URL('tauri://localhost/omnia'));
		const node = anchor('/note/x.md');
		decorateExternalAnchor(node);
		expect(node.attrs.has('target')).toBe(false);
	});

	it('treats every web link as external when there is no location', () => {
		const internal = anchor('/note/x.md');
		decorateExternalAnchor(internal);
		expect(internal.attrs.has('target')).toBe(false);
		const external = anchor('https://example.com');
		decorateExternalAnchor(external);
		expect(external.attrs.get('target')).toBe('_blank');
	});

	it('ignores anchors without an href and other elements', () => {
		const bare = anchor(null);
		decorateExternalAnchor(bare);
		expect(bare.attrs.size).toBe(0);
		const img = anchor('https://example.com', 'IMG');
		decorateExternalAnchor(img);
		expect(img.attrs.has('target')).toBe(false);
	});
});

describe('preserveIndentation', () => {
	it('keeps an indented continuation line indented', () => {
		expect(preserveIndentation('Parent\n  child')).toBe('Parent\n<br>&nbsp;&nbsp;child');
		// Four columns straight after a paragraph line continue it; they are
		// not a code block, which needs a blank line first.
		expect(preserveIndentation('Parent\n    deeper')).toBe('Parent\n<br>&nbsp;&nbsp;&nbsp;&nbsp;deeper');
		expect(preserveIndentation('Parent\n\tchild')).toBe('Parent\n<br>&nbsp;&nbsp;&nbsp;&nbsp;child');
	});

	it('leaves an indented code block alone, across its blank lines', () => {
		const code = 'Intro\n\n    first()\n\n    second()\n      nested()';
		expect(preserveIndentation(code)).toBe(code);
		expect(preserveIndentation('    at the top')).toBe('    at the top');
	});

	it('leaves lists, quotes and fences alone', () => {
		for (const source of [
			'- a\n  - b\n    - c',
			'1. a\n   2) b',
			'Quote\n  > nested',
			'```\nParent\n  child\n```',
			'~~~\n  child\n~~~'
		]) {
			expect(preserveIndentation(source)).toBe(source);
		}
	});
});

describe('extractHeadings', () => {
	it('lists ATX headings with their level, plain text and line', () => {
		const source = '# Title\ntext\n## Sub *em* `code`\n### Closed ###\n#nospace';
		expect(extractHeadings(source)).toEqual([
			{ level: 1, text: 'Title', line: 1 },
			{ level: 2, text: 'Sub em code', line: 3 },
			{ level: 3, text: 'Closed', line: 4 }
		]);
	});

	it('skips headings inside fences', () => {
		const source = '# Kept\n```\n# comment\n```\n~~~\n## also code\n~~~\n## After';
		expect(extractHeadings(source)).toEqual([
			{ level: 1, text: 'Kept', line: 1 },
			{ level: 2, text: 'After', line: 8 }
		]);
	});
});

describe('noteStats', () => {
	it('counts the body without its frontmatter', () => {
		expect(noteStats('---\ntitle: x\ntags: [a, b]\n---\none two  three\n')).toEqual({
			words: 3,
			characters: 'one two  three\n'.length,
			readingMinutes: 1
		});
	});

	it('rounds reading time at 220 words a minute, never below one', () => {
		expect(noteStats('').readingMinutes).toBe(1);
		expect(noteStats('word '.repeat(440)).readingMinutes).toBe(2);
		expect(noteStats('word '.repeat(770)).readingMinutes).toBe(4);
	});
});
