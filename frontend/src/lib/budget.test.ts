// Settings > OMNIA > Monthly budget. A misread here is silent and costly: a
// budget of 2 tokens pauses every unattended job until someone notices.
import { describe, expect, it } from 'vitest';
import { parseTokenBudget } from './budget';

describe('parseTokenBudget', () => {
	it.each([
		['2M', 2_000_000],
		['1.5m', 1_500_000],
		['50k', 50_000],
		['50 K', 50_000],
		['1,500,000', 1_500_000],
		['1_000', 1_000],
		[' 250 ', 250],
		['1 000 000', 1_000_000],
		['1.2345k', 1_235],
		['', 0],
		['   ', 0],
		['0', 0],
		['0k', 0],
		['0.0k', 0]
	])('%j -> %d', (input, tokens) => {
		expect(parseTokenBudget(input)).toBe(tokens);
	});

	it.each(['abc', '-5', '2MB', '1e6', '.5k', '2 M tokens', 'k', ',', '1.'])(
		'%j is rejected',
		(input) => {
			expect(parseTokenBudget(input)).toBeNull();
		}
	);

	// '1.5' is 1.5M with the M left off; rounding it would save a 2-token
	// budget, and '0.4' would round to 0, which means no limit at all.
	it.each(['1.5', '0.4', '2.0', '0.0004k'])('%j is rejected rather than rounded', (input) => {
		expect(parseTokenBudget(input)).toBeNull();
	});

	it('rejects a number too long to be finite rather than sending Infinity', () => {
		expect(parseTokenBudget('9'.repeat(400))).toBeNull();
	});
});
