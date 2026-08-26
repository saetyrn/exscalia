<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { tick } from 'svelte';
	import { fade, fly } from 'svelte/transition';
	import { api, encodePath } from './api';
	import { numberedPath } from './notepaths';
	import { recentNotes, refreshNotes, vault } from './stores.svelte';
	import { closePalette, notify, openPalette, promptDialog, ui } from './ui.svelte';

	interface Command {
		id: string;
		label: string;
		hint: string;
		keywords: string;
		run: () => void | Promise<void>;
	}

	let query = $state('');
	let selected = $state(0);
	let input: HTMLInputElement | undefined = $state();
	let palette: HTMLDivElement | undefined = $state();
	// The recent-notes list is owned by the store now (the note page records
	// opens via rememberNote); the palette just reads it.
	let recentPaths = $derived(recentNotes.paths);

	function currentNotePath(): string | null {
		return page.url.pathname.startsWith('/note/')
			? decodeURIComponent(page.url.pathname.replace(/^\/note\//, ''))
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

	let commands = $derived.by(() => {
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
				const result = await api.openDaily();
				await refreshNotes();
				await goto(`/note/${encodePath(result.path)}`);
			} },
			{ id: 'search', label: 'Search the vault', hint: '', keywords: 'find notes tasks', run: () => goto('/search') },
			{ id: 'tasks', label: 'Tasks', hint: '', keywords: 'todo board', run: () => goto('/tasks') },
			{ id: 'media', label: 'Media', hint: '', keywords: 'books movies tv games library tracker', run: () => goto('/media') },
			{ id: 'calendar', label: 'Calendar', hint: '', keywords: 'dates activity', run: () => goto('/calendar') },
			{ id: 'graph', label: 'Graph', hint: '', keywords: 'links map network wikilinks', run: () => goto('/graph') },
			{ id: 'omnia', label: 'OMNIA', hint: '', keywords: 'ai chat', run: () => goto('/omnia') },
			{ id: 'settings', label: 'Settings', hint: '', keywords: 'appearance themes', run: () => goto('/settings') },
			{ id: 'themes', label: 'Themes', hint: '', keywords: 'appearance color', run: () => goto('/settings#appearance') },
			{ id: 'intoxicate', label: 'Intoxicate mode', hint: '', keywords: 'borders appearance', run: () => goto('/settings#intoxicate') },
			{
			id: 'omnia-history',
			label: 'OMNIA history',
			hint: '',
			keywords: 'actions log undo what did omnia do audit',
			run: () => goto('/history/omnia')
		},
		{ id: 'trash', label: 'Trash', hint: '', keywords: 'deleted restore', run: () => goto('/trash') }
		];
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
		for (const recentPath of recentPaths) {
			const note = vault.notes.find((item) => item.path === recentPath);
			if (!note) continue;
			base.push({
				id: `recent:${note.path}`,
				label: note.name,
				hint: `Recent · ${note.path}`,
				keywords: `recent note ${note.path}`,
				run: () => goto(`/note/${encodePath(note.path)}`)
			});
		}
		for (const note of vault.notes.filter((item) => !recentPaths.includes(item.path))) {
			base.push({
				id: `note:${note.path}`,
				label: note.name,
				hint: note.path,
				keywords: `note ${note.path}`,
				run: () => goto(`/note/${encodePath(note.path)}`)
			});
		}
		return base;
	});

	let filtered = $derived.by(() => {
		const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
		if (words.length === 0) return commands.slice(0, 18);
		return commands
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
	});

	$effect(() => {
		if (!ui.paletteOpen) return;
		query = '';
		selected = 0;
		tick().then(() => input?.focus());
	});

	// Keep the highlight inside the list as it shrinks. Selection was only
	// clamped on arrow keys, so arrowing down and then narrowing the query left
	// `selected` past the end: nothing highlighted, and Enter ran `undefined`
	// and fell through to a full-vault search instead of the visible match.
	$effect(() => {
		if (selected > filtered.length - 1) selected = Math.max(0, filtered.length - 1);
	});

	async function run(command?: Command) {
		closePalette();
		try {
			if (command) await command.run();
			else if (query.trim()) await goto(`/search?q=${encodeURIComponent(query.trim())}`);
		} catch (error) {
			notify((error as Error).message, { tone: 'error' });
		}
	}

	function keydown(event: KeyboardEvent) {
		if (event.key === 'Escape') {
			closePalette();
			return;
		}
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
			void run(filtered[selected]);
		}
		if (event.key === 'Tab' && palette) {
			const focusable = Array.from(
				palette.querySelectorAll<HTMLElement>(
					'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
				)
			);
			if (!focusable.length) return;
			const first = focusable[0];
			const last = focusable[focusable.length - 1];
			if (event.shiftKey && document.activeElement === first) {
				event.preventDefault();
				last.focus();
			} else if (!event.shiftKey && document.activeElement === last) {
				event.preventDefault();
				first.focus();
			}
		}
	}
</script>

<svelte:window
	onkeydown={(event) => {
		if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
			event.preventDefault();
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
			in:fly={{ y: -8, duration: 160 }}
		>
			<div class="palette-input">
				<span aria-hidden="true">⌕</span>
				<input bind:this={input} bind:value={query} placeholder="Search notes or run a command…" />
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
			<div class="results" role="listbox">
				{#each filtered as command, index (command.id)}
					<button
						class:selected={index === selected}
						role="option"
						aria-selected={index === selected}
						onmouseenter={() => (selected = index)}
						onclick={() => run(command)}
					>
						<span>{command.label}</span>
						<small>{command.hint}</small>
					</button>
				{:else}
					<button class="search-all" onclick={() => run()}>
						Search the vault for “{query}”
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
		z-index: 900;
		border: 0;
		border-radius: 0;
		background: color-mix(in srgb, #000 38%, transparent);
		backdrop-filter: blur(2px);
	}
	.palette-wrap {
		position: fixed;
		inset: 0;
		z-index: 901;
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
			padding: 58px 8px 8px;
		}
		.palette-input kbd {
			display: none;
		}
		.palette-close {
			display: block;
		}
		.palette {
			border-radius: 11px;
		}
		.results {
			max-height: calc(100vh - 150px);
		}
	}
</style>
