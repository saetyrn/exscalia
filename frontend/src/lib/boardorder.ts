/** Task-board ordering, as pure functions of the task list.
 *
 * This used to live inside the board route. The rules it carries are the ones
 * that break silently: the drop target is a gap among the column WITHOUT the
 * dragged card (HANDOFF §7), the live preview uses the same insertion
 * arithmetic as the drop so the preview is the outcome, and a reorder only
 * ever rewrites its own column's slots. Plain TypeScript, so they are pinned
 * down in boardorder.test.ts; the DOM geometry stays in dragmath.ts.
 */

import type { TaskStatus } from './api';

/** The subset of a CalTask the ordering cares about. */
export interface BoardCard {
	id: string;
	status: TaskStatus;
}

/** Where a drag would land: a gap index among the column's cards without the
 *  dragged one. */
export interface DropTarget {
	status: TaskStatus;
	index: number;
}

function columnIds(tasks: readonly BoardCard[], status: TaskStatus): string[] {
	return tasks.filter((task) => task.status === status).map((task) => task.id);
}

/** The column as it will read if the drag drops here — the dragged card
 *  lifted out of wherever it lives and sitting in the aimed gap. */
export function previewColumn<T extends BoardCard>(
	tasks: readonly T[],
	status: TaskStatus,
	dragging: string | null,
	dropAt: DropTarget | null
): T[] {
	const base = tasks.filter((task) => task.status === status);
	if (!dragging || !dropAt) return base;
	const dragged = tasks.find((t) => t.id === dragging);
	if (!dragged) return base;
	const without = base.filter((t) => t.id !== dragging);
	if (dropAt.status !== status) return dragged.status === status ? without : base;
	const next = [...without];
	next.splice(Math.min(dropAt.index, next.length), 0, dragged);
	return next;
}

/** `status`'s ids once `taskId` sits in gap `gapIndex` — dropAt's convention,
 *  so no own-slot adjustment is needed. Past-the-end gaps clamp to the end. */
export function orderAfterPlace(
	tasks: readonly BoardCard[],
	taskId: string,
	status: TaskStatus,
	gapIndex: number
): string[] {
	const next = columnIds(tasks, status).filter((id) => id !== taskId);
	next.splice(Math.max(0, Math.min(gapIndex, next.length)), 0, taskId);
	return next;
}

/** The card's column with it stepped one place up or down; null when it is
 *  already at that end (or unknown). Dragging is pointer-only, so this is how
 *  the same ordering is reached from the card menu. */
export function orderAfterStep(
	tasks: readonly BoardCard[],
	taskId: string,
	delta: -1 | 1
): string[] | null {
	const task = tasks.find((item) => item.id === taskId);
	if (!task) return null;
	const ids = columnIds(tasks, task.status);
	const from = ids.indexOf(taskId);
	const to = from + delta;
	if (to < 0 || to >= ids.length) return null;

	// Swap past the neighbour, then rebuild the column around it.
	const neighbour = ids[to];
	const next = ids.filter((id) => id !== taskId);
	const at = next.indexOf(neighbour);
	next.splice(delta < 0 ? at : at + 1, 0, taskId);
	return next;
}

/** `tasks` with `status`'s cards reading in `ids` order, the other columns'
 *  entries exactly where they sit. Returns `tasks` itself — nothing to apply —
 *  unless `ids` is a permutation of that column: after a failed cross-column
 *  move the dragged card is still in its old column, and writing it into a
 *  slot here would duplicate it and drop the column's last card. */
export function applyOrder<T extends BoardCard>(
	tasks: T[],
	status: TaskStatus,
	ids: readonly string[]
): T[] {
	const slots = columnIds(tasks, status);
	const members = new Set(slots);
	if (
		ids.length !== slots.length ||
		new Set(ids).size !== ids.length ||
		ids.some((id) => !members.has(id))
	)
		return tasks;
	const byId = new Map(tasks.map((task) => [task.id, task]));
	const ordered = ids.map((id) => byId.get(id) as T);
	let next = 0;
	return tasks.map((task) => (task.status === status ? ordered[next++] : task));
}
