<script lang="ts">
	// The morning briefing body: OMNIA's greeting, today's events + in-progress
	// tasks, and the coming week grouped by day. Rendered inside the desktop
	// banner and the mobile bottom sheet alike — it owns no chrome of its own
	// beyond a header, so both hosts can wrap it however they need.
	import { goto } from '$app/navigation';
	import { encodePath, type CalTask, type CalendarEventOccurrence } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import ChatPanel from '$lib/ChatPanel.svelte';
	import { overseerChat } from '$lib/stores.svelte';
	import {
		morning,
		dismissMorning,
		activateMorningThread,
		localToday,
		confirmObservation,
		dismissObservation
	} from '$lib/morning.svelte';

	// Inside the mobile bottom sheet the host already draws a header (the date as
	// its title, an X to dismiss), so the component drops its own date + dismiss
	// and keeps just the greeting line.
	let { embedded = false }: { embedded?: boolean } = $props();

	let replyOpen = $state(false);

	function toggleReply() {
		replyOpen = !replyOpen;
		// Only claim the daily thread when the user actually means to reply.
		if (replyOpen) activateMorningThread();
	}

	const todayIso = localToday();
	const todayTitle = new Date().toLocaleDateString(undefined, {
		weekday: 'long',
		month: 'long',
		day: 'numeric'
	});

	function weekdayShort(iso: string): string {
		return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { weekday: 'short' });
	}
	function dayNumOf(iso: string): string {
		return String(new Date(`${iso}T00:00:00`).getDate());
	}
	function eventTime(ev: CalendarEventOccurrence): string {
		if (ev.all_day) return 'All day';
		try {
			return new Date(ev.occurrence_start).toLocaleTimeString(undefined, {
				hour: 'numeric',
				minute: '2-digit'
			});
		} catch {
			return '';
		}
	}

	// The staged observation's two answers. Both read the id off the store at
	// click time rather than closing over it — the row is gone by the time the
	// request settles.
	function confirmObs() {
		const obs = morning.observation;
		if (obs) void confirmObservation(obs.id);
	}
	function dismissObs() {
		const obs = morning.observation;
		if (obs) void dismissObservation(obs.id);
	}
	// What confirming a habit observation stores (the server's own test: key
	// and value both, or it keeps the sentence as a memory instead). The
	// sentence only describes the write, so the pair is shown under it.
	const obsHabit = $derived.by(() => {
		const obs = morning.observation;
		return obs?.kind === 'habit' && obs.habit_key && obs.habit_value
			? `${obs.habit_key} → ${obs.habit_value}`
			: '';
	});

	function openTask(task: CalTask) {
		dismissMorning();
		void goto(`/note/${encodePath(task.path)}`);
	}
	function openEvent(ev: CalendarEventOccurrence) {
		dismissMorning();
		void goto(ev.note_path ? `/note/${encodePath(ev.note_path)}` : '/calendar');
	}
	function openBackup() {
		dismissMorning();
		void goto('/settings#vault');
	}

	// The off-site backup has been failing (or silent) for a while. One muted
	// line, present only while it stays that way.
	let backupLine = $derived.by(() => {
		const b = morning.briefing;
		if (!b?.backup_stale) return '';
		if (!b.backup_failing_since) return 'Vault backup out of date';
		const since = new Date(b.backup_failing_since).toLocaleDateString(undefined, {
			month: 'short',
			day: 'numeric'
		});
		return `Vault backup failing since ${since}`;
	});

	let todayEvents = $derived(morning.briefing?.today_events ?? []);
	let inProgress = $derived(morning.briefing?.in_progress ?? []);
	// Only the days that actually carry something — a row of empty days buries
	// the one that matters. The whole-week-clear case gets a single line below.
	let weekDays = $derived(
		(morning.briefing?.week ?? []).filter((d) => d.tasks.length || d.events.length)
	);
	let emptyToday = $derived(todayEvents.length === 0 && inProgress.length === 0);

	// Yesterday at a glance, as one muted line. Only counts that happened are
	// listed — a "0 tasks done" would be a fact nobody asked for — and a day
	// where nothing happened has no line at all.
	let statParts = $derived.by(() => {
		const s = morning.briefing?.stats;
		if (!s) return [];
		const parts: string[] = [];
		const n = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`;
		// Edits only stand in for creations; both would double-count the same day.
		if (s.notes_created) parts.push(n(s.notes_created, 'note'));
		else if (s.notes_modified) parts.push(`${s.notes_modified} edited`);
		if (s.tasks_completed) parts.push(`${n(s.tasks_completed, 'task')} done`);
		if (s.flashcards_due) parts.push(`${n(s.flashcards_due, 'card')} due`);
		// Rounded before the test, so six logged minutes never reads "0.0h".
		const hours = Math.round((s.activity_hours_yesterday ?? 0) * 10) / 10;
		if (hours) parts.push(`${hours.toFixed(1)}h logged`);
		if (s.pending_changes) parts.push(`${s.pending_changes} staged`);
		if (s.media_in_progress) parts.push(`${s.media_in_progress} in progress`);
		return parts;
	});
</script>

<div class="briefing">
	{#if !embedded || morning.message}
		<header class="head">
			<div class="titles">
				{#if !embedded}
					<span class="date">{todayTitle}</span>
				{/if}
				{#if morning.message}
					<button
						class="greeting"
						title={replyOpen ? 'Close the conversation' : 'Reply to OMNIA'}
						aria-expanded={replyOpen}
						onclick={toggleReply}>{morning.message}</button
					>
				{/if}
			</div>
			{#if !embedded}
				<button class="dismiss" onclick={dismissMorning} title="Dismiss" aria-label="Dismiss">
					<Glyph name="x" />
				</button>
			{/if}
		</header>
	{/if}

	{#if replyOpen}
		<div class="reply">
			<ChatPanel chat={overseerChat} placeholder="Reply to OMNIA…" />
		</div>
	{/if}

	<div class="sections">
		{#if backupLine}
			<button class="backup" onclick={openBackup} title="Backup settings">{backupLine}</button>
		{/if}
		{#if statParts.length > 0}
			<p class="stats">{statParts.join(' · ')}</p>
		{/if}
		{#if morning.observation}
			<div class="observation">
				<span class="obs-text">
					{morning.observation.text}
					{#if obsHabit}<span class="obs-habit">{obsHabit}</span>{/if}
				</span>
				<button class="obs-act" onclick={confirmObs} title="Confirm" aria-label="Confirm">
					<Glyph name="check" />
				</button>
				<button class="obs-act" onclick={dismissObs} title="Dismiss" aria-label="Dismiss">
					<Glyph name="x" />
				</button>
			</div>
		{/if}
		<section>
			<h3>Today</h3>
			<div class="day">
				<div class="chip today" title={todayTitle}>
					<span class="wd">{weekdayShort(todayIso)}</span>
					<span class="dn">{dayNumOf(todayIso)}</span>
				</div>
				{#if emptyToday}
					<p class="empty">Nothing scheduled.</p>
				{:else}
					<ul class="rows">
						{#each todayEvents as ev (ev.id)}
							<li>
								<button class="row" onclick={() => openEvent(ev)}>
									<Glyph name="calendar" />
									<span class="row-text">{ev.title}</span>
									<span class="row-meta">{eventTime(ev)}</span>
								</button>
							</li>
						{/each}
						{#each inProgress as task (task.id)}
							<li>
								<button class="row" onclick={() => openTask(task)}>
									<span class="dot" title="In progress"></span>
									<span class="row-text">{task.text}</span>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</div>
		</section>

		<section>
			<h3>The week ahead</h3>
			{#if weekDays.length === 0}
				<p class="empty">Clear week ahead.</p>
			{:else}
				<ul class="days">
					{#each weekDays as day (day.date)}
						<li class="day">
							<div class="chip">
								<span class="wd">{weekdayShort(day.date)}</span>
								<span class="dn">{dayNumOf(day.date)}</span>
							</div>
							<ul class="rows">
								{#each day.events as ev (ev.id)}
									<li>
										<button class="row" onclick={() => openEvent(ev)}>
											<Glyph name="calendar" />
											<span class="row-text">{ev.title}</span>
											<span class="row-meta">{eventTime(ev)}</span>
										</button>
									</li>
								{/each}
								{#each day.tasks as task (task.id)}
									<li>
										<button class="row" onclick={() => openTask(task)}>
											<Glyph name="task" />
											<span class="row-text">{task.text}</span>
										</button>
									</li>
								{/each}
							</ul>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
	</div>
</div>

<style>
	.briefing {
		display: flex;
		flex-direction: column;
		min-height: 0;
		gap: 10px;
	}
	.head {
		display: flex;
		align-items: flex-start;
		gap: 10px;
	}
	.titles {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.date {
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.02em;
		color: var(--text-muted);
	}
	.greeting {
		margin: 0;
		padding: 0;
		border: 0;
		background: none;
		font: inherit;
		font-size: 14px;
		text-align: left;
		color: var(--text);
		cursor: pointer;
	}
	.greeting:hover {
		color: var(--accent);
	}
	.dismiss {
		flex-shrink: 0;
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		padding: 0;
		border: 0;
		border-radius: var(--radius);
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.dismiss:hover {
		background: var(--bg-hover);
	}
	.dismiss :global(svg) {
		width: 16px;
		height: 16px;
	}
	.reply {
		height: min(420px, 50vh);
		display: flex;
		flex-direction: column;
		min-height: 0;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg);
		overflow: hidden;
		padding: 0 10px 10px;
	}
	.sections {
		display: flex;
		flex-direction: column;
		gap: 14px;
		min-height: 0;
		overflow-y: auto;
	}
	section {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	h3 {
		margin: 0;
		font-size: 11px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
	}
	.empty {
		margin: 0;
		font-size: 13px;
		color: var(--text-muted);
	}
	/* One line of counts above Today — same muted weight as a section label,
	   without the label's uppercase shout. */
	.stats {
		margin: 0;
		font-size: 12px;
		color: var(--text-muted);
	}
	/* A failing off-site backup: the counts' muted line, clickable. */
	.backup {
		align-self: flex-start;
		margin: 0;
		padding: 0;
		border: 0;
		background: none;
		font: inherit;
		font-size: 12px;
		text-align: left;
		color: var(--text-muted);
		cursor: pointer;
		transition: color var(--motion-fast);
	}
	.backup:hover {
		color: var(--text);
	}
	/* Something OMNIA thinks it has noticed, waiting on a yes or a no. Same
	   muted weight as the counts above it — it is a question, not news — and
	   the two answers are bare glyphs, borderless like the media chrome's. */
	.observation {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.obs-text {
		flex: 1;
		min-width: 0;
	}
	.obs-habit {
		display: block;
		overflow-wrap: anywhere;
	}
	.obs-act {
		flex-shrink: 0;
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		padding: 0;
		border: 0;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		transition: color var(--motion-fast);
	}
	.obs-act:hover {
		color: var(--text);
	}
	.obs-act :global(svg) {
		width: 15px;
		height: 15px;
	}
	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}
	.row {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px;
		border: 0;
		border-radius: var(--radius);
		background: none;
		font: inherit;
		font-size: 13.5px;
		text-align: left;
		color: var(--text);
		cursor: pointer;
	}
	.row:hover {
		background: var(--bg-hover);
	}
	.row :global(svg) {
		width: 15px;
		height: 15px;
		flex-shrink: 0;
		color: var(--text-muted);
	}
	.row-text {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		white-space: nowrap;
		text-overflow: ellipsis;
	}
	.row-meta {
		flex-shrink: 0;
		font-size: 12px;
		color: var(--text-muted);
	}
	.dot {
		flex-shrink: 0;
		width: 8px;
		height: 8px;
		margin: 0 3px;
		border-radius: 50%;
		background: var(--accent);
	}
	.days {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.day {
		display: flex;
		gap: 10px;
		align-items: flex-start;
	}
	.chip {
		flex-shrink: 0;
		width: 44px;
		display: flex;
		flex-direction: column;
		align-items: center;
		padding: 4px 0;
		border-radius: var(--radius);
		background: var(--bg-panel);
	}
	.chip .wd {
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.chip .dn {
		font-size: 16px;
		font-weight: 600;
		color: var(--text);
	}
	/* The current date, marked on the briefing's calendar. */
	.chip.today {
		background: var(--accent-soft);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.chip.today .wd,
	.chip.today .dn {
		color: var(--accent);
	}
	.day .rows {
		flex: 1;
		min-width: 0;
		padding-top: 1px;
	}
	.day .empty {
		flex: 1;
		align-self: center;
	}

	/* Phone: the briefing is a bottom sheet, not the desktop banner, so it
	   takes the shared mobile scale — labels to the app-wide section spec,
	   rows to a real touch target. Scoped rather than applied to the base
	   above because both hosts render this same component, and the banner's
	   tighter proportions are correct where a pointer is doing the aiming.
	   The counts and the observation line stay 12px muted on purpose: they are
	   read, not tapped. */
	@media (max-width: 760px) {
		h3 {
			font-size: var(--m-label);
			font-weight: 600;
			letter-spacing: 0.5px;
		}
		.row {
			min-height: 44px;
			padding: 6px 10px;
			font-size: 14px;
		}
		.obs-act {
			width: 44px;
			height: 44px;
		}
		.backup {
			min-height: 40px;
		}
	}
</style>
