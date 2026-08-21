<script lang="ts">
	import { untrack } from 'svelte';
	import { flip } from 'svelte/animate';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { fade } from 'svelte/transition';
	import { api, encodePath, type CalTask, type TaskStatus } from '$lib/api';
	import { gapIndexAmong } from '$lib/dragmath';
	import { vault } from '$lib/stores.svelte';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';
	import { touchDrag } from '$lib/touchdrag';

	const statuses: { id: TaskStatus; label: string; symbol: string }[] = [
		{ id: 'todo', label: 'To do', symbol: '○' },
		{ id: 'in_progress', label: 'In progress', symbol: '◐' },
		{ id: 'done', label: 'Done', symbol: '✓' }
	];

	let { data } = $props();

	// '' is the universal board; anything else is that folder's own board. The
	// layout remounts this route on every navigation, so a plain read is safe.
	const folder = untrack(() => data.folder ?? '');
	const missing = untrack(() => data.missing === true);
	// The page keeps its "Tasks" heading; "Global" is the dropdown's name for
	// the universal board.
	const boardName = folder ? (folder.split('/').pop() ?? folder) : 'Tasks';

	// Initial-only capture is deliberate: this route remounts per navigation.
	const initialTasks = untrack(() => data.tasks);
	let tasks: CalTask[] = $state(initialTasks ?? []);
	let loading = $state(initialTasks === null);
	// The route load() covers the initial fetch; recover here only if it failed
	// — never when the folder simply has no board.
	if (initialTasks === null && !missing) void load();

	// Every folder that owns a task board, for the board switcher. Same
	// derivation the sidebar uses for its board glyphs.
	const boardFolders = $derived(
		[
			...new Set(
				vault.notes
					.filter((n) => n.board)
					.map((n) => n.path.split('/').slice(0, -1).join('/'))
			)
		].sort((a, b) => a.localeCompare(b))
	);

	function switchBoard(target: string) {
		if (target === folder) return;
		void goto(target ? `/tasks/${encodePath(target)}` : '/tasks');
	}
	let showCompleted = $state(false);
	let addOpen = $state(page.url.searchParams.get('new') === '1');
	let text = $state('');
	let description = $state('');
	let due = $state('');
	let dueOn = $state(false);
	let adding = $state(false);

	function focusNow(el: HTMLElement) {
		el.focus();
	}

	function closeOnEscape(event: KeyboardEvent) {
		if (event.key === 'Escape') addOpen = false;
	}
	let dragging = $state<string | null>(null);
	let dragOver = $state<TaskStatus | null>(null);
	// Which gap the card would land in: an index into the column's visible
	// cards, counting the dragged card's own slot.
	let dropAt = $state<{ status: TaskStatus; index: number } | null>(null);
	let mobileStatus = $state<TaskStatus>('todo');
	let moveOpen = $state<string | null>(null);

	async function load() {
		// Only the very first fetch may blank the board — a refresh with cards
		// on screen flashing to "Loading tasks…" reads as flicker.
		if (tasks.length === 0) loading = true;
		try {
			const fresh = await api.listTasks(showCompleted, '', folder);
			// The reload after our own reorder echoes back exactly what the
			// optimistic update already shows; skipping the assignment skips
			// the re-render entirely.
			if (JSON.stringify(fresh) !== JSON.stringify(tasks)) tasks = fresh;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			loading = false;
		}
	}

	// A task toggled on another device (or by OMNIA) rewrites a note, which
	// arrives here as a live `note` event. Reload the board — but coalesce
	// bursts and never yank the DOM out from under an active drag; the drop
	// handler's own reload covers that case.
	let liveRefresh: ReturnType<typeof setTimeout> | null = null;
	$effect(() => {
		const onLive = (e: Event) => {
			const detail = (e as CustomEvent).detail as { type?: string };
			if (detail?.type !== 'note' && detail?.type !== 'note_removed') return;
			if (liveRefresh) clearTimeout(liveRefresh);
			liveRefresh = setTimeout(() => {
				liveRefresh = null;
				if (dragging === null && moveOpen === null) void load();
			}, 600);
		};
		window.addEventListener('exscalia:live', onLive);
		return () => {
			window.removeEventListener('exscalia:live', onLive);
			if (liveRefresh) clearTimeout(liveRefresh);
		};
	});


	function column(status: TaskStatus) {
		return tasks.filter((task) => task.status === status);
	}

	async function addTask() {
		if (!text.trim() || adding) return;
		adding = true;
		try {
			const task = await api.createTask({
				text: text.trim(),
				// The server maps the folder to its board note; the client never
				// guesses that path, which may be numbered or renamed.
				folder: folder || undefined,
				due: (dueOn && due) || null,
				description: description.trim() || null
			});
			tasks = [task, ...tasks];
			text = '';
			description = '';
			due = '';
			dueOn = false;
			addOpen = false;
			notify('Task added.', { tone: 'success' });
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			adding = false;
		}
	}

	async function setStatus(task: CalTask, status: TaskStatus) {
		if (task.status === status) return;
		const previous = task.status;
		task.status = status;
		task.done = status === 'done';
		moveOpen = null;
		try {
			const updated = await api.updateTask(task.id, { status });
			Object.assign(task, updated);
			notify(
				status === 'done'
					? 'Task completed.'
					: status === 'in_progress'
						? 'Task moved to In progress.'
						: 'Task moved to To do.',
				{
					tone: 'success',
					actionLabel: 'Undo',
					action: () => void setStatus(task, previous)
				}
			);
		} catch (cause) {
			task.status = previous;
			task.done = previous === 'done';
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function editTask(task: CalTask) {
		const next = await promptDialog('Edit the task text:', {
			title: 'Edit task',
			value: task.text,
			confirmLabel: 'Save'
		});
		if (!next || next.trim() === task.text) return;
		try {
			Object.assign(task, await api.updateTask(task.id, { text: next.trim() }));
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function editDue(task: CalTask) {
		const next = await promptDialog('Enter a due date as YYYY-MM-DD, or “none” to clear it:', {
			title: 'Task due date',
			value: task.due ?? '',
			placeholder: 'YYYY-MM-DD or none',
			confirmLabel: 'Continue'
		});
		if (next === null) return;
		const due = next.trim().toLowerCase() === 'none' || !next.trim() ? null : next.trim();
		if (due && !/^\d{4}-\d{2}-\d{2}$/.test(due)) {
			notify('Use a date in YYYY-MM-DD format.', { tone: 'error' });
			return;
		}
		const visible = due
			? await confirmDialog('Show this due date in the portable Markdown task line?', {
					title: 'Due-date visibility',
					confirmLabel: 'Show date',
					cancelLabel: 'Keep hidden'
				})
			: false;
		try {
			Object.assign(
				task,
				await api.updateTask(task.id, {
					due,
					due_set: true,
					due_visible: visible
				})
			);
			moveOpen = null;
			notify(due ? 'Due date updated.' : 'Due date cleared.', { tone: 'success' });
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	/** One click to drop a due date, without walking through the edit prompt. */
	async function clearDue(task: CalTask) {
		moveOpen = null;
		try {
			Object.assign(
				task,
				await api.updateTask(task.id, { due: null, due_set: true, due_visible: false })
			);
			notify('Due date removed.', { tone: 'success' });
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function removeTask(task: CalTask) {
		if (
			!(await confirmDialog(`Permanently remove “${task.text}” from its note? Its milestone history will remain on the calendar.`, {
				title: 'Delete task',
				confirmLabel: 'Delete task',
				danger: true
			}))
		)
			return;
		try {
			await api.deleteTask(task.id);
			tasks = tasks.filter((item) => item.id !== task.id);
			notify('Task removed. Calendar milestones were preserved.');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	function startDrag(event: DragEvent, task: CalTask) {
		dragging = task.id;
		event.dataTransfer?.setData('text/plain', task.id);
		if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
	}

	function endDrag() {
		dragging = null;
		dragOver = null;
		dropAt = null;
	}

	/** Which gap the pointer is in: the number of settled cards whose midpoint
	 *  sits above it. Measured against the on-screen preview WITHOUT the
	 *  dragged card — that makes it a fixed point under preview motion: after
	 *  the cards shuffle, the same pointer position counts the same midpoints,
	 *  so the target cannot flap. (Targeting the card under the pointer did
	 *  flap: the preview moved that card, which changed the target, which
	 *  moved it back — the jitter.) `dropAt.index` is therefore an insertion
	 *  position among the column-minus-dragged, everywhere. */
	function gapIndex(columnEl: HTMLElement, y: number): number {
		// Settled positions, not live rects: mid-flip rects glide past the
		// pointer and would flap the target (visible on short rows; latent
		// here on tall cards).
		return gapIndexAmong(
			columnEl.querySelectorAll('article[data-task-id]:not(.dragging)'),
			y
		);
	}

	function columnDragOver(event: DragEvent, status: TaskStatus) {
		if (!dragging) return;
		event.preventDefault();
		const index = gapIndex(event.currentTarget as HTMLElement, event.clientY);
		dragOver = status;
		// Same-value writes still re-render the preview; skip them.
		if (dropAt?.status !== status || dropAt.index !== index) dropAt = { status, index };
	}

	/** The column as it will read if the drag drops here — the dragged card
	 *  lifted out of wherever it lives and sitting in the aimed gap. Same
	 *  insertion arithmetic as placeAt, so the preview is the outcome. */
	function previewColumn(status: TaskStatus) {
		const base = column(status);
		if (!dragging || !dropAt) return base;
		const dragged = tasks.find((t) => t.id === dragging);
		if (!dragged) return base;
		const without = base.filter((t) => t.id !== dragging);
		if (dropAt.status !== status) return dragged.status === status ? without : base;
		const next = [...without];
		next.splice(Math.min(dropAt.index, next.length), 0, dragged);
		return next;
	}

	async function drop(status: TaskStatus) {
		const target = dropAt;
		const task = tasks.find((item) => item.id === dragging);
		endDrag();
		if (!task) return;
		if (task.status !== status) await setStatus(task, status);
		if (target && target.status === status) await placeAt(task, status, target.index);
	}

	/** Every task in the column, in stored order. */
	function columnIds(status: TaskStatus) {
		return tasks.filter((task) => task.status === status).map((task) => task.id);
	}

	/** Rewrite `tasks` so `status`'s cards read in `ids` order, leaving the
	 *  other columns' entries exactly where they sit. */
	function applyOrder(status: TaskStatus, ids: string[]) {
		const byId = new Map(tasks.map((task) => [task.id, task]));
		const ordered = ids.map((id) => byId.get(id)).filter((task) => task !== undefined);
		let next = 0;
		tasks = tasks.map((task) =>
			task.status === status ? (ordered[next++] ?? task) : task
		);
	}

	async function placeAt(task: CalTask, status: TaskStatus, index: number) {
		// `index` is a gap position among the column WITHOUT the moving card —
		// dropAt's convention — so no own-slot adjustment is needed.
		const remaining = column(status)
			.map((item) => item.id)
			.filter((id) => id !== task.id);
		const anchor = index > 0 ? remaining[Math.min(index, remaining.length) - 1] : null;

		const next = columnIds(status).filter((id) => id !== task.id);
		next.splice(anchor ? next.indexOf(anchor) + 1 : 0, 0, task.id);
		await commitOrder(status, next);
	}

	/** Step a card one place up or down its column. Dragging is mouse-only, so
	 *  this is how the same ordering is reached by touch and by keyboard. */
	async function moveBy(task: CalTask, delta: -1 | 1) {
		const status = task.status;
		const visible = column(status).map((item) => item.id);
		const from = visible.indexOf(task.id);
		const to = from + delta;
		if (from === -1 || to < 0 || to >= visible.length) return;

		// Swap past the neighbour the user can actually see, then rebuild the
		// full column around it so cards the filter is hiding stay put.
		const neighbour = visible[to];
		const next = columnIds(status).filter((id) => id !== task.id);
		const at = next.indexOf(neighbour);
		next.splice(delta < 0 ? at : at + 1, 0, task.id);
		moveOpen = null;
		await commitOrder(status, next);
	}

	async function commitOrder(status: TaskStatus, next: string[]) {
		if (next.join() === columnIds(status).join()) return;
		const previous = tasks;
		applyOrder(status, next);
		try {
			await api.reorderTasks(next);
		} catch (cause) {
			tasks = previous;
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	/** Shared hit-testing for the touch drag: aim at the gap above or below
	 *  the card under the finger, or the end of the column under it. */
	function touchHit(x: number, y: number) {
		const col = document
			.elementFromPoint(x, y)
			?.closest('[data-column]') as HTMLElement | null;
		if (!col) return;
		const status = col.dataset.column as TaskStatus;
		const index = gapIndex(col, y);
		dragOver = status;
		if (dropAt?.status !== status || dropAt.index !== index) dropAt = { status, index };
	}

	function touchDragFor(task: CalTask) {
		return {
			disabled: () => moveOpen !== null,
			onStart: () => (dragging = task.id),
			onMove: touchHit,
			onDrop: () => {
				const status = dragOver;
				if (status) void drop(status);
				else endDrag();
			},
			onCancel: endDrag
		};
	}

	function dateLabel(value: string | null) {
		if (!value) return '';
		return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
</script>

<!-- Clicking anywhere off an open card menu closes it; clicks inside the menu
     belong to its buttons (each action closes on its own). Same idiom as the
     sidebar's context menu. -->
<svelte:window
	onclick={(e) => {
		const target = e.target;
		if (moveOpen !== null) {
			if (!(target instanceof Element && target.closest('.task-menu'))) moveOpen = null;
		}
		// The add form dismisses on click-off only while it's empty — typed
		// text is never thrown away by a stray click.
		if (
			addOpen &&
			text.trim() === '' &&
			description.trim() === '' &&
			!(target instanceof Element && (target.closest('.inline-add') || target.closest('.col-add')))
		) {
			addOpen = false;
		}
	}}
	onkeydown={(e) => {
		if (e.key === 'Escape') moveOpen = null;
	}}
/>

<div class="tasks-page">
	<header>
		<!-- Named from the folder, never the board note's title: the vault pins
		     that to "Tasks" for every board. -->
		<h1>{boardName}</h1>
		{#if folder}
			<p class="board-scope">{folder}</p>
		{/if}
	</header>

	{#if missing}
		<p class="state">
			No task board in <strong>{folder}</strong>. Right-click the folder in the sidebar to
			create one.
		</p>
	{:else}

	<div class="toolbar">
		<select
			class="board-jump"
			aria-label="Switch task board"
			value={folder}
			onchange={(e) => switchBoard((e.currentTarget as HTMLSelectElement).value)}
		>
			<option value="">Global</option>
			{#each boardFolders as f (f)}
				<option value={f}>{f}</option>
			{/each}
		</select>
		<label>
			<input type="checkbox" bind:checked={showCompleted} onchange={() => void load()} />
			Show completed from earlier weeks
		</label>
	</div>

	<div class="mobile-tabs" role="tablist">
		{#each statuses as status (status.id)}
			<button class:active={mobileStatus === status.id} onclick={() => (mobileStatus = status.id)}>
				{status.symbol} {status.label}
				<span>{column(status.id).length}</span>
			</button>
		{/each}
	</div>

	{#if loading}
		<p class="state">Loading tasks…</p>
	{:else}
		<div class="board">
			{#each statuses as status (status.id)}
				<section
					role="group"
					aria-label={status.label}
					data-column={status.id}
					class:hidden-mobile={mobileStatus !== status.id}
					class:drag-over={dragOver === status.id}
					ondragover={(event) => columnDragOver(event, status.id)}
					ondragleave={(event) => {
						// dragleave fires when the pointer crosses INTO a child
						// card, not just out of the column — clearing there made
						// the drop-line blink once per card crossed.
						const into = event.relatedTarget as Node | null;
						if (into && (event.currentTarget as HTMLElement).contains(into)) return;
						dragOver = null;
						dropAt = null;
					}}
					ondrop={(event) => {
						event.preventDefault();
						void drop(status.id);
					}}
				>
					<header>
						<h2>{status.symbol} {status.label}</h2>
						<span class="col-meta">
							{#if status.id === 'todo'}
								<button
									class="col-add"
									title="Add task"
									aria-label="Add task"
									aria-expanded={addOpen}
									onclick={() => (addOpen = !addOpen)}
								>
									+
								</button>
							{/if}
							<span class="count">{column(status.id).length}</span>
						</span>
					</header>
					<div class="cards">
						{#if status.id === 'todo' && addOpen}
							<form
								class="inline-add"
								onsubmit={(event) => {
									event.preventDefault();
									void addTask();
								}}
							>
								<input
									type="text"
									bind:value={text}
									placeholder="Task"
									aria-label="Task"
									use:focusNow
									onkeydown={closeOnEscape}
								/>
								<textarea
									bind:value={description}
									placeholder="Description (optional)"
									aria-label="Description"
									rows="2"
									onkeydown={closeOnEscape}
								></textarea>
								<div class="add-row">
									<label class="due-toggle">
										<input type="checkbox" bind:checked={dueOn} />
										<span>Due date</span>
									</label>
									{#if dueOn}
										<input
											type="date"
											bind:value={due}
											aria-label="Due date"
											transition:fade={{ duration: 120 }}
										/>
									{/if}
									<button class="primary" type="submit" disabled={!text.trim() || adding}>
										{adding ? '…' : 'Add'}
									</button>
								</div>
							</form>
						{/if}
						{#each previewColumn(status.id) as task, index (task.id)}
							<article
								animate:flip={{ duration: 150 }}
								class:dragging={dragging === task.id}
								class:menu-open={moveOpen === task.id}
								draggable="true"
								data-task-id={task.id}
								data-status={status.id}
								data-index={index}
								use:touchDrag={touchDragFor(task)}
								ondragstart={(event) => startDrag(event, task)}
								ondragend={endDrag}
								ondrop={(event) => {
									event.preventDefault();
									event.stopPropagation();
									void drop(status.id);
								}}
							>
								<div class="card-top">
									<button
										class="status-button"
										class:complete={task.status === 'done'}
										title={task.status === 'done' ? 'Mark as To do' : 'Mark complete'}
										aria-label={task.status === 'done' ? 'Mark as To do' : 'Mark complete'}
										onclick={() => setStatus(task, task.status === 'done' ? 'todo' : 'done')}
									>
										{task.status === 'done' ? '✓' : task.status === 'in_progress' ? '◐' : '○'}
									</button>
									<!-- stopPropagation: the window listener below closes the
									     menu on any click that escapes a card's own controls. -->
									<button
										class="task-name"
										ondblclick={() => editTask(task)}
										onclick={(e) => {
											e.stopPropagation();
											moveOpen = moveOpen === task.id ? null : task.id;
										}}
									>
										{task.text}
									</button>
									<button
										class="more"
										aria-label="Task actions"
										onclick={(e) => {
											e.stopPropagation();
											moveOpen = moveOpen === task.id ? null : task.id;
										}}
									>
										•••
									</button>
								</div>
								{#if task.description}
									<p class="card-desc">{task.description}</p>
								{/if}
								{#if task.due || task.created_at || task.started_at || task.completed_at}
									<div class="dates">
										{#if task.due}<span class:overdue={task.status !== 'done' && task.due < new Date().toISOString().slice(0, 10)}>Due {task.due}</span>{/if}
										{#if task.started_at}<span>Started {dateLabel(task.started_at)}</span>{/if}
										{#if task.completed_at}<span>Done {dateLabel(task.completed_at)}</span>{/if}
									</div>
								{/if}
								<a class="source" href="/note/{encodePath(task.path)}">{task.path}</a>
								{#if moveOpen === task.id}
									<div class="task-menu">
										<strong>Move to</strong>
										{#each statuses as target (target.id)}
											<button class:active={task.status === target.id} onclick={() => setStatus(task, target.id)}>
												{target.symbol} {target.label}
											</button>
										{/each}
										<strong>Position</strong>
										<button disabled={index === 0} onclick={() => moveBy(task, -1)}>
											Move up
										</button>
										<button
											disabled={index === column(status.id).length - 1}
											onclick={() => moveBy(task, 1)}
										>
											Move down
										</button>
										<button onclick={() => editTask(task)}>Edit text</button>
										<button onclick={() => editDue(task)}>Edit due date</button>
										{#if task.due}
											<button onclick={() => clearDue(task)}>Remove due date</button>
										{/if}
										<button class="danger-text" onclick={() => removeTask(task)}>Delete task</button>
									</div>
								{/if}
							</article>
						{:else}
							<p class="empty-column">No {status.label.toLowerCase()} tasks.</p>
						{/each}
					</div>
				</section>
			{/each}
		</div>
	{/if}
	{/if}
</div>

<style>
	.tasks-page {
		flex: 1;
		overflow: auto;
		padding: 22px 24px 60px;
	}
	.tasks-page > header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 16px;
		max-width: 1280px;
		margin: 0 auto 15px;
	}
	h1 {
		margin: 0 0 3px;
		font-size: 21px;
	}
	.col-meta {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.col-add {
		width: 22px;
		height: 22px;
		padding: 0;
		border: none;
		border-radius: 5px;
		color: var(--text-muted);
		font-size: 15px;
		line-height: 1;
	}
	.col-add:not(:disabled):hover {
		color: var(--accent);
		background: var(--bg-hover);
	}
	.inline-add {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin-bottom: 8px;
		padding: 8px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg);
	}
	.inline-add textarea {
		resize: vertical;
		min-height: 46px;
		font-size: 13px;
	}
	.add-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.due-toggle {
		display: flex;
		align-items: center;
		gap: 5px;
		color: var(--text-muted);
		font-size: 12.5px;
		white-space: nowrap;
		cursor: pointer;
	}
	.add-row input[type='date'] {
		flex: 1;
		min-width: 0;
		padding: 4px 8px;
		font-size: 12.5px;
	}
	.add-row button {
		margin-left: auto;
		flex-shrink: 0;
	}
	.toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		max-width: 1280px;
		margin: 0 auto 12px;
	}
	/* The board switcher: Global plus every folder that owns a board. */
	.board-jump {
		width: min(280px, 100%);
		font-family: inherit;
		font-size: 14px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 6px 10px;
	}
	.toolbar label {
		display: flex;
		align-items: center;
		gap: 6px;
		color: var(--text-muted);
		font-size: 12px;
	}
	.board {
		display: grid;
		grid-template-columns: repeat(3, minmax(240px, 1fr));
		gap: 12px;
		max-width: 1280px;
		margin: 0 auto;
	}
	.board > section {
		min-height: 420px;
		padding: 9px;
		border: 1px solid var(--border);
		border-radius: 11px;
		background: color-mix(in srgb, var(--bg-panel) 70%, transparent);
		transition: border-color var(--motion-fast), background-color var(--motion-fast);
	}
	.board > section.drag-over {
		border-color: var(--accent);
		background: var(--accent-soft);
	}
	.board section > header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 2px 3px 9px;
	}
	h2 {
		margin: 0;
		font-size: 13px;
	}
	/* Scoped to .count so the flex wrapper around it isn't also drawn as a
	   pill. Not selectable text — it's a readout, so keep the arrow cursor. */
	.board section > header .count {
		min-width: 23px;
		padding: 2px 6px;
		border-radius: 10px;
		background: var(--bg-hover);
		color: var(--text-muted);
		font-size: 11px;
		text-align: center;
		cursor: default;
		user-select: none;
	}
	.cards {
		display: flex;
		flex-direction: column;
		gap: 7px;
	}
	article {
		position: relative;
		padding: 10px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--bg-panel);
		box-shadow: 0 3px 10px color-mix(in srgb, var(--text) 4%, transparent);
		cursor: grab;
		transition: opacity var(--motion-fast), border-color var(--motion-fast);
	}
	article:hover {
		border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
	}
	/* The hover transform makes a card its own stacking context, which trapped
	   its open menu inside it — the cards after it in the column then painted
	   over the menu. Lift the whole card while its menu is open so the menu
	   clears every sibling regardless of what is hovered. */
	article.menu-open {
		z-index: 20;
	}
	article.dragging {
		opacity: 0.45;
		transform: scale(0.98);
	}
	/* Where the card will land. Sits in the column's 7px gap, so it marks the
	   seam without shifting anything below it. */
	.card-top {
		display: flex;
		align-items: flex-start;
		gap: 7px;
	}
	.status-button {
		flex: 0 0 25px;
		width: 25px;
		height: 25px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 50%;
		color: var(--text-muted);
	}
	.status-button.complete {
		border-color: var(--accent);
		background: var(--accent);
		color: #fff;
	}
	.task-name {
		flex: 1;
		padding: 2px 0;
		border: 0;
		color: var(--text);
		font-size: 13px;
		font-weight: 600;
		line-height: 1.4;
		text-align: left;
	}
	.more {
		padding: 1px 3px;
		border: 0;
		color: var(--text-muted);
		letter-spacing: 1px;
	}
	.card-desc {
		margin: 5px 0 0 32px;
		color: var(--text-muted);
		font-size: 12.5px;
		line-height: 1.4;
		white-space: pre-wrap;
	}
	.dates {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin: 7px 0 4px 32px;
	}
	.dates span {
		padding: 2px 5px;
		border-radius: 4px;
		background: var(--bg-hover);
		color: var(--text-muted);
		font-size: 10px;
	}
	.dates .overdue {
		color: var(--danger);
	}
	.source {
		display: block;
		margin: 5px 0 0 32px;
		overflow: hidden;
		color: var(--text-muted);
		font: 10px var(--font-mono);
		text-decoration: none;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.source:hover {
		color: var(--accent);
	}
	.task-menu {
		position: absolute;
		z-index: 8;
		top: 36px;
		right: 8px;
		width: 160px;
		padding: 5px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-panel);
		box-shadow: 0 12px 32px color-mix(in srgb, #000 25%, transparent);
	}
	.task-menu strong {
		display: block;
		padding: 4px 7px;
		color: var(--text-muted);
		font-size: 10px;
		text-transform: uppercase;
	}
	.task-menu button {
		width: 100%;
		padding: 6px 7px;
		border: 0;
		text-align: left;
	}
	.task-menu button.active {
		background: var(--bg-active);
		color: var(--accent);
	}
	/* Already at the top or the bottom of its column. */
	.task-menu button:disabled {
		color: var(--text-muted);
		opacity: 0.45;
		cursor: default;
		background: none;
	}
	.danger-text {
		color: var(--danger);
	}
	.board-scope {
		margin: 2px 0 0;
		color: var(--text-muted);
		font: 11px var(--font-mono);
	}
	.empty-column,
	.state {
		padding: 28px 10px;
		color: var(--text-muted);
		font-size: 12px;
		text-align: center;
	}
	.mobile-tabs {
		display: none;
	}
	@media (max-width: 760px) {
		.tasks-page {
			padding: 12px 10px 40px;
		}
		.toolbar {
			align-items: stretch;
			flex-direction: column;
		}
		.mobile-tabs {
			display: grid;
			grid-template-columns: repeat(3, 1fr);
			gap: 3px;
			max-width: 1280px;
			margin: 0 auto 9px;
			padding: 3px;
			border-radius: 8px;
			background: var(--bg-panel);
		}
		.mobile-tabs button {
			padding: 7px 3px;
			border: 0;
			font-size: 11px;
		}
		.mobile-tabs button.active {
			background: var(--bg-active);
			color: var(--accent);
		}
		.mobile-tabs span {
			margin-left: 2px;
			color: var(--text-muted);
		}
		.board {
			display: block;
		}
		.board > section.hidden-mobile {
			display: none;
		}
		.board > section {
			min-height: 300px;
		}
		article {
			cursor: default;
			touch-action: pan-y;
		}
	}
</style>
