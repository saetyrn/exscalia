<script lang="ts">
	import { goto } from '$app/navigation';
	import { api, encodePath } from '$lib/api';
	import { noteHref } from '$lib/recipes/links';
	import { localToday } from '$lib/dates';
	import Glyph from '$lib/Glyph.svelte';
	import { numberedPath } from '$lib/notepaths';
	import { recentNotes, refreshNotes, vault } from '$lib/stores.svelte';
	import { notify, openPalette } from '$lib/ui.svelte';

	// "Thursday, August 21" — the locale decides the wording; the fields are
	// what a lock screen shows, minus the year nobody needs to be told.
	let today = $derived(
		new Date().toLocaleDateString(undefined, {
			weekday: 'long',
			month: 'long',
			day: 'numeric'
		})
	);

	// Same source as the sidebar's Pinned section — a flag on the note list
	// item, set from its frontmatter. The sidebar caps pinning at three, so
	// three is also all this can ever show.
	let pinned = $derived(
		vault.notes
			.filter((n) => n.pinned)
			.slice(0, 3)
			.map((n) => ({ path: n.path, name: n.name.replace(/\.md$/, '') }))
	);

	// The last few notes worth linking back to, newest first. Prefer the live
	// vault entry (so the name stays current and a deleted note drops off), but
	// if the list has not loaded yet, offer the remembered path optimistically
	// rather than blank. A note listed again two rows below its own Pinned
	// entry is noise, so anything already up there is skipped.
	let recent = $derived.by(() => {
		const above = new Set(pinned.map((n) => n.path));
		const out: { path: string; name: string }[] = [];
		for (const p of recentNotes.paths) {
			if (above.has(p)) continue;
			const found = vault.notes.find((n) => n.path === p);
			if (found) out.push({ path: found.path, name: found.name.replace(/\.md$/, '') });
			else if (!vault.loaded)
				out.push({ path: p, name: (p.split('/').pop() ?? p).replace(/\.md$/, '') });
			if (out.length === 5) break;
		}
		return out;
	});

	/** Folder a note lives in, shown muted beside the name; '' for root notes. */
	const folderOf = (path: string) => path.split('/').slice(0, -1).join('/');

	let busy = $state(false);

	// Mirror the sidebar: get-or-create today's daily note, then open it.
	async function openToday() {
		if (busy) return;
		busy = true;
		try {
			const date = localToday();
			const path = `daily/${date}.md`;
			if (!vault.notes.some((n) => n.path === path)) {
				await api.openDaily(date);
				void refreshNotes();
			}
			await goto(`/note/${encodePath(path)}`);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			busy = false;
		}
	}

	// Mirror the sidebar's root "+": next free untitled, created then opened.
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
</script>

