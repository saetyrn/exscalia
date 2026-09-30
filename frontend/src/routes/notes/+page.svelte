<script lang="ts">
	// The mobile notes browser: the whole vault behind the Notes tab, so a
	// phone never has to open the sidebar drawer to find a note.
	//
	// It draws the same tree as the sidebar, from the same builder
	// (lib/notetree.ts) and the same persisted preferences
	// (lib/notelist.svelte.ts) — change the sort here and the sidebar is
	// already sorted that way, collapse a folder here and it is collapsed
	// there. What it deliberately does not carry is the sidebar's editing
	// surface: no drag-reorder, no context menus, no rename-on-double-click.
	import { goto } from '$app/navigation';
	import { api, encodePath } from '$lib/api';
	import Glyph from '$lib/Glyph.svelte';
	import { numberedPath } from '$lib/notepaths';
	import { noteView, setNoteSort, toggleFolder } from '$lib/notelist.svelte';
	import {
		buildTree,
		filterTree,
		noteLabel,
		NOTE_SORTS,
		type NoteSort,
		type TreeNode
	} from '$lib/notetree';
	import { refreshNotes, vault } from '$lib/stores.svelte';
	import { notify } from '$lib/ui.svelte';

	let query = $state('');
	let busy = $state(false);

	let needle = $derived(query.trim().toLowerCase());
	let searching = $derived(needle.length > 0);

	let tree = $derived(
		buildTree({
			notes: vault.notes,
			folders: vault.folders,
			sortMode: noteView.sort,
			folderOrder: vault.folderOrder,
			noteOrder: vault.noteOrder
		})
	);

	// Matching on the path as well as the name means typing a folder name
	// surfaces everything inside it. Folders with nothing left under them drop
	// out, so the filtered tree is all signal.
	let shown = $derived(
		searching
			? filterTree(tree, (n) => n.children === null && n.path.toLowerCase().includes(needle))
			: tree
	);

	let pinned = $derived(
		vault.notes
			.filter((n) => n.pinned)
			.filter((n) => !searching || n.path.toLowerCase().includes(needle))
	);

	let empty = $derived(vault.loaded && shown.length === 0 && pinned.length === 0);

	/** While filtering, every folder is open — a hit hidden inside a collapsed
	 * folder is a search that looks broken. */
	const isOpen = (path: string) => searching || !noteView.collapsed[path];

	async function newNote() {
		if (busy) return;
		busy = true;
		try {
			// The same numbering every create path in the app uses, so this never
			// collides with a note the sidebar or the palette just made.
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

<svelte:head><title>Notes — EXSCALIA</title></svelte:head>

<div class="notes">
	<div class="search-bar">
		<span class="search-icon" aria-hidden="true"><Glyph name="search" /></span>
		<input
			type="search"
			bind:value={query}
			placeholder="Search notes"
			aria-label="Filter notes"
			autocomplete="off"
			autocorrect="off"
			spellcheck="false"
		/>
	</div>

	<div class="controls">
		<select
			class="sort"
			aria-label="Sort notes by"
			value={noteView.sort}
			onchange={(e) => setNoteSort(e.currentTarget.value as NoteSort)}
		>
			{#each NOTE_SORTS as option (option.value)}
				<option value={option.value}>{option.label}</option>
			{/each}
		</select>
		<button type="button" class="new" onclick={newNote} disabled={busy}>
			<span class="new-icon" aria-hidden="true"><Glyph name="plus" /></span>
			New note
		</button>
	</div>

	{#if pinned.length > 0}
		<section class="section">
			<h2 class="section-label">Pinned</h2>
			<div class="rows">
				{#each pinned as note (note.path)}
					<a class="row note" href="/note/{encodePath(note.path)}">
						<span class="row-icon"><Glyph name="note" /></span>
						<span class="row-name">{noteLabel(note.name)}</span>
					</a>
				{/each}
			</div>
		</section>
	{/if}

	<section class="section" class:pending={!vault.loaded && shown.length === 0}>
		<h2 class="section-label">{searching ? 'Matches' : 'All notes'}</h2>
		<!-- Hidden until the note list arrives, so the first paint is not an
		     empty bordered box that then fills in. -->
		<div class="rows">
			{#snippet level(nodes: TreeNode[], depth: number)}
				{#each nodes as node (node.path)}
					{#if node.children !== null}
						<!-- Depth travels as a custom property rather than a computed
						     padding-left: an inline padding would outrank the row's own
						     gutter and pin every nesting level to the desktop scale. -->
						<button
							type="button"
							class="row folder"
							style:--depth={depth}
							aria-expanded={isOpen(node.path)}
							onclick={() => toggleFolder(node.path)}
						>
							<span class="chev" class:open={isOpen(node.path)} aria-hidden="true">
								<Glyph name="chevron" />
							</span>
							<span class="row-icon"><Glyph name="folder" /></span>
							<span class="row-name">{node.name}</span>
						</button>
						{#if isOpen(node.path)}
							{@render level(node.children, depth + 1)}
						{/if}
					{:else}
						<a class="row note" style:--depth={depth} href="/note/{encodePath(node.path)}">
							<span class="row-icon"><Glyph name="note" /></span>
							<span class="row-name">{noteLabel(node.name)}</span>
						</a>
					{/if}
				{/each}
			{/snippet}
			{@render level(shown, 0)}
			{#if empty}
				<p class="empty">{searching ? 'No notes match.' : 'No notes yet.'}</p>
			{/if}
		</div>
	</section>

	{#if !searching && vault.tags.length > 0}
		<section class="section">
			<h2 class="section-label">Tags</h2>
			<div class="tags">
				{#each vault.tags as tag (tag.tag)}
					<a class="chip" href="/tags/{encodeURIComponent(tag.tag)}">
						#{tag.tag}<span class="count">{tag.count}</span>
					</a>
				{/each}
			</div>
		</section>
	{/if}
</div>

<style>
	.notes {
		flex: 1;
		min-height: 0;
		width: 100%;
		max-width: 640px;
		margin: 0 auto;
		overflow-y: auto;
		-webkit-overflow-scrolling: touch;
		/* The tab bar sits in flow below the route, so the route view is
		   already --tabbar-h shorter; this is breathing room, not clearance. */
		padding: 0 16px 28px;
	}

	/* Sticky over the list it filters, opaque so rows pass behind it. The
	   glyph sits inside the field, so the input's edges line up with every
	   other left edge on the page. */
	.search-bar {
		position: sticky;
		top: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		margin: 0 -16px;
		padding: 10px 16px;
		background: var(--bg);
	}
	.search-icon {
		position: absolute;
		left: 27px;
		display: block;
		width: 16px;
		height: 16px;
		color: var(--text-muted);
		pointer-events: none;
	}
	.search-bar input {
		flex: 1;
		min-width: 0;
		height: 44px;
		padding: 0 10px 0 34px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg-panel);
		color: var(--text);
		font: inherit;
		font-size: 15px;
	}
	.search-bar input:focus {
		border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
		outline: none;
	}
	.search-bar input::-webkit-search-cancel-button {
		-webkit-appearance: none;
	}

	.controls {
		display: flex;
		align-items: center;
		gap: 10px;
		padding-bottom: 14px;
	}
	.sort {
		flex: 1;
		min-width: 0;
		height: 40px;
		padding: 0 8px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg-panel);
		color: var(--text-muted);
		font: inherit;
		font-size: 13px;
	}
	.new {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
		height: 40px;
		padding: 0 12px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg-panel);
		color: var(--text);
		font: inherit;
		font-size: 13px;
		font-weight: 600;
		cursor: pointer;
		transition: background-color var(--motion-fast) var(--ease-out);
	}
	.new:active {
		background: var(--bg-hover);
	}
	.new:disabled {
		opacity: 0.5;
		pointer-events: none;
	}
	.new-icon {
		display: block;
		width: 14px;
		height: 14px;
		color: var(--text-muted);
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding-bottom: 22px;
	}
	.section.pending {
		visibility: hidden;
	}
	.section-label {
		margin: 0;
		padding-left: 2px;
		color: var(--text-muted);
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.7px;
		text-transform: uppercase;
	}

	.rows {
		display: flex;
		flex-direction: column;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg-panel);
		overflow: hidden;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		min-height: 48px;
		/* One step of nesting per --depth, set by the tree snippet; rows outside
		   the tree (Pinned) leave it unset and sit flush at the gutter. */
		padding: 0 12px 0 calc(12px + var(--depth, 0) * 14px);
		border: 0;
		border-radius: 0;
		background: none;
		color: var(--text);
		font: inherit;
		text-align: left;
		text-decoration: none;
		cursor: pointer;
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
		width: 17px;
		height: 17px;
		color: var(--text-muted);
	}
	.row-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		font-size: 15px;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row.folder .row-name {
		font-weight: 600;
	}
	.chev {
		display: block;
		flex-shrink: 0;
		width: 13px;
		height: 13px;
		color: var(--text-muted);
		/* Rotation is orientation, not a hover/press effect. */
		transition: transform var(--motion-fast) var(--ease-out);
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.empty {
		margin: 0;
		padding: 14px 12px;
		color: var(--text-muted);
		font-size: 14px;
	}

	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		min-height: 32px;
		padding: 0 10px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--bg-panel);
		color: var(--text-muted);
		font-size: 12px;
		text-decoration: none;
		transition:
			background-color var(--motion-fast) var(--ease-out),
			color var(--motion-fast) var(--ease-out);
	}
	.chip:active {
		background: var(--bg-hover);
		color: var(--text);
	}
	.chip .count {
		color: var(--text-muted);
		font-size: 11px;
		opacity: 0.75;
	}

	.notes :global(svg) {
		display: block;
		width: 100%;
		height: 100%;
	}

	/* The phone scale: the shared --m-* proportions, so a row here is the same
	   object as a row on the home dashboard or in the drawer. Kept as an
	   override rather than folded into the base above, because desktop can
	   still reach this URL and keeps the sizes it has always had. */
	@media (max-width: 760px) {
		.notes {
			padding: 0 var(--m-gutter) 28px;
		}
		.search-bar {
			margin: 0 calc(-1 * var(--m-gutter));
			padding: 10px var(--m-gutter);
		}
		/* 16px or iOS zooms the page in the moment the field is focused — the
		   same reason every field in app.css is 16px under this breakpoint. */
		.search-bar input {
			font-size: var(--m-row-text);
		}
		/* The pair sat at a declared 40px while app.css's mobile floor pushed
		   the select to 44 — two controls on one line at two heights. */
		.sort,
		.new {
			height: 44px;
			font-size: var(--m-row-text);
		}
		.new-icon {
			width: 16px;
			height: 16px;
		}
		.section-label {
			font-size: var(--m-label);
			font-weight: 600;
			letter-spacing: 0.5px;
		}
		.rows {
			border-radius: var(--m-radius);
		}
		.row {
			gap: 12px;
			min-height: var(--m-row-h);
			padding: 0 14px 0 calc(14px + var(--depth, 0) * 14px);
		}
		.row-icon {
			width: var(--m-icon);
			height: var(--m-icon);
		}
		.row-name {
			font-size: var(--m-row-text);
		}
		.chip {
			min-height: 36px;
			padding: 0 12px;
			font-size: var(--m-label);
		}
	}

	/* Desktop can reach /notes by URL and it works, but the sidebar is the
	   browser there — this page just widens its gutters and gets out of the
	   way rather than pretending to be a second navigation surface. */
	@media (min-width: 761px) {
		.notes {
			padding: 0 28px 40px;
		}
		.search-bar {
			margin: 0 -28px;
			padding: 16px 28px;
		}
		.search-icon {
			left: 39px;
		}
	}
</style>
