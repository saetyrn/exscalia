/** The monthly token budget field, parsed strictly.
 *
 * The usage line above the field prints budgets as '1.5M' / '250k', which
 * invites typing them back that way; parseInt read '2M' as 2 and '1,500,000'
 * as 1 and saved it without a word. Anything this does not understand is
 * rejected (null) so the caller can say so instead of guessing.
 */

const SUFFIX: Record<string, number> = { k: 1_000, m: 1_000_000 };

/** Tokens for a budget string: '' is 0 (no limit), '2M' is 2000000, '50k' is
 * 50000, '1,500,000' is 1500000. Null for anything else, negatives included,
 * and for a fraction with no suffix ('1.5' is 1.5M missing its M, not 2
 * tokens) or one that rounds to 0 tokens (0 means no limit). */
export function parseTokenBudget(input: string): number | null {
	const text = input.trim();
	if (text === '') return 0;
	const match = /^(\d+(?:\.\d+)?)\s*([km])?$/i.exec(text.replace(/[,_\s]/g, ''));
	if (!match) return null;
	if (match[1].includes('.') && !match[2]) return null;
	const amount = Number(match[1]);
	const value = Math.round(amount * (match[2] ? SUFFIX[match[2].toLowerCase()] : 1));
	if (value === 0 && amount !== 0) return null;
	return Number.isFinite(value) ? value : null;
}
