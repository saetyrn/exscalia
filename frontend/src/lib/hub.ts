// What the hub (the home page) shows, worked out from the day's briefing and
// the note list. Kept apart from the page so it can be tested without a DOM.
import type { BriefingDay, BriefingStats, CalTask, CalendarEventOccurrence } from '$lib/api';

/** Yesterday at a glance, as parts of one muted line. Only counts that
 *  happened are listed — a "0 tasks done" is a fact nobody asked for. */
export function statParts(stats: BriefingStats | null | undefined): string[] {
	if (!stats) return [];
	const parts: string[] = [];
	const n = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`;
	// Edits only stand in for creations; both would double-count the same day.
	if (stats.notes_created) parts.push(n(stats.notes_created, 'note'));
	else if (stats.notes_modified) parts.push(`${stats.notes_modified} edited`);
	if (stats.tasks_completed) parts.push(`${n(stats.tasks_completed, 'task')} done`);
	if (stats.flashcards_due) parts.push(`${n(stats.flashcards_due, 'card')} due`);
	// Rounded before the test, so six logged minutes never reads "0.0h".
	const hours = Math.round((stats.activity_hours_yesterday ?? 0) * 10) / 10;
	if (hours) parts.push(`${hours.toFixed(1)}h logged`);
	if (stats.pending_changes) parts.push(`${stats.pending_changes} staged`);
	if (stats.media_in_progress) parts.push(`${stats.media_in_progress} in progress`);
	return parts;
}

/** The first few lines of the daily note worth reading. Skipped: frontmatter,
 *  task metadata comments, headings and tag-only lines (the template's
 *  scaffolding), and list items with nothing written in them yet. */
export function noteExcerpt(content: string, limit = 4): string[] {
	let body = content.replace(/\r\n/g, '\n');
	const fm = /^---\n[\s\S]*?\n---(?:\n|$)/.exec(body);
	if (fm) body = body.slice(fm[0].length);
	const lines: string[] = [];
	for (const raw of body.split('\n')) {
		const line = raw.replace(/<!--.*?-->/g, '').trim();
		if (!line || /^#{1,6}\s/.test(line)) continue;
		if (/^(#[\w/-]+\s*)+$/.test(line)) continue;
		if (/^([-*+]|\d+\.)(\s+\[[ xX/]\])?$/.test(line)) continue;
		lines.push(line);
		if (lines.length === limit) break;
	}
	return lines;
}

export interface UpcomingItem {
	key: string;
	date: string;
	kind: 'event' | 'task';
	title: string;
	/** Start time for a timed event, '' otherwise. */
	time: string;
	event?: CalendarEventOccurrence;
	task?: CalTask;
}

export function eventTime(ev: CalendarEventOccurrence): string {
	if (ev.all_day) return '';
	const at = new Date(ev.occurrence_start);
	if (Number.isNaN(at.getTime())) return '';
	return at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

/** Everything after today in the briefing's week, in day order, events first
 *  within a day. Daily tasks are left out: ticking one spawns tomorrow's copy,
 *  and the Dailies panel already holds them. */
export function comingUp(week: BriefingDay[] | null | undefined, today: string): UpcomingItem[] {
	const out: UpcomingItem[] = [];
	for (const day of week ?? []) {
		if (day.date <= today) continue;
		for (const ev of day.events) {
			out.push({
				key: `e:${ev.id}`,
				date: day.date,
				kind: 'event',
				title: ev.title,
				time: eventTime(ev),
				event: ev
			});
		}
		for (const task of day.tasks) {
			if (task.repeats === 'daily') continue;
			out.push({ key: `t:${task.id}`, date: day.date, kind: 'task', title: task.text, time: '', task });
		}
	}
	return out;
}

/** "Sat 31": the weekday and the day of the month. */
export function dayLabel(iso: string): string {
	const d = new Date(`${iso}T00:00:00`);
	return `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${d.getDate()}`;
}

/** Whether a timed event has ended. All-day events last the whole day. */
export function eventOver(ev: CalendarEventOccurrence, now: Date): boolean {
	if (ev.all_day) return false;
	const end = new Date(ev.occurrence_end);
	return !Number.isNaN(end.getTime()) && end.getTime() <= now.getTime();
}
