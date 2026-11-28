// What the Paper viewer reads off its own scroller and page ids, kept pure so
// the rules can be tested without a browser.

/** A vertical extent in viewport pixels — enough of a DOMRect for the rules. */
export interface Span {
	top: number;
	bottom: number;
}

/** The page being read. At the bottom of the scroller it is the last page: a
 * page shorter than the reader (an A4 sheet on a phone, a 16:9 import on a
 * desktop) can never bring its top up to the reader's, so any "top has
 * passed" rule leaves it unreachable. Otherwise it is the page filling most
 * of the reader, the earlier one on a tie. */
export function pickCurrentIndex(reader: Span, sheets: Span[], atBottom: boolean): number {
	if (sheets.length === 0) return 0;
	if (atBottom) return sheets.length - 1;
	let best = 0;
	let most = -1;
	sheets.forEach((sheet, i) => {
		const visible = Math.max(
			0,
			Math.min(sheet.bottom, reader.bottom) - Math.max(sheet.top, reader.top)
		);
		if (visible > most) {
			best = i;
			most = visible;
		}
	});
	return best;
}

/** When the server parked a conflict copy, from its
 * `<id>.conflict-YYYYMMDDHHMMSS` id (UTC). A same-second collision rewrites
 * the last digit, so the seconds are taken on trust and dropped when they
 * are not a real second; anything else that is not a real date is null. */
export function conflictStamp(id: string): Date | null {
	const match = /\.conflict-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/.exec(id);
	if (!match) return null;
	const [year, month, day, hour, minute, second] = match.slice(1).map(Number);
	const date = new Date(Date.UTC(year, month - 1, day, hour, minute, second < 60 ? second : 0));
	// Date.UTC rolls an out-of-range field into the next one; a stamp that
	// does not survive the round trip was never a time.
	if (
		date.getUTCFullYear() !== year ||
		date.getUTCMonth() !== month - 1 ||
		date.getUTCDate() !== day ||
		date.getUTCHours() !== hour ||
		date.getUTCMinutes() !== minute
	)
		return null;
	return date;
}
