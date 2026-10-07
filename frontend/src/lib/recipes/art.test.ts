import { describe, expect, it } from 'vitest';
import { artRows } from './art';

describe('artRows', () => {
	it('joins runs of one colour and leaves unkeyed characters in the default ink', () => {
		const rows = artRows(' (~) \n\\___/', '.aab.\n.....', { a: '#e8c070', b: '#ffffff' });
		expect(rows).toEqual([
			[{ text: ' ' }, { text: '(~', color: '#e8c070' }, { text: ')', color: '#ffffff' }, { text: ' ' }],
			[{ text: '\\___/' }]
		]);
	});

	it('reads missing colour rows and keys as default ink', () => {
		expect(artRows('ab\ncd', 'x')).toEqual([[{ text: 'ab' }], [{ text: 'cd' }]]);
	});

	it('ignores palette values that are not hex colours', () => {
		const rows = artRows('ab', 'ab', { a: 'red; background: url(x)', b: '#0f08' });
		expect(rows).toEqual([[{ text: 'a' }, { text: 'b', color: '#0f08' }]]);
	});

	it('draws nothing for no art', () => {
		expect(artRows('')).toEqual([]);
	});
});
