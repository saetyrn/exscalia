<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { tick } from 'svelte';
	import { fade, fly } from '$lib/motion';
	import { api, encodePath, type NoteListItem } from './api';
	import { openToday } from './daily';
	import { appShell, modal, modalOpen } from './modal';
	import { numberedPath } from './notepaths';
	import { handOffToOmnia, recentNotes, refreshNotes, vault } from './stores.svelte';
	import { closePalette, notify, openPalette, promptDialog, ui } from './ui.svelte';
	import { showMorning } from './morning.svelte';
	import { closeTab, cycleTab, newTab, openBackgroundTab, safeDecode, tabs } from './tabs.svelte';

	interface Command {
		id: string;
		label: string;
		hint: string;
		keywords: string;
		run: () => void | Promise<void>;
		/** A place with a URL of its own: Cmd/Ctrl+Enter opens it in a
		 * background tab instead. */
		href?: string;
	}

	/** Rows on an empty query. Recents and every command always show; only
	 * the note list is trimmed to fit. */
	const EMPTY_LIMIT = 30;
	const RECENT_ROWS = 8;

	let query = $state('');
	let selected = $state(0);
	let input: HTMLInputElement | undefined = $state();
	// The list scrolls; the highlight effect keeps its option in view.
	let palette: HTMLDivElement | undefined = $state();
	// The recent-notes list is owned by the store now (the note page records
	// opens via rememberNote); the palette just reads it.
	let recentPaths = $derived(recentNotes.paths);

	// The tab strip and the sidebar collapse are desktop-width chrome; in the
	// mobile layout their commands would act on nothing you can see.
	let narrow = $state(false);
	$effect(() => {
		const mq = window.matchMedia('(max-width: 760px)');
		narrow = mq.matches;
		const onChange = (e: MediaQueryListEvent) => (narrow = e.matches);
		mq.addEventListener('change', onChange);
		return () => mq.removeEventListener('change', onChange);
	});

	function currentNotePath(): string | null {
		return page.url.pathname.startsWith('/note/')
			? safeDecode(page.url.pathname.replace(/^\/note\//, ''))
			: null;
	}

	async function createUntitled(template?: string) {
		const path = numberedPath(vault.notes, template ?? 'untitled');
		const note = await api.createNote(path, undefined, template);
		await refreshNotes();
		await goto(`/note/${encodePath(note.path)}`);
	}

	async function createFolder() {
		const path = await promptDialog('Folder path, including any nesting:', {
			title: 'New folder',
			placeholder: 'projects/2026',
			confirmLabel: 'Create'
		});
		if (!path) return;
		await api.createFolder(path.replace(/\/+$/, ''));
		await refreshNotes();
		notify(`Created ${path}`, { tone: 'success' });
	}

	function dirOf(path: string): string {
		return path.split('/').slice(0, -1).join('/');
	}

	function place(id: string, label: string, keywords: string, href: string, hint = ''): Command {
		return { id, label, hint, keywords, href, run: () => goto(href) };
	}

	/** Where a note opens. A board and a media item each have a page of their
	 * own; under /note/ they are just the markdown behind it. */
	function noteHref(note: NoteListItem): string {
		if (note.board) {
			const folder = dirOf(note.path);
			return folder ? `/tasks/${encodePath(folder)}` : '/tasks';
		}
		// MediaCard's shape: /media/<vault path>, which starts with media/.
		if (note.path.startsWith('media/')) return `/media/${encodePath(note.path)}`;
		// And a recipe's page: /recipes/<vault path>.
		if (note.path.startsWith('recipes/')) return `/recipes/${encodePath(note.path)}`;
		return `/note/${encodePath(note.path)}`;
	}

	/** Commands proper — listed in full on an empty query. */
	let actions = $derived.by(() => {
		const notePath = currentNotePath();
		const base: Command[] = [
			{ id: 'new-note', label: 'New note', hint: 'Ctrl M', keywords: 'create', run: () => createUntitled() },
			// One command per template — searchable, no submenu machinery.
			...vault.notes
				.filter((n) => n.path.startsWith('templates/') && n.path.endsWith('.md'))
				.map((n) => n.name.replace(/\.md$/, ''))
				.sort()
				.map((t) => ({
					id: `template:${t}`,
					label: `New note from ${t}`,
					hint: '',
					keywords: 'create template',
					run: () => createUntitled(t)
				})),
			{ id: 'new-folder', label: 'New folder', hint: '', keywords: 'create directory', run: createFolder },
			{ id: 'new-task', label: 'New task', hint: '', keywords: 'todo inbox', run: () => goto('/tasks?new=1') },
			{ id: 'today', label: 'Open today', hint: '', keywords: 'daily note', run: async () => {
				await openToday();
			} },
			{ id: 'briefing', label: "Today's briefing", hint: '', keywords: 'morning greeting agenda day', run: showMorning },
			{ id: 'home', label: 'Home', hint: '', keywords: 'start', run: () => goto('/') },
			{ id: 'notes', label: 'All notes', hint: '', keywords: 'browse list folders', run: () => goto('/notes') },
			{ id: 'search', label: 'Search the vault', hint: '', keywords: 'find notes tasks', run: () => goto('/search') },
			{
				id: 'pending',
				label: 'Review pending changes',
				hint: vault.pendingCount > 0 ? String(vault.pendingCount) : '',
				keywords: 'approve proposals omnia edits',
				run: () => goto('/pending')
			},
			{ id: 'tasks', label: 'Tasks', hint: '', keywords: 'todo board', run: () => goto('/tasks') },
			{ id: 'media', label: 'Media', hint: '', keywords: 'books movies tv games library tracker', run: () => goto('/media') },
			{ id: 'recipes', label: 'Recipes', hint: '', keywords: 'cooking food dishes meals ingredients', run: () => goto('/recipes') },
			{ id: 'paper', label: 'Paper', hint: '', keywords: 'handwriting notebooks ipad pencil', run: () => goto('/paper') },
			{ id: 'calendar', label: 'Calendar', hint: '', keywords: 'dates activity', run: () => goto('/calendar') },
			{ id: 'graph', label: 'Graph', hint: '', keywords: 'links map network wikilinks', run: () => goto('/graph') },
			{ id: 'omnia', label: 'OMNIA', hint: '', keywords: 'ai chat', run: () => goto('/omnia') },
			{ id: 'settings', label: 'Settings', hint: '', keywords: 'appearance themes', run: () => goto('/settings') },
			{ id: 'themes', label: 'Themes', hint: '', keywords: 'appearance color', run: () => goto('/settings#appearance') },
			{ id: 'intoxicate', label: 'Intoxicate mode', hint: '', keywords: 'borders appearance', run: () => goto('/settings#intoxicate') },
			{ id: 'texture', label: 'Texture', hint: '', keywords: 'scanlines grain overlay appearance', run: () => goto('/settings#texture') },
			{
			id: 'omnia-history',
			label: 'OMNIA history',
			hint: '',
			keywords: 'actions log undo what did omnia do audit',
			run: () => goto('/history/omnia')
		},
		{ id: 'trash', label: 'Trash', hint: '', keywords: 'deleted restore', run: () => goto('/trash') }
		];
		if (!narrow) {
			base.push(
				{ id: 'tab-new', label: 'New tab', hint: '', keywords: 'open', run: () => newTab() },
				{ id: 'tab-close', label: 'Close tab', hint: '', keywords: '', run: () => closeTab(tabs.activeId) },
				{ id: 'tab-next', label: 'Next tab', hint: '', keywords: 'switch', run: () => cycleTab(1) },
				{ id: 'tab-prev', label: 'Previous tab', hint: '', keywords: 'switch', run: () => cycleTab(-1) },
				{ id: 'sidebar', label: 'Toggle sidebar', hint: '', keywords: 'hide show collapse', run: () => {
					window.dispatchEvent(new CustomEvent('exscalia:toggle-sidebar'));
				} }
			);
		}
		if (notePath) {
			base.push(
				{ id: 'history', label: 'Version history', hint: '', keywords: 'restore revisions', run: () => goto(`/history?path=${encodeURIComponent(notePath)}`) },
				{ id: 'rename', label: 'Rename current note', hint: '', keywords: 'move title', run: () => {
					window.dispatchEvent(new CustomEvent('exscalia:rename-note'));
				} },
				{ id: 'delete', label: 'Move current note to Trash', hint: '', keywords: 'remove', run: () => {
					window.dispatchEvent(new CustomEvent('exscalia:delete-note'));
				} }
			);
		}
		return base;
	});

	/** Recently opened notes that still exist, newest first. */
	let recentList = $derived(
		recentPaths
			.map((path) => vault.notes.find((item) => item.path === path))
			.filter((note): note is NoteListItem => note !== undefined)
			.slice(0, RECENT_ROWS)
	);
	let recents = $derived(
		recentList.map((note) =>
			place(`recent:${note.path}`, note.name, `recent note ${note.path}`, noteHref(note), `Recent · ${note.path}`)
		)
	);

	/** Tags and folder boards: searchable, never crowding the empty list. */
	let places = $derived([
		...vault.tags.map((t) => place(`tag:${t.tag}`, `#${t.tag}`, 'tag', `/tags/${encodeURIComponent(t.tag)}`)),
		...[...new Set(vault.notes.filter((n) => n.board).map((n) => dirOf(n.path)))]
			.filter(Boolean)
			.sort()
			.map((folder) => place(`board:${folder}`, `Tasks · ${folder}`, 'board todo', `/tasks/${encodePath(folder)}`))
	]);

	/** Every other note. Boards stay out: their row is the folder's
	 * "Tasks · …" place. */
	let notes = $derived.by(() => {
		const shown = new Set(recentList.map((note) => note.path));
		return vault.notes
			.filter((n) => !shown.has(n.path) && !n.board)
			.map((note) => place(`note:${note.path}`, note.name, `note ${note.path}`, noteHref(note), note.path));
	});

	/** The empty list's notes. OMNIA's own files and the templates only turn
	 * up for a typed query (a template's usual row is its "New note from"
	 * command), so opening one to edit still works. */
	let everydayNotes = $derived(notes.filter((note) => !/^note:(omnia|templates)\//.test(note.id)));

	let commands = $derived([...actions, ...recents, ...places, ...notes]);

	let filtered = $derived.by(() => {
		const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
		if (words.length === 0) {
			// Recents lead — the palette's best jump list used to be sliced off
			// below the commands — and no command is ever cut.
			const head = [...recents, ...actions];
			return [...head, ...everydayNotes.slice(0, Math.max(0, EMPTY_LIMIT - head.length))];
		}
		const matches = commands
			.map((command) => {
				const haystack = `${command.label} ${command.keywords} ${command.hint}`.toLowerCase();
				const score = words.reduce((total, word) => {
					const index = haystack.indexOf(word);
					return index < 0 ? -1000 : total + (index === 0 ? 8 : 3) - index / 100;
				}, 0);
				return { command, score };
			})
			.filter((entry) => entry.score > -100)
			.sort((a, b) => b.score - a.score)
			.map((entry) => entry.command)
			.slice(0, 30);
		// A short question nearly always matches some note path, which used to
		// hide this row exactly when one was typed — so it is always the last.
		if (matches.length) {
			const text = query.trim();
			matches.push({ id: 'ask-omnia', label: `Ask OMNIA: ${text}`, hint: '', keywords: '', run: () => askOmnia(text) });
		}
		return matches;
	});

	$effect(() => {
		if (!ui.paletteOpen) return;
		query = '';
		selected = 0;
	});

	// Keep the highlight inside the list as it shrinks. Selection was only
	// clamped on arrow keys, so arrowing down and then narrowing the query left
	// `selected` past the end: nothing highlighted, and Enter ran `undefined`
	// and fell through to a full-vault search instead of the visible match.
	$effect(() => {
		if (selected > filtered.length - 1) selected = Math.max(0, filtered.length - 1);
	});

	// The list scrolls; the highlight must not leave it.
	$effect(() => {
		palette?.querySelector(`#palette-opt-${selected}`)?.scrollIntoView({ block: 'nearest' });
	});

	/** Cmd/Ctrl+Enter or Cmd/Ctrl+click on a place: have it ready in a tab. */
	function runInBackground(href: string) {
		closePalette();
		openBackgroundTab(href);
	}

	async function run(command?: Command) {
		closePalette();
		try {
			if (command) await command.run();
			else if (query.trim()) await goto(`/search?q=${encodeURIComponent(query.trim())}`);
		} catch (error) {
			notify((error as Error).message, { tone: 'error' });
		}
	}

	/** Typed text is often a question, not a search: the last row of any
	 * query, and the fallback when nothing matches. Send it to OMNIA, which
	 * asks it as soon as /omnia has a thread. */
	async function askOmnia(text = query.trim()) {
		if (!text) return;
		closePalette();
		handOffToOmnia(text);
		await goto('/omnia');
	}

	function keydown(event: KeyboardEvent) {
		if (event.key === 'ArrowDown') {
			event.preventDefault();
			selected = Math.min(selected + 1, Math.max(0, filtered.length - 1));
		}
		if (event.key === 'ArrowUp') {
			event.preventDefault();
			selected = Math.max(0, selected - 1);
		}
		if (event.key === 'Enter') {
			event.preventDefault();
			const command = filtered[selected];
			if ((event.metaKey || event.ctrlKey) && command?.href) runInBackground(command.href);
			else void run(command);
		}
	}
</script>

<svelte:window
	onkeydown={(event) => {
		if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
			event.preventDefault();
			// A dialog already open owns the keyboard. The palette's lane is
			// below the dialogs', so it opened out of sight behind one, took
			// focus from it and made the shell under it inert.
			if (!ui.paletteOpen && modalOpen()) return;
			ui.paletteOpen ? closePalette() : openPalette();
		}
	}}
/>

{#if ui.paletteOpen}
	<button class="palette-scrim" aria-label="Close command palette" onclick={closePalette} transition:fade={{ duration: 110 }}></button>
	<div class="palette-wrap" role="presentation">
		<div
			class="palette"
			role="dialog"
			aria-modal="true"
			aria-label="Command palette"
			tabindex="-1"
			bind:this={palette}
			onkeydown={keydown}
			use:modal={{ initialFocus: () => input, onEscape: closePalette, inert: appShell() }}
			in:fly={{ y: -8, duration: 160 }}
		>
			<div class="palette-input">
				<span aria-hidden="true">⌕</span>
				<input
					bind:this={input}
					bind:value={query}
					placeholder="Search notes or run a command…"
					role="combobox"
					aria-expanded="true"
					aria-controls="palette-results"
					aria-label="Search notes or run a command"
					aria-activedescendant={filtered[selected] ? `palette-opt-${selected}` : undefined}
				/>
				<kbd>esc</kbd>
				<button
					class="palette-close"
					type="button"
					aria-label="Close"
					onclick={closePalette}
				>
					✕
				</button>
			</div>
			<div class="results" id="palette-results" role="listbox">
				{#each filtered as command, index (command.id)}
					<button
						id="palette-opt-{index}"
						class:selected={index === selected}
						class:ask-omnia={command.id === 'ask-omnia'}
						role="option"
						aria-selected={index === selected}
						onmouseenter={() => (selected = index)}
						onclick={(event) =>
							(event.metaKey || event.ctrlKey) && command.href
								? runInBackground(command.href)
								: run(command)}
					>
						<span>{command.label}</span>
						<small>{command.hint}</small>
					</button>
				{:else}
					<button class="search-all" onclick={() => run()}>
						Search the vault for “{query}”
					</button>
					<button class="ask-omnia" onclick={() => askOmnia()}>
						Ask OMNIA: {query}
					</button>
				{/each}
			</div>
		</div>
	</div>
{/if}

<style>
	.palette-scrim {
		position: fixed;
		inset: 0;
		z-index: var(--z-palette);
		border: 0;
		border-radius: 0;
		background: color-mix(in srgb, #000 38%, transparent);
		backdrop-filter: blur(2px);
	}
	.palette-wrap {
		position: fixed;
		inset: 0;
		z-index: calc(var(--z-palette) + 1);
		display: flex;
		justify-content: center;
		align-items: flex-start;
		padding: min(16vh, 130px) 16px 16px;
		pointer-events: none;
	}
	.palette {
		width: min(620px, 100%);
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: 14px;
		background: var(--bg-panel);
		color: var(--text);
		box-shadow: 0 26px 90px color-mix(in srgb, #000 40%, transparent);
		pointer-events: auto;
	}
	.palette-input {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 13px;
		border-bottom: 1px solid var(--border);
	}
	.palette-input > span {
		color: var(--text-muted);
		font-size: 20px;
	}
	.palette-input input {
		flex: 1;
		border: 0;
		background: transparent;
		padding: 6px 0;
		font-size: 16px;
		outline: none;
	}
	kbd {
		padding: 2px 5px;
		border: 1px solid var(--border);
		border-radius: 5px;
		color: var(--text-muted);
		font: 10px var(--font-mono);
	}
	.results {
		max-height: min(54vh, 470px);
		overflow-y: auto;
		padding: 6px;
	}
	.results button {
		width: 100%;
		display: flex;
		justify-content: space-between;
		gap: 12px;
		padding: 9px 10px;
		border: 0;
		text-align: left;
	}
	.results button.selected,
	.results button:hover {
		background: var(--bg-hover);
		color: var(--accent);
		transform: none;
	}
	.results small {
		overflow: hidden;
		color: var(--text-muted);
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.search-all {
		color: var(--accent);
	}
	/* Second of the two fallbacks, and quieter than the search above it. */
	.ask-omnia {
		color: var(--text-muted);
	}
	.palette-close {
		display: none;
		border: 0;
		background: none;
		color: var(--text-muted);
		font-size: 15px;
		min-width: 44px;
		min-height: 44px;
	}
	.palette-close:active {
		color: var(--text);
	}

	@media (max-width: 760px) {
		.palette-wrap {
			padding: 58px 12px 12px;
		}
		.palette-input kbd {
			display: none;
		}
		.palette-close {
			display: block;
		}
		.palette {
			border-radius: var(--m-radius);
		}
		/* dvh, not vh: with the URL bar expanded vh overstates the screen, and
		   the last result ended up under it. */
		.results {
			max-height: calc(100dvh - 150px);
		}
	}
</style>