<div class="landing">
	<!-- Desktop keeps the bare wordmark: the sidebar is always open there, so
	     the landing has no work to do. Exactly one of these two headings is
	     ever displayed, so there is only ever one h1 on the page. -->
	<h1 class="wordmark">EXSCALIA</h1>

	<div class="home">
		<header class="hero">
			<p class="date">{today}</p>
			<h1 class="greeting">Today</h1>
		</header>

		<button type="button" class="daily" onclick={openToday} disabled={busy}>
			<span class="daily-label">Open today's daily</span>
			<span class="chev" aria-hidden="true"><Glyph name="chevron" /></span>
		</button>

		{#if pinned.length > 0}
			<section class="section">
				<h2 class="section-label">Pinned</h2>
				<div class="rows">
					{#each pinned as note (note.path)}
						<a class="row" href={noteHref(note.path)}>
							<span class="row-icon"><Glyph name="note" /></span>
							<span class="row-name">{note.name}</span>
							{#if folderOf(note.path)}
								<span class="row-folder">{folderOf(note.path)}</span>
							{/if}
							<span class="row-chev" aria-hidden="true"><Glyph name="chevron" /></span>
						</a>
					{/each}
				</div>
			</section>
		{/if}

		{#if recent.length > 0}
			<section class="section">
				<h2 class="section-label">Recent</h2>
				<div class="rows">
					{#each recent as note (note.path)}
						<a class="row" href={noteHref(note.path)}>
							<span class="row-icon"><Glyph name="note" /></span>
							<span class="row-name">{note.name}</span>
							{#if folderOf(note.path)}
								<span class="row-folder">{folderOf(note.path)}</span>
							{/if}
							<span class="row-chev" aria-hidden="true"><Glyph name="chevron" /></span>
						</a>
					{/each}
				</div>
			</section>
		{/if}

		<section class="section">
			<h2 class="section-label">Go to</h2>
			<!-- Deliberately the destinations the tab bar does NOT carry: Home,
			     Notes, Media, Recipes and OMNIA are one thumb-press away already. -->
			<div class="tiles">
				<button type="button" class="tile" onclick={newNote} disabled={busy}>
					<span class="tile-icon"><Glyph name="plus" /></span>
					<span class="tile-label">New note</span>
				</button>
				<a class="tile" href="/calendar">
					<span class="tile-icon"><Glyph name="calendar" /></span>
					<span class="tile-label">Calendar</span>
				</a>
				<a class="tile" href="/tasks">
					<span class="tile-icon"><Glyph name="task" /></span>
					<span class="tile-label">Tasks</span>
				</a>
				<a class="tile" href="/paper">
					<span class="tile-icon"><Glyph name="paper" /></span>
					<span class="tile-label">Paper</span>
				</a>
				<button type="button" class="tile" onclick={openPalette}>
					<span class="tile-icon"><Glyph name="search" /></span>
					<span class="tile-label">Search</span>
				</button>
			</div>
		</section>
	</div>
</div>

<style>
	.landing {
		margin: 0;
		flex: 1;
		min-height: 0;
		/* Desktop: the wordmark sits quietly in the bottom-right corner rather
		   than dead-centre. Mobile resets this to a normal scroller below. */
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		justify-content: flex-end;
		padding: 20px 24px;
		text-align: right;
	}
	.wordmark {
		color: var(--text-muted);
		font-size: 20px;
		margin: 0;
	}
	/* The dashboard is a mobile affordance only. */
	.home {
		display: none;
	}

	@media (max-width: 760px) {
		/* The old landing had no scroll container, so anything past the fold
		   was unreachable. It is the scroller now. The tab bar is in flow
		   below <main>, not fixed over it, so --tabbar-h clearance is already
		   provided by the layout — the padding here is breathing room. */
		.landing {
			flex: 1;
			min-height: 0;
			width: 100%;
			margin: 0;
			/* Undo the desktop corner-pin: the dashboard flows top-down, full width. */
			align-items: stretch;
			justify-content: flex-start;
			padding: 0;
			overflow-y: auto;
			-webkit-overflow-scrolling: touch;
			text-align: left;
		}
		.wordmark {
			display: none;
		}
		.home {
			display: flex;
			flex-direction: column;
			gap: 22px;
			max-width: 440px;
			margin: 0 auto;
			padding: 22px var(--m-gutter) 28px;
		}

		.hero {
			display: flex;
			flex-direction: column;
			gap: 4px;
		}
		.date {
			margin: 0;
			color: var(--text-muted);
			font-size: var(--m-meta);
			letter-spacing: 0.2px;
		}
		.greeting {
			margin: 0;
			color: var(--text);
			font-size: 28px;
			font-weight: 700;
			letter-spacing: -0.4px;
			line-height: 1.1;
		}

		/* The app's most-used entry, so it gets the only tinted surface. */
		.daily {
			display: flex;
			align-items: center;
			justify-content: space-between;
			gap: 10px;
			width: 100%;
			min-height: var(--m-row-h);
			padding: 0 14px;
			border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--border));
			border-radius: var(--m-radius);
			background: var(--accent-soft);
			color: var(--text);
			font: inherit;
			text-align: left;
			cursor: pointer;
			transition: background-color var(--motion-fast) var(--ease-out);
		}
		.daily-label {
			font-size: var(--m-row-text);
			font-weight: 600;
		}
		.daily .chev {
			display: block;
			flex-shrink: 0;
			width: 16px;
			height: 16px;
			color: var(--text-muted);
		}
		.daily:active {
			background: var(--bg-hover);
		}
		.daily:disabled {
			opacity: 0.5;
			pointer-events: none;
		}

		.section {
			display: flex;
			flex-direction: column;
			gap: 8px;
		}
		/* The app-wide mobile section label: --m-label / 600 / 0.5px / uppercase. */
		.section-label {
			margin: 0;
			padding-left: 2px;
			color: var(--text-muted);
			font-size: var(--m-label);
			font-weight: 600;
			letter-spacing: 0.5px;
			text-transform: uppercase;
		}

		/* One grouped list with hairlines rather than eight separate cards —
		   less border noise at the same tap size. */
		.rows {
			border: 1px solid var(--border);
			border-radius: var(--m-radius);
			background: var(--bg-panel);
			overflow: hidden;
		}
		.row {
			display: flex;
			align-items: center;
			gap: 12px;
			min-height: var(--m-row-h);
			padding: 0 14px;
			color: var(--text);
			text-decoration: none;
			transition: background-color var(--motion-fast) var(--ease-out);
		}
		.row + .row {
			border-top: 1px solid var(--border);
		}
		.row:active {
			background: var(--bg-hover);
		}
		.row-icon {
			display: block;
			flex-shrink: 0;
			width: var(--m-icon);
			height: var(--m-icon);
			color: var(--text-muted);
		}
		.row-name {
			flex: 1;
			min-width: 0;
			overflow: hidden;
			font-size: var(--m-row-text);
			text-overflow: ellipsis;
			white-space: nowrap;
		}
		.row-folder {
			flex-shrink: 0;
			max-width: 40%;
			overflow: hidden;
			color: var(--text-muted);
			font-size: var(--m-label);
			text-overflow: ellipsis;
			white-space: nowrap;
		}
		/* The disclosure every iOS list row ends with: it is what tells a row
		   apart from a static line of text before you touch it. */
		.row-chev {
			display: block;
			flex-shrink: 0;
			width: 13px;
			height: 13px;
			margin-left: -2px;
			color: var(--text-muted);
		}

		.tiles {
			display: grid;
			grid-template-columns: 1fr 1fr;
			gap: 10px;
		}
		/* Icon beside the label, not above it: stacked, they read as headings
		   with a decoration on top. Side by side at row proportions, they read
		   as the buttons they are. */
		.tile {
			display: flex;
			align-items: center;
			gap: 10px;
			min-height: 56px;
			padding: 0 14px;
			border: 1px solid var(--border);
			border-radius: var(--m-radius);
			background: var(--bg-panel);
			color: var(--text);
			font: inherit;
			text-align: left;
			text-decoration: none;
			cursor: pointer;
			transition: background-color var(--motion-fast) var(--ease-out);
		}
		.tile:active {
			background: var(--bg-hover);
		}
		.tile:disabled {
			opacity: 0.5;
			pointer-events: none;
		}
		.tile-icon {
			display: block;
			flex-shrink: 0;
			width: var(--m-icon);
			height: var(--m-icon);
			color: var(--text-muted);
		}
		.tile-label {
			min-width: 0;
			overflow: hidden;
			font-size: 15px;
			font-weight: 600;
			text-overflow: ellipsis;
			white-space: nowrap;
		}

		.home :global(svg) {
			display: block;
			width: 100%;
			height: 100%;
		}
	}
</style>
