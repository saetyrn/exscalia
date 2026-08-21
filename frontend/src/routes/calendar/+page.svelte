<script lang="ts">
	import { untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from 'svelte/transition';
	import { api, encodePath, type CalTask, type DayView, type MonthView, type TaskStatus } from '$lib/api';
	import { cachedMonth, calendarMonths, loadMonth, monthKey, watchCalendarLive } from '$lib/calendarview.svelte';
	import { notify } from '$lib/ui.svelte';
	import { refreshNotes } from '$lib/stores.svelte';

	let { data } = $props();

	const today = new Date();
	let year = $state(today.getFullYear());
	let month = $state(today.getMonth() + 1); // 1-based
	// Initial-only capture is deliberate: this route remounts per navigation.
	const initialView = untrack(() => data.view);
	let view: MonthView | null = $state(initialView);
	let selected: DayView | null = $state(null);
	// The route load() covers the initial month; recover here only if it failed.
	if (initialView === null) void load();

	const monthName = $derived(
		new Date(year, month - 1, 1).toLocaleString('en', { month: 'long', year: 'numeric' })
	);

	async function load() {
		view = await loadMonth(year, month);
	}

	// Whatever the shared cache holds for the month on screen wins — that covers
	// the revalidation the loader started behind a cached render, and the
	// refresh a note change triggers, without either blanking the grid.
	$effect(() => {
		const fresh = calendarMonths.byMonth[monthKey(year, month)];
		if (fresh && fresh !== untrack(() => view)) view = fresh;
	});

	$effect(() => {
		watchCalendarLive();
	});

	function shift(delta: number) {
		const d = new Date(year, month - 1 + delta, 1);
		year = d.getFullYear();
		month = d.getMonth() + 1;
		// A month visited before is already in hand; only a new one waits.
		view = cachedMonth(year, month);
		void load();
		selected = null;
	}

	function iso(day: number): string {
		return `${year.toString().padStart(4, '0')}-${month.toString().padStart(2, '0')}-${day
			.toString()
			.padStart(2, '0')}`;
	}

	// Build the grid: array of weeks, each 7 cells (null = out of month).
	let weeks = $derived.by(() => {
		const first = new Date(year, month - 1, 1);
		const daysInMonth = new Date(year, month, 0).getDate();
		const lead = first.getDay(); // 0 = Sunday
		const cells: (number | null)[] = [
			...Array(lead).fill(null),
			...Array.from({ length: daysInMonth }, (_, i) => i + 1)
		];
		while (cells.length % 7 !== 0) cells.push(null);
		const out: (number | null)[][] = [];
		for (let i = 0; i < cells.length; i += 7) out.push(cells.slice(i, i + 7));
		return out;
	});

	/** Double-click target: straight into the day's daily note, creating it
	 *  the same way the Daily link does. */
	async function openDailyFor(date: string) {
		try {
			const opened = await api.openDaily(date);
			await goto(`/note/${encodePath(opened.path)}`);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function selectDay(day: number) {
		selected = await api.dayView(iso(day));
	}

	async function openDaily(date: string) {
		const { path } = await api.openDaily(date);
		await refreshNotes();
		goto(`/note/${encodePath(path)}`);
	}

	async function setStatus(task: CalTask, status: TaskStatus) {
		if (task.tombstone) return;
		try {
			await api.updateTask(task.id, { status });
			await load();
			if (selected) selected = await api.dayView(selected.date);
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	const eventLabels: Record<string, string> = {
		created: 'Created',
		started: 'Started',
		completed: 'Completed',
		due: 'Due'
	};

	const todayIso = `${today.getFullYear()}-${(today.getMonth() + 1)
		.toString()
		.padStart(2, '0')}-${today.getDate().toString().padStart(2, '0')}`;
</script>

<div class="cal">
	<header>
		<h1>{monthName}</h1>
		<div class="nav">
			<button onclick={() => shift(-1)}>←</button>
			<button
				onclick={() => {
					year = today.getFullYear();
					month = today.getMonth() + 1;
					void load();
				}}
			>
				Today
			</button>
			<button onclick={() => shift(1)}>→</button>
		</div>
	</header>

	{#key `${year}-${month}`}
		<div
			class="grid"
			in:fly={{ x: 10, duration: 190, easing: cubicOut }}
			out:fade={{ duration: 80 }}
		>
			{#each ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as d (d)}
				<div class="dow">{d}</div>
			{/each}
			{#each weeks as week, wi (wi)}
				{#each week as day, di (di)}
					{#if day === null}
						<div class="cell empty"></div>
					{:else}
						{@const dateStr = iso(day)}
						<button
							type="button"
							class="cell"
							class:today={dateStr === todayIso}
							class:selected={selected?.date === dateStr}
							onclick={() => selectDay(day)}
							ondblclick={() => void openDailyFor(dateStr)}
							title="Double-click opens the day's daily note"
						>
							<span class="daynum">
								{day}
								{#if view?.daily_notes.includes(dateStr)}
									<span class="daily-dot" title="Daily note exists"></span>
								{/if}
							</span>
							{#if view?.activity[dateStr]}
								<span class="activity">{view.activity[dateStr]} note{view.activity[dateStr] === 1 ? '' : 's'}</span>
							{/if}
							{#if view?.task_activity[dateStr]}
								<span class="task-count">
									{view.task_activity[dateStr]} task event{view.task_activity[dateStr] === 1 ? '' : 's'}
								</span>
							{/if}
							{#if view?.event_activity?.[dateStr]}
								<span class="event-count">
									{view.event_activity[dateStr]} event{view.event_activity[dateStr] === 1 ? '' : 's'}
								</span>
							{/if}
							{#if view?.activity_hours?.[dateStr]}
								<span class="hours-count">{view.activity_hours[dateStr]}h logged</span>
							{/if}
						</button>
					{/if}
				{/each}
			{/each}
		</div>
	{/key}

	{#if selected}
		<section class="day-panel" in:fly={{ y: 8, duration: 190, easing: cubicOut }}>
			<header class="day-head">
				<h2>{selected.date}</h2>
				<button class="primary" onclick={() => openDaily(selected!.date)}>
					{selected.daily_note ? 'Open daily note' : 'Create daily note'}
				</button>
			</header>

			{#if selected.task_events.length > 0}
				<h3>Task activity</h3>
				<ul class="tasklist">
					{#each selected.task_events as event (event.type + event.task.id)}
						<li>
							<span class="event-kind kind-{event.type}">{eventLabels[event.type]}</span>
							<span class:done={event.task.done}>{event.task.text}</span>
							{#if !event.task.tombstone}
								<select
									aria-label="Task status"
									value={event.task.status}
									onchange={(e) => setStatus(event.task, e.currentTarget.value as TaskStatus)}
								>
									<option value="todo">To do</option>
									<option value="in_progress">In progress</option>
									<option value="done">Done</option>
								</select>
								<a class="src" href="/note/{encodePath(event.task.path)}">{event.task.path}</a>
							{:else}
								<span class="src">deleted task</span>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}

			{#if selected.events.length > 0}
				<h3>Events</h3>
				<ul class="eventlist">
					{#each selected.events as event (event.event_id + event.occurrence_start)}
						<li>
							<div>
								<strong>{event.title}</strong>
								<span>
									{event.all_day
										? 'All day'
										: new Date(event.occurrence_start).toLocaleTimeString([], {
												hour: 'numeric',
												minute: '2-digit'
											})}
								</span>
							</div>
							{#if event.note_path}
								<a class="src" href="/note/{encodePath(event.note_path)}">{event.note_path}</a>
							{/if}
						</li>
					{/each}
				</ul>
			{/if}

			{#if selected.activity_hours}
				<p class="logged-hours"><strong>{selected.activity_hours}h</strong> logged in the Activity tab.</p>
			{/if}

			<h3>Notes created or modified</h3>
			{#if selected.notes.length === 0}
				<p class="muted">Nothing touched on this date.</p>
			{:else}
				<ul class="notelist">
					{#each selected.notes as n (n.path)}
						<li>
							<a href="/note/{encodePath(n.path)}">{n.title || n.path}</a>
							<span class="kind">{n.created_today ? 'created' : 'modified'}</span>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	{/if}
</div>

<style>
	.cal {
		flex: 1;
		overflow-y: auto;
		max-width: 1020px;
		width: 100%;
		margin: 0 auto;
		padding: 22px 28px 60px;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 14px;
	}
	h1 {
		font-size: 19px;
		margin: 0;
	}
	.nav {
		display: flex;
		gap: 4px;
	}
	.nav button {
		border: 1px solid var(--border);
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(7, 1fr);
		gap: 4px;
	}
	.dow {
		text-align: center;
		font-size: 11px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.5px;
		color: var(--text-muted);
		padding: 4px 0;
	}
	.cell {
		min-height: 86px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-panel);
		padding: 6px 8px;
		text-align: left;
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 2px;
		font-size: 12px;
		overflow: hidden;
		transition:
			background-color var(--motion-base) var(--ease-out),
			border-color var(--motion-base) var(--ease-out),
			box-shadow var(--motion-base) var(--ease-out);
	}
	.cell:not(:disabled):hover {
		border-color: var(--accent);
		background: var(--bg-hover);
		box-shadow: 0 5px 14px color-mix(in srgb, var(--text) 8%, transparent);
	}
	.cell:not(:disabled):active {
		transform: translateY(0) scale(0.985);
	}
	.cell.empty {
		background: transparent;
		border-color: transparent;
		pointer-events: none;
	}
	.cell.today {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.cell.selected {
		background: var(--accent-soft);
		box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 50%, transparent);
	}
	.daynum {
		font-weight: 700;
		font-size: 13px;
		display: flex;
		align-items: center;
		gap: 5px;
	}
	.daily-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--accent);
		display: inline-block;
		box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 35%, transparent);
		animation: daily-breathe 2.4s ease-in-out infinite;
	}
	@keyframes daily-breathe {
		50% {
			box-shadow: 0 0 0 4px transparent;
		}
	}
	.activity {
		color: var(--text-muted);
		font-size: 11px;
	}
	.task-count {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		background: var(--accent-soft);
		color: var(--accent);
		border-radius: 4px;
		padding: 1px 5px;
	}
	.event-count,
	.hours-count {
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		border-radius: 4px;
		padding: 1px 5px;
	}
	.event-count {
		background: color-mix(in srgb, var(--accent) 12%, var(--bg-panel));
		color: var(--text);
	}
	.hours-count {
		color: var(--text-muted);
	}
	.eventlist {
		list-style: none;
		padding: 0;
		margin: 6px 0 18px;
		display: grid;
		gap: 6px;
	}
	.eventlist li {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		padding: 8px 10px;
		border-radius: 8px;
		background: var(--bg-panel);
		border: 1px solid var(--border);
	}
	.eventlist li > div {
		display: grid;
		gap: 2px;
	}
	.eventlist span,
	.logged-hours {
		color: var(--text-muted);
		font-size: 12px;
	}
	.day-panel {
		margin-top: 20px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg-panel);
		padding: 14px 18px;
		box-shadow: 0 8px 24px color-mix(in srgb, var(--text) 5%, transparent);
	}
	.day-head {
		margin-bottom: 4px;
	}
	.day-panel h2 {
		font-size: 16px;
		margin: 0;
	}
	.day-panel h3 {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.5px;
		color: var(--text-muted);
		margin: 14px 0 6px;
	}
	.tasklist,
	.notelist {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.tasklist li {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 3px 0;
		font-size: 13.5px;
		border-radius: 6px;
		transition:
			background-color var(--motion-base) var(--ease-out),
			padding-left var(--motion-base) var(--ease-out);
	}
	.tasklist li > span:not(.event-kind):not(.src) {
		flex: 1;
	}
	.tasklist li > span.done {
		color: var(--text-muted);
		text-decoration: line-through;
	}
	.tasklist select {
		width: auto;
		padding: 3px 22px 3px 7px;
		font-size: 11px;
	}
	.event-kind {
		min-width: 66px;
		padding: 2px 5px;
		border-radius: 4px;
		background: var(--bg-hover);
		color: var(--text-muted);
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
	}
	.kind-started {
		color: var(--accent);
	}
	.kind-completed {
		color: var(--added);
	}
	.kind-due {
		color: var(--danger);
	}
	.tasklist li:hover {
		background: var(--bg-hover);
		padding-left: 5px;
	}
	.src {
		margin-left: auto;
		font-size: 12px;
		font-family: var(--font-mono);
		color: var(--text-muted);
		text-decoration: none;
	}
	.src:hover {
		color: var(--accent);
	}
	.notelist li {
		padding: 3px 0;
		font-size: 13.5px;
	}
	.notelist a {
		color: var(--accent);
		text-decoration: none;
		font-weight: 600;
	}
	.kind {
		color: var(--text-muted);
		font-size: 12px;
		margin-left: 8px;
	}
	.muted {
		color: var(--text-muted);
		font-size: 13px;
	}

	@media (max-width: 760px) {
		.cal {
			padding: 12px 10px 24px;
		}
		.cal > header {
			align-items: center;
			gap: 8px;
			margin-bottom: 9px;
		}
		h1 {
			font-size: 17px;
		}
		.nav {
			gap: 2px;
		}
		.nav button {
			min-width: 40px;
			padding-inline: 8px;
		}
		.grid {
			gap: 2px;
		}
		.dow {
			font-size: 9px;
			letter-spacing: 0.2px;
			padding: 2px 0;
		}
		.cell {
			min-height: 50px;
			padding: 5px;
			gap: 0;
			border-radius: 6px;
			align-items: center;
		}
		.cell:not(:disabled):hover,
		.cell:not(:disabled):active {
			transform: none;
		}
		.daynum {
			font-size: 12px;
			gap: 3px;
		}
		.daily-dot {
			width: 5px;
			height: 5px;
		}
		.activity,
		.task-count {
			display: none;
		}
		.cell:has(.task-count)::after {
			content: '';
			width: 5px;
			height: 5px;
			margin-top: 6px;
			border-radius: 50%;
			background: var(--text-muted);
		}
		.day-panel {
			margin-top: 14px;
			padding: 12px;
			border-radius: 8px;
		}
		.day-head {
			align-items: stretch;
			flex-direction: column;
			gap: 9px;
		}
		.day-head .primary {
			width: 100%;
		}
		.tasklist li {
			align-items: flex-start;
			flex-wrap: wrap;
			gap: 7px;
			padding: 7px 2px;
		}
		.tasklist li > span:not(.event-kind):not(.src) {
			flex: 1;
			min-width: 0;
		}
		.src {
			width: calc(100% - 28px);
			margin: 0 0 0 28px;
			overflow: hidden;
			text-overflow: ellipsis;
			white-space: nowrap;
		}
		.notelist li {
			padding: 7px 0;
		}
	}
</style>
