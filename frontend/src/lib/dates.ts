// Calendar-date helpers shared by every surface that asks "what day is it".

/** Today in the browser's local zone as YYYY-MM-DD (not UTC — due dates, daily
 *  notes and the greeting are local-day concepts, and toISOString() would roll
 *  a day early after 8pm ET). */
export function localToday(): string {
	const d = new Date();
	const y = String(d.getFullYear()).padStart(4, '0');
	const m = String(d.getMonth() + 1).padStart(2, '0');
	const day = String(d.getDate()).padStart(2, '0');
	return `${y}-${m}-${day}`;
}
