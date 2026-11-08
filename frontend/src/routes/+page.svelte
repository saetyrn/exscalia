<script lang="ts">
	// The hub: the first page of each day (the layout lands here on the first
	// open of a new local date) and where EXSCALIA returns to from Settings.
	// Your day on the left, where to go in the middle, OMNIA and the week on
	// the right. On a phone the three stack into one scrolling column.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { api, encodePath, type CalTask } from '$lib/api';
	import { noteHref } from '$lib/recipes/links';
	import { localToday } from '$lib/dates';
	import Glyph from '$lib/Glyph.svelte';
	import { numberedPath } from '$lib/notepaths';
	import { recentNotes, refreshNotes, vault } from '$lib/stores.svelte';
	import { notify, openPalette } from '$lib/ui.svelte';
	import {
		morning,
		refreshMorning,
		activateMorningThread,
		confirmObservation,
		dismissObservation
	} from '$lib/morning.svelte';
	import { comingUp, dayLabel, eventOver, eventTime, noteExcerpt, statParts } from '$lib/hub';

	const today = localToday();
	const title = new Date().toLocaleDateString(undefined, {
		weekday: 'long',
		month: 'long',
		day: 'numeric'
	});
	const dailyPath = `daily/${today}.md`;

	// Ticks once a minute so a finished event fades without a reload.
	let now = $state(new Date());

	const name = (path: string) => (path.split('/').pop() ?? path).replace(/\.md$/, '');
	const folderOf = (path: string) => path.split('/').slice(0, -1).join('/');

	// Two pinned notes, then the five most recently opened on this device that
	// are not already up there.
	let pinned = $derived(
		vault.notes
			.filter((n) => n.pinned)
			.slice(0, 2)
			.map((n) => n.path)
	);
	let recent = $derived.by(() => {
		const above = new Set(pinned);
		const out: string[] = [];
		for (const p of recentNotes.paths) {
			if (above.has(p)) continue;
			// A deleted note drops off once the list has loaded.
			if (vault.loaded && !vault.notes.some((n) => n.path === p)) continue;
			out.push(p);
			if (out.length === 5) break;
		}
		return out;
	});

	let dailies = $state<CalTask[]>([]);
	let dailiesDone = $derived(dailies.filter((t) => t.done).length);
	let adding = $state(false);
	let draft = $state('');

	let excerpt = $state<string[]>([]);
	let hasDaily = $derived(vault.notes.some((n) => n.path === dailyPath));

	let briefing = $derived(morning.briefing);
	let todayEvents = $derived(briefing?.today_events ?? []);
	let upcoming = $derived(comingUp(briefing?.week, today));
	let inProgress = $derived(briefing?.in_progress ?? []);
	let stats = $derived(statParts(briefing?.stats));
	let backupLine = $derived.by(() => {
		if (!briefing?.backup_stale) return '';
		if (!briefing.backup_failing_since) return 'Vault backup out of date';
		const since = new Date(briefing.backup_failing_since).toLocaleDateString(undefined, {
			month: 'short',
			day: 'numeric'
		});
		return `Vault backup failing since ${since}`;
	});

	async function loadDailies() {
		try {
			dailies = await api.listDailies();
		} catch {
			// Offline, or a server without the route: the list stays as it was.
		}
	}

	async function loadExcerpt() {
		if (!hasDaily) {
			excerpt = [];
			return;
		}
		try {
			excerpt = noteExcerpt((await api.readNote(dailyPath)).content ?? '');
		} catch {
			excerpt = [];
		}
	}

	onMount(() => {
		if (!morning.briefing) void refreshMorning();
		void loadDailies();
		const tick = setInterval(() => (now = new Date()), 60_000);
		// OMNIA or another device may tick a daily or add one.
		const changed = () => void loadDailies();
		window.addEventListener('exscalia:data-changed', changed);
		return () => {
			clearInterval(tick);
			window.removeEventListener('exscalia:data-changed', changed);
		};
	});

	// The note list arrives after mount on a cold open; read the excerpt once
	// it says today's note exists.
	$effect(() => {
		if (hasDaily) void loadExcerpt();
	});

	async function toggleDaily(task: CalTask) {
		const done = !task.done;
		task.done = done;
		task.status = done ? 'done' : 'todo';
		try {
			await api.updateTask(task.id, { status: done ? 'done' : 'todo' });
		} catch (e) {
			task.done = !done;
			task.status = done ? 'todo' : 'done';
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function addDaily(e: SubmitEvent) {
		e.preventDefault();
		const text = draft.trim();
		if (!text) return;
		try {
			await api.createTask({ text, repeats: 'daily', due: today });
			draft = '';
			await loadDailies();
		} catch (err) {
			notify((err as Error).message, { tone: 'error' });
		}
	}

	let busy = $state(false);
	// Get-or-create today's daily note, then open it — what the sidebar's Daily does.
	async function openToday() {
		if (busy) return;
		busy = true;
		try {
			if (!hasDaily) {
				await api.openDaily(today);
				void refreshNotes();
			}
			await goto(`/note/${encodePath(dailyPath)}`);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	async function newNote() {
		if (busy) return;
		busy = true;
		try {
			const path = numberedPath(vault.notes, 'untitled');
			await api.createNote(path);
			await refreshNotes();
			await goto(`/note/${encodePath(path)}`);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	function replyToOmnia() {
		activateMorningThread();
		void goto('/omnia');
	}

	function confirmObs() {
		const obs = morning.observation;
		if (obs) void confirmObservation(obs.id);
	}
	function dismissObs() {
		const obs = morning.observation;
		if (obs) void dismissObservation(obs.id);
	}
</script>

<div class="hub">
	<header class="head">
		<h1 class="date">{title}</h1>
		{#if stats.length > 0}
			<span class="stats">yesterday {stats.join(' · ')}</span>
		{/if}
		<button type="button" class="daily-btn" onclick={openToday} disabled={busy}>
			<Glyph name="calendar" />
			<span>Go to daily</span>
		</button>
	</header>

	<div class="grid">
		<section class="panel day">
			<div class="label-row">
				<h2 class="label">Dailies · {dailiesDone}/{dailies.length}</h2>
				<button
					type="button"
					class="icon-btn"
					title="Add daily"
					aria-label="Add daily"
					aria-expanded={adding}
					onclick={() => (adding = !adding)}
				>
					<Glyph name="plus" />
				</button>
			</div>
			<div class="progress" aria-hidden="true">
				<i style:width="{dailies.length ? (dailiesDone / dailies.length) * 100 : 0}%"></i>
			</div>
			{#each dailies as task (task.id)}
				<button type="button" class="row check" class:done={task.done} onclick={() => toggleDaily(task)}>
					<span class="box" aria-hidden="true">
						{#if task.done}<Glyph name="check" />{/if}
					</span>
					<span class="text">{task.text}</span>
				</button>
			{/each}
			{#if adding}
				<form class="add" onsubmit={addDaily}>
					<!-- svelte-ignore a11y_autofocus -->
					<input
						bind:value={draft}
						placeholder="Add a daily"
						aria-label="New daily"
						autofocus
						onkeydown={(e) => {
							if (e.key === 'Escape') adding = false;
						}}
					/>
				</form>
			{/if}

			<h2 class="label spaced">Daily note</h2>
			{#if excerpt.length > 0}
				<div class="excerpt">
					{#each excerpt as line, i (i)}<p>{line}</p>{/each}
				</div>
			{/if}
			<button type="button" class="link" onclick={openToday} disabled={busy}>
				Open <Glyph name="chevron" />
			</button>

			{#if inProgress.length > 0}
				<h2 class="label bottom">In progress</h2>
				{#each inProgress as task (task.id)}
					<a class="row" href={noteHref(task.path)}>
						<span class="dot" aria-hidden="true"></span>
						<span class="text">{task.text}</span>
						<span class="meta">{name(task.path)}</span>
					</a>
				{/each}
			{/if}
		</section>

		<section class="panel goto">
			<div class="label-row">
				<h2 class="label">Go to</h2>
				<button
					type="button"
					class="icon-btn"
					title="New note"
					aria-label="New note"
					onclick={newNote}
					disabled={busy}
				>
					<Glyph name="plus" />
				</button>
			</div>
			{#if pinned.length > 0}
				<div class="pins">
					{#each pinned as path (path)}
						<a class="pin" href={noteHref(path)}>
							<Glyph name="bookmark" />
							<span class="pin-name">{name(path)}</span>
							{#if folderOf(path)}<span class="folder">{folderOf(path)}</span>{/if}
						</a>
					{/each}
				</div>
			{/if}
			{#if recent.length > 0}
				<h2 class="label spaced">Recent</h2>
				{#each recent as path (path)}
					<a class="row roomy" href={noteHref(path)}>
						<span class="glyph"><Glyph name="note" /></span>
						<span class="text">{name(path)}</span>
						{#if folderOf(path)}<span class="folder">{folderOf(path)}</span>{/if}
					</a>
				{/each}
			{/if}
			<!-- Calendar, Tasks and Paper have no place in the phone's tab bar. -->
			<div class="phone-tiles">
				<a class="tile" href="/calendar"><Glyph name="calendar" /><span>Calendar</span></a>
				<a class="tile" href="/tasks"><Glyph name="task" /><span>Tasks</span></a>
				<a class="tile" href="/paper"><Glyph name="paper" /><span>Paper</span></a>
			</div>
			<div class="spacer"></div>
			<button type="button" class="find" onclick={openPalette}>
				<Glyph name="search" />
				<span>Search notes</span>
			</button>
		</section>

		<div class="side">
			<section class="panel omnia">
				<h2 class="label">OMNIA</h2>
				{#if morning.message}
					<button type="button" class="greeting" title="Reply to OMNIA" onclick={replyToOmnia}>
						{morning.message}
					</button>
				{/if}
				{#if morning.observation}
					<div class="observation">
						<span>{morning.observation.text}</span>
						<button type="button" class="icon-btn" onclick={confirmObs} title="Confirm" aria-label="Confirm">
							<Glyph name="check" />
						</button>
						<button type="button" class="icon-btn" onclick={dismissObs} title="Dismiss" aria-label="Dismiss">
							<Glyph name="x" />
						</button>
					</div>
				{/if}
				{#if backupLine}
					<a class="backup" href="/settings#vault">{backupLine}</a>
				{/if}
			</section>

			<section class="panel week">
				<h2 class="label">This week</h2>
				{#if todayEvents.length > 0}
					<h3 class="label small">Today</h3>
					{#each todayEvents as ev (ev.id)}
						<a
							class="row"
							class:past={eventOver(ev, now)}
							href={ev.note_path ? noteHref(ev.note_path) : '/calendar'}
						>
							<span class="time">{eventTime(ev) || 'All day'}</span>
							<span class="ring" aria-hidden="true"></span>
							<span class="text">{ev.title}</span>
						</a>
					{/each}
				{/if}
				{#if upcoming.length > 0}
					{#if todayEvents.length > 0}<h3 class="label small spaced-sm">Coming up</h3>{/if}
					{#each upcoming as item (item.key)}
						<a
							class="row"
							href={item.task
								? noteHref(item.task.path)
								: item.event?.note_path
									? noteHref(item.event.note_path)
									: '/calendar'}
						>
							<span class="time">{dayLabel(item.date)}</span>
							<span class={item.kind === 'event' ? 'ring' : 'dot'} aria-hidden="true"></span>
							<span class="text">{item.title}</span>
							{#if item.time}<span class="meta">{item.time}</span>{/if}
						</a>
					{/each}
				{/if}
				{#if briefing && todayEvents.length === 0 && upcoming.length === 0}
					<p class="empty">Nothing scheduled.</p>
				{/if}
			</section>
		</div>
	</div>
</div>

<style>
	.hub {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		padding: 28px 40px 36px;
	}
	.head {
		display: flex;
		align-items: baseline;
		gap: 16px;
		margin-bottom: 20px;
	}
	.date {
		margin: 0;
		font-size: 28px;
		font-weight: 800;
		letter-spacing: -0.4px;
		line-height: 1.1;
	}
	.stats {
		color: var(--text-muted);
		font-size: 13px;
	}
	.daily-btn {
		margin-left: auto;
		align-self: center;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 7px 15px 7px 13px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: none;
		color: var(--text);
		font: inherit;
		font-size: 13px;
		font-weight: 600;
		cursor: pointer;
		transition:
			background-color var(--motion-fast) var(--ease-out),
			border-color var(--motion-fast) var(--ease-out);
	}
	.daily-btn :global(svg) {
		width: 14px;
		height: 14px;
	}
	.daily-btn:hover {
		background: var(--bg-hover);
	}
	.grid {
		flex: 1;
		min-height: 0;
		display: grid;
		grid-template-columns: 1fr 1.2fr 1fr;
		gap: 16px;
	}
	.panel {
		min-height: 0;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		padding: 18px;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--bg-panel);
	}
	.side {
		min-height: 0;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.week {
		flex: 1;
	}
	.label {
		margin: 0 0 10px;
		color: var(--text-muted);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.9px;
		text-transform: uppercase;
	}
	.label.small {
		margin-bottom: 4px;
	}
	.label.spaced {
		margin-top: 24px;
	}
	.label.spaced-sm {
		margin-top: 14px;
	}
	.label.bottom {
		margin-top: auto;
		padding-top: 20px;
	}
	.label-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 10px;
	}
	.label-row .label {
		margin: 0;
	}
	.icon-btn {
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		flex: none;
		padding: 0;
		border: none;
		border-radius: 7px;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		transition:
			background-color var(--motion-fast) var(--ease-out),
			color var(--motion-fast) var(--ease-out);
	}
	.icon-btn :global(svg) {
		width: 14px;
		height: 14px;
	}
	.icon-btn:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.progress {
		height: 2px;
		margin-bottom: 8px;
		overflow: hidden;
		border-radius: 2px;
		background: var(--border);
	}
	.progress i {
		display: block;
		height: 100%;
		background: var(--text);
		transition: width var(--motion-slow) var(--ease-out);
	}
	.row {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 8px 10px;
		border: none;
		border-radius: 8px;
		background: none;
		color: var(--text);
		font: inherit;
		font-size: 14px;
		text-align: left;
		text-decoration: none;
		cursor: pointer;
		transition: background-color var(--motion-fast) var(--ease-out);
	}
	.row:hover {
		background: var(--bg-hover);
	}
	.row.roomy {
		padding: 11px 10px;
	}
	.row.past {
		opacity: 0.4;
	}
	.text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.meta {
		margin-left: auto;
		flex: none;
		color: var(--text-muted);
		font-size: 12px;
	}
	.time {
		min-width: 52px;
		flex: none;
		color: var(--text-muted);
		font-size: 12.5px;
		font-variant-numeric: tabular-nums;
	}
	.folder {
		flex: none;
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.glyph {
		display: grid;
		color: var(--text-muted);
	}
	.glyph :global(svg) {
		width: 14px;
		height: 14px;
	}
	.box {
		display: grid;
		place-items: center;
		width: 16px;
		height: 16px;
		flex: none;
		border: 1.5px solid var(--text-muted);
		border-radius: 5px;
		transition:
			background-color var(--motion-fast) var(--ease-out),
			border-color var(--motion-fast) var(--ease-out);
	}
	.box :global(svg) {
		width: 11px;
		height: 11px;
		color: var(--bg);
	}
	.check.done .box {
		border-color: var(--text);
		background: var(--text);
	}
	.check.done .text {
		color: var(--text-muted);
		text-decoration: line-through;
	}
	.dot {
		width: 6px;
		height: 6px;
		flex: none;
		border-radius: 50%;
		background: var(--text);
	}
	.ring {
		width: 7px;
		height: 7px;
		flex: none;
		border: 1.5px solid var(--text);
		border-radius: 50%;
	}
	.add input {
		width: 100%;
		margin-top: 4px;
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
		color: var(--text);
		font: inherit;
		font-size: 14px;
	}
	.excerpt {
		padding: 2px 0 2px 12px;
		border-left: 2px solid var(--border);
		color: var(--text-muted);
		font-size: 13.5px;
	}
	.excerpt p {
		margin: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.link {
		display: flex;
		align-items: center;
		gap: 4px;
		width: fit-content;
		margin-top: 10px;
		padding: 0;
		border: none;
		background: none;
		color: var(--text-muted);
		font: inherit;
		font-size: 12.5px;
		cursor: pointer;
		transition: color var(--motion-fast) var(--ease-out);
	}
	.link :global(svg) {
		width: 12px;
		height: 12px;
	}
	.link:hover {
		color: var(--text);
	}
	.pins {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
	}
	.pin {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 6px;
		min-width: 0;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: 10px;
		color: var(--text);
		text-decoration: none;
		transition:
			background-color var(--motion-fast) var(--ease-out),
			border-color var(--motion-fast) var(--ease-out);
	}
	.pin :global(svg) {
		width: 14px;
		height: 14px;
		color: var(--text-muted);
	}
	.pin:hover {
		background: var(--bg-hover);
	}
	.pin-name {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 700;
	}
	.find {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 11px 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: none;
		color: var(--text-muted);
		font: inherit;
		font-size: 14px;
		cursor: pointer;
		transition:
			background-color var(--motion-fast) var(--ease-out),
			color var(--motion-fast) var(--ease-out);
	}
	.find :global(svg) {
		width: 14px;
		height: 14px;
	}
	.find:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	/* Pushes search to the bottom of the panel, keeping a gap when the lists run long. */
	.spacer {
		flex: 1;
		min-height: 16px;
	}
	.phone-tiles {
		display: none;
	}
	.greeting {
		margin: 0 0 14px;
		padding: 0;
		border: none;
		background: none;
		color: var(--text);
		font: inherit;
		font-size: 14px;
		line-height: 1.5;
		text-align: left;
		cursor: pointer;
		transition: color var(--motion-fast) var(--ease-out);
	}
	.greeting:hover {
		color: var(--text-muted);
	}
	.observation {
		display: flex;
		align-items: flex-start;
		gap: 6px;
		font-size: 13.5px;
	}
	.observation span {
		flex: 1;
	}
	.backup {
		margin-top: 10px;
		color: var(--text-muted);
		font-size: 12.5px;
		text-decoration: none;
	}
	.backup:hover {
		color: var(--text);
	}
	.empty {
		margin: 6px 10px;
		color: var(--text-muted);
		font-size: 13px;
	}
	button:disabled {
		opacity: 0.5;
		cursor: default;
	}

	/* Narrow windows: the side column moves under the first two. */
	@media (max-width: 1100px) and (min-width: 761px) {
		.grid {
			grid-template-columns: 1fr 1fr;
		}
		.side {
			grid-column: 1 / -1;
			flex-direction: row;
		}
		.side .panel {
			flex: 1;
		}
	}

	@media (max-width: 760px) {
		.hub {
			overflow-y: auto;
			-webkit-overflow-scrolling: touch;
			padding: 22px var(--m-gutter) 28px;
		}
		.head {
			flex-wrap: wrap;
			gap: 6px 12px;
			margin-bottom: 18px;
		}
		.date {
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		.stats {
			order: 3;
			width: 100%;
			font-size: var(--m-meta);
		}
		.daily-btn {
			min-height: 44px;
			font-size: var(--m-meta);
		}
		/* One scrolling column: nothing shrinks to fit the screen. */
		.grid,
		.side {
			flex: none;
			display: flex;
			flex-direction: column;
			gap: 14px;
		}
		.panel {
			flex: none;
			min-height: auto;
			overflow: visible;
			padding: 14px;
			border-radius: var(--m-radius);
		}
		.label {
			font-size: var(--m-label);
			font-weight: 600;
			letter-spacing: 0.5px;
		}
		.label.bottom {
			margin-top: 20px;
			padding-top: 0;
		}
		.row,
		.row.roomy {
			min-height: 44px;
			font-size: var(--m-row-text);
		}
		.meta,
		.time,
		.folder,
		.empty,
		.backup {
			font-size: var(--m-meta);
		}
		.icon-btn {
			width: 44px;
			height: 44px;
		}
		.excerpt,
		.observation,
		.greeting,
		.add input,
		.find {
			font-size: var(--m-row-text);
		}
		.link {
			min-height: 44px;
			font-size: var(--m-meta);
		}
		.find {
			min-height: 44px;
		}
		.spacer {
			min-height: 14px;
		}
		.phone-tiles {
			display: grid;
			grid-template-columns: repeat(3, 1fr);
			gap: 8px;
			margin-top: 14px;
		}
		.tile {
			display: flex;
			flex-direction: column;
			align-items: center;
			justify-content: center;
			gap: 6px;
			min-height: 64px;
			border: 1px solid var(--border);
			border-radius: var(--m-radius);
			color: var(--text);
			font-size: var(--m-meta);
			text-decoration: none;
		}
		.tile :global(svg) {
			width: var(--m-icon);
			height: var(--m-icon);
		}
		.tile:active {
			background: var(--bg-hover);
		}
	}
</style>
