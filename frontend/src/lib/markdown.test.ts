// The inline #tag grammar has to agree with the backend indexer, or a chip
// links to a tag page the server never filled ('#caf' for #café). Both sides
// run testing/tag-grammar.json — backend/tests/test_tag_grammar.py loads the
// same file.
import { describe, expect, it } from 'vitest';
import { extractLiveTags, extractNoteTags, sanitizeTag } from './markdown';
import cases from './testing/tag-grammar.json';

describe('extractNoteTags', () => {
	it.each(cases)('$body', ({ body, tags }) => {
		expect(extractNoteTags(body, {})).toEqual(tags);
	});

	it('skips the frontmatter block and keeps its tags', () => {
		const content = '---\ntags: [résumé]\n---\n\n#café in the body';
		expect(extractLiveTags(content)).toEqual(['résumé', 'café']);
	});
});

describe('sanitizeTag', () => {
	it.each([
		['résumé', 'résumé'],
		['Übung', 'Übung'],
		['日本語', '日本語'],
		[' my tag ', 'my-tag'],
		['#x', 'x'],
		['##a b!', 'a-b'],
		['résumé', 'résumé']
	])('%j -> %j', (raw, tag) => {
		expect(sanitizeTag(raw)).toBe(tag);
	});
});
