<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { openMediaAdd } from '$lib/media/adddialog.svelte';
	import { onMount, tick } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { flip, slide } from '$lib/motion';
	import { gapIndexAmong } from './dragmath';
	import { api, encodePath } from './api';
	import { openToday as openTodayNote } from './daily';
	import { closeTabsUnder, openBackgroundTab, retargetTabs, safeDecode } from './tabs.svelte';
	import { numberedPath } from './notepaths';
	import { buildTree, moveNoteInOrder, NOTE_SORTS, type NoteSort, type TreeNode } from './notetree';
	import {
		expandTo,
		noteView,
		renameCollapsedPrefix,
		setNoteSort,
		toggleFolder
	} from './notelist.svelte';
	import { touchDrag } from './touchdrag';
	import { isDesktop } from './runtime';
	import { modal } from './modal';
	import FolderIcon from './FolderIcon.svelte';
	import {
		afterPathsMoved,
		afterPathsRemoved,
		refreshNotes,
		refreshPendingCount,
		refreshTags,
		saveFolderOrder,
		saveNoteOrder,
		syncState,
		vault
	} from './stores.svelte';
	import { confirmDialog, notify, promptDialog } from './ui.svelte';

	let {
		mobileOpen = false,
		onClose = () => {},
		onCollapse = () => {}
	}: { mobileOpen?: boolean; onClose?: () => void; onCollapse?: () => void } = $props();

	let searchQ = $state('');
	let asideEl: HTMLElement | undefined = $state();

	// On a phone the open drawer is a modal: Tab stays inside it, Escape
	// closes it (the layer stack, not a window handler of the layout's), and
	// closing hands focus back to the menu button that opened it. The scrim
	// stays in +layout.
	$effect(() => {
		if (!mobileOpen || !asideEl) return;
		const drawer = modal(asideEl, { onEscape: () => onClose() });
		return () => drawer.destroy();
	});

	// Per-folder collapse state and the note sort both live in
	// notelist.svelte.ts, shared with the mobile /notes browser so the two
	// surfaces never disagree about the same persisted preference.
	let collapsed = $derived(noteView.collapsed);

	// Tags and the pending count change when data changes, not when you
	// navigate. Refetching per route change meant two extra requests every time
	// you clicked a note — 40 of them to read through 20 notes — to re-fetch a
	// tag histogram that almost never moves.
	onMount(() => {
		refreshTags();
		refreshPendingCount();
		const onDataChanged = () => {
			refreshTags();
			refreshPendingCount();
		};
		window.addEventListener('exscalia:data-changed', onDataChanged);
		window.addEventListener('exscalia:note-saved', onDataChanged);
		return () => {
			window.removeEventListener('exscalia:data-changed', onDataChanged);
			window.removeEventListener('exscalia:note-saved', onDataChanged);
		};
	});

	function submitSearch(e: Event) {
		e.preventDefault();
		const q = searchQ.trim();
		if (q) {
			goto(`/search?q=${encodeURIComponent(q)}`);
			onClose();
		}
	}

	// Instant when today's note exists (lib/daily.ts, shared with the palette).
	async function openToday() {
		await openTodayNote();
		onClose();
	}

	let noteSort = $derived(noteView.sort);

	let tree = $derived(
		buildTree({
			notes: vault.notes,
			folders: vault.folders,
			sortMode: noteSort,
			folderOrder: vault.folderOrder,
			noteOrder: vault.noteOrder
		})
	);
	/** Folders that own a task board. */
	let boardFolders = $derived(
		new Set(vault.notes.filter((n) => n.board).map((n) => dirOf(n.path)))
	);
	let pinnedNotes = $derived(vault.notes.filter((n) => n.pinned));
	let current = $derived(safeDecode(page.url.pathname.replace(/^\/note\//, '')));
	let todayPath = $state('');


	async function newNote(folder = '', template?: string) {
		// A note in the daily folder IS a daily note — creating one there means
		// today's note: open it if it exists, create it from the template if not.
		if (folder === 'daily') {
			expandTo('daily');
			await openToday();
			return;
		}
		// A templated note is named after its template; renaming one click away
		// beats a growing pile of untitleds.
		const path = numberedPath(vault.notes, template ?? 'untitled', folder);
		try {
			await api.createNote(path, undefined, template);
			await refreshNotes();
			if (folder) expandTo(folder);
			await goto(`/note/${encodePath(path)}`);
			onClose();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	// Template stems, straight from the notes list — no extra fetch, and the
	// menu stays correct offline.
	let templateNames = $derived(
		vault.notes
			.filter((n) => n.path.startsWith('templates/') && n.path.endsWith('.md'))
			.map((n) => n.name.replace(/\.md$/, ''))
			.sort()
	);

	onMount(() => {
		const now = new Date();
		todayPath = `daily/${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(
			now.getDate()
		).padStart(2, '0')}.md`;

		const handleKeydown = (event: KeyboardEvent) => {
			// A modified chord never types text, so it must also fire while the
			// editor (contenteditable) has focus — which is most of the time.
			if (
				(event.ctrlKey || event.metaKey) &&
				!event.shiftKey &&
				!event.altKey &&
				!event.repeat &&
				event.key.toLowerCase() === 'm'
			) {
				event.preventDefault();
				event.stopPropagation();
				newNote();
			}
		};

		// Capture the shortcut globally, while leaving normal typing untouched.
		window.addEventListener('keydown', handleKeydown, true);
		return () => window.removeEventListener('keydown', handleKeydown, true);
	});

	// Drag a note onto a folder row (or the sidebar background for the vault
	// root) to move it — a rename under the hood, like the note-header Rename.
	let dragged = $state<string | null>(null);
	let dropTarget = $state<string | null>(null); // folder path; '' = root

	function dirOf(path: string) {
		return path.split('/').slice(0, -1).join('/');
	}

	// Dragging a FOLDER rearranges it among its siblings rather than moving it
	// into things: an insertion line shows where it will land, and only slots
	// beside its own siblings are offered. Nesting a folder somewhere else stays
	// a deliberate act via right-click → Move, so a slipped drag can't bury a
	// folder inside another one.
	let draggedFolder = $state<string | null>(null);
	let dropSlot = $state<{ before: string; edge: 'above' | 'below' } | null>(null);

	function dragStart(e: DragEvent, path: string) {
		dragged = path;
		e.dataTransfer?.setData('text/plain', path);
		if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
	}

	function folderDragStart(e: DragEvent, path: string) {
		e.stopPropagation();
		draggedFolder = path;
		e.dataTransfer?.setData('text/plain', path);
		if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move';
	}

	function endDrag() {
		dragged = null;
		dropTarget = null;
		draggedFolder = null;
		dropSlot = null;
		noteDrop = null;
	}

	function dragOver(e: DragEvent, folder: string) {
		if (draggedFolder !== null) return; // handled by folderDragOver
		if (dragged === null) return;
		e.preventDefault();
		e.stopPropagation();
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
		dropTarget = folder;
		// Aiming at a folder is a move, not a reorder — drop the preview gap.
		noteDrop = null;
	}

	/** Offer an insertion slot above or below `folder`, if it is a sibling of
	 * the folder being dragged. */
	function folderDragOver(e: DragEvent, folder: string) {
		if (draggedFolder === null) return;
		e.preventDefault();
		e.stopPropagation();
		if (folder === draggedFolder || dirOf(folder) !== dirOf(draggedFolder)) {
			dropSlot = null;
			if (e.dataTransfer) e.dataTransfer.dropEffect = 'none';
			return;
		}
		if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
		const box = (e.currentTarget as HTMLElement).getBoundingClientRect();
		const edge = e.clientY - box.top < box.height / 2 ? 'above' : 'below';
		dropSlot = { before: folder, edge };
	}

	/** Every folder path in the order it is currently displayed. */
	function displayedFolders(nodes: TreeNode[], into: string[] = []): string[] {
		for (const node of nodes) {
			if (node.children === null) continue;
			into.push(node.path);
			displayedFolders(node.children, into);
		}
		return into;
	}

	/** Every note path in the order it is currently displayed. */
	function displayedNotes(nodes: TreeNode[], into: string[] = []): string[] {
		for (const node of nodes) {
			if (node.children === null) into.push(node.path);
			else displayedNotes(node.children, into);
		}
		return into;
	}

	/** Live note reordering, the task board's scheme: the target gap is the
	 *  count of settled sibling rows whose midpoint sits above the pointer,
	 *  measured with the dragged row excluded — a fixed point while the
	 *  preview shuffles, so the target cannot flap. */
	let noteDrop = $state<{ dir: string; index: number } | null>(null);

	function fileGapIndex(dir: string, y: number): number {
		// Tree rows only: the sticky Pinned copies sit above every folder and
		// would count as a sibling each, pushing the gap one row low apiece.
		const rows = [
			...document.querySelectorAll('nav.tree a.item.file:not(.pinned):not(.dragging)')
		].filter((el) => dirOf((el as HTMLElement).dataset.contextPath ?? '') === dir);
		return gapIndexAmong(rows, y);
	}

	/** Dragover for a whole level (nav root or a folder's contents). A note
	 *  over its OWN level is a reorder — handled here, on the container, so
	 *  the gaps between rows compute the same target as the rows themselves
	 *  instead of falling through to "move into folder" and snapping the
	 *  preview back. Anything else keeps the move-into behaviour. */
	function contentsDragOver(e: DragEvent, folder: string) {
		if (dragged !== null && dirOf(dragged) === folder) {
			e.preventDefault();
			e.stopPropagation();
			if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
			const index = fileGapIndex(folder, e.clientY);
			if (noteDrop?.dir !== folder || noteDrop.index !== index)
				noteDrop = { dir: folder, index };
			dropTarget = null;
			return;
		}
		dragOver(e, folder);
	}

	function contentsDrop(e: DragEvent, folder: string) {
		if (noteDrop !== null && dragged !== null && dirOf(dragged) === folder) {
			e.preventDefault();
			e.stopPropagation();
			void dropNoteAtGap();
			return;
		}
		drop(e, folder);
	}

	/** The sibling files as they will read if the drag drops here. */
	function previewFiles(nodes: TreeNode[]): TreeNode[] {
		const files = nodes.filter((n) => n.children === null);
		if (!dragged || !noteDrop) return files;
		const moving = files.find((n) => n.path === dragged);
		if (!moving || noteDrop.dir !== dirOf(dragged)) return files;
		const without = files.filter((n) => n.path !== dragged);
		const next = [...without];
		next.splice(Math.min(noteDrop.index, next.length), 0, moving);
		return next;
	}

	async function dropNoteAtGap() {
		const moving = dragged;
		const target = noteDrop;
		endDrag();
		// Under any other sort the rows show that sort, not the arrangement —
		// saving them would replace the arrangement with it.
		if (noteSort !== 'manual') return;
		if (!moving || !target) return;
		// Anchor on the sibling that precedes the gap, then lift-and-reinsert
		// in the saved arrangement — other groups keep what they had.
		const displayed = displayedNotes(tree);
		const group = displayed.filter((path) => path !== moving && dirOf(path) === target.dir);
		if (group.length === 0) return;
		const anchor =
			target.index > 0 ? group[Math.min(target.index, group.length) - 1] : null;
		try {
			await saveNoteOrder(
				moveNoteInOrder(vault.noteOrder, displayed, moving, anchor, target.dir)
			);
		} catch (err) {
			notify((err as Error).message, { tone: 'error' });
		}
	}

	async function dropFolder(e: DragEvent) {
		e.preventDefault();
		e.stopPropagation();
		const moving = draggedFolder;
		const slot = dropSlot;
		endDrag();
		if (!moving || !slot) return;

		// Only relative order within a sibling group is read back, so removing
		// the folder and re-inserting it next to the target is enough — other
		// groups keep the order they already had.
		const order = displayedFolders(tree).filter((path) => path !== moving);
		const at = order.indexOf(slot.before);
		if (at === -1) return;
		order.splice(slot.edge === 'above' ? at : at + 1, 0, moving);
		try {
			await saveFolderOrder(order);
		} catch (err) {
			notify((err as Error).message, { tone: 'error' });
		}
	}

	async function moveNote(path: string, folder: string) {
		if (dirOf(path) === folder) return;
		const name = path.split('/').pop()!;
		const target = folder ? `${folder}/${name}` : name;
		try {
			await api.renameNote(path, target);
			// The note page does this on its own renames; the sidebar never did,
			// so moving a note with unsaved edits stranded its draft under the
			// old path and orphaned its OMNIA conversation.
			await afterPathsMoved([[path, target]]);
			await refreshNotes();
			if (folder) expandTo(folder);
			if (current === path) await goto(`/note/${encodePath(target)}`);
		} catch (err) {
			notify((err as Error).message, { tone: 'error' });
		}
	}

	async function drop(e: DragEvent, folder: string) {
		if (draggedFolder !== null) return dropFolder(e);
		e.preventDefault();
		e.stopPropagation();
		const path = dragged;
		endDrag();
		if (path !== null) await moveNote(path, folder);
	}

	/** Touch drag for a note row: folders under the finger become the drop
	 *  target (the tree background means the vault root). Folder rows keep
	 *  their sibling-reorder drag mouse-only — the context menu covers it. */
	function touchDragNote(path: string) {
		return {
			disabled: () => ctxMenu !== null,
			onStart: () => (dragged = path),
			onMove: (x: number, y: number) => {
				const el = document.elementFromPoint(x, y);
				// A sibling note row means reordering, same as the mouse path.
				const noteRow = el?.closest('[data-context-kind="note"]') as HTMLElement | null;
				const over = noteRow?.dataset.contextPath;
				if (over && dirOf(over) === dirOf(path)) {
					// Only in manual order — elsewhere a sibling is no target at all.
					if (noteSort !== 'manual') {
						noteDrop = null;
						dropTarget = null;
						return;
					}
					const dir = dirOf(path);
					const index = fileGapIndex(dir, y);
					if (noteDrop?.dir !== dir || noteDrop.index !== index) noteDrop = { dir, index };
					dropTarget = null;
					return;
				}
				noteDrop = null;
				const row = el?.closest('[data-context-kind="folder"]') as HTMLElement | null;
				if (row) {
					dropTarget = row.dataset.contextPath ?? null;
					return;
				}
				dropTarget = el?.closest('nav.tree') ? '' : null;
			},
			onDrop: () => {
				if (noteDrop) {
					void dropNoteAtGap();
					return;
				}
				const target = dropTarget;
				const path_ = dragged;
				endDrag();
				if (path_ !== null && target !== null && dirOf(path_) !== target) {
					void moveNote(path_, target);
				}
			},
			onCancel: endDrag
		};
	}

	type MenuKind = 'note' | 'folder' | 'root';
	let ctxMenu = $state<{ x: number; y: number; path: string; kind: MenuKind } | null>(null);
	// The foot + swaps jobs on media routes: it files media, not notes.
	let mediaMenu = $state<{ x: number; y: number } | null>(null);
	let inMediaMode = $derived(page.url.pathname.startsWith('/media'));
	let moveOpen = $state(false);
	let tplOpen = $state(false);
	let contextMenuElement: HTMLDivElement | undefined = $state();

	function moveFromMenu(path: string, folder: string) {
		ctxMenu = null;
		moveNote(path, folder);
	}

	// Valid "Move to folder" destinations: never the item's own parent (no-op),
	// and a folder can't move into itself or its own subtree.
	function moveTargets(path: string, kind: MenuKind): string[] {
		const dir = dirOf(path);
		return vault.folders.filter((f) => {
			if (f === dir) return false;
			if (kind === 'folder' && (f === path || f.startsWith(path + '/'))) return false;
			return true;
		});
	}

	function contextParent(path: string, kind: MenuKind) {
		if (kind === 'folder') return path;
		if (kind === 'note') return dirOf(path);
		return '';
	}

	async function focusContextMenu() {
		await tick();
		contextMenuElement?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
	}

	function openContextMenu(x: number, y: number, path: string, kind: MenuKind) {
		moveOpen = false;
		tplOpen = false;
		const vw = window.innerWidth || document.documentElement.clientWidth;
		const vh = window.innerHeight || document.documentElement.clientHeight;
		ctxMenu = {
			x: Math.max(8, Math.min(x, vw - 198)),
			// Folder menus carry an extra row (New task board), so they need
			// more headroom than a note's before they run off the bottom.
			y: Math.max(8, Math.min(y, vh - (kind === 'root' ? 112 : kind === 'folder' ? 285 : 250))),
			path,
			kind
		};
		void focusContextMenu();
	}

	async function applyFolderMove(path: string, target: string) {
		try {
			await api.renameFolder(path, target);
			// Every note inside moved too: its draft, OMNIA conversation, tabs
			// and recents follow it, as they do for a single note's move.
			const inside = vault.notes.filter((n) => n.path.startsWith(path + '/'));
			await afterPathsMoved(
				inside.map((n): [string, string] => [n.path, target + n.path.slice(path.length)])
			);
			retargetTabs(path, target); // the folder's own board tab
			renameCollapsedPrefix(path, target);
			await refreshNotes();
			expandTo(target);
			// Follow the open note if it lived inside the moved folder.
			if (current.startsWith(path + '/')) {
				await goto(`/note/${encodePath(target + current.slice(path.length))}`);
			}
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function renameFolderFromMenu(path: string) {
		ctxMenu = null;
		const name = path.split('/').pop()!;
		const input = (
			await promptDialog('Choose a new folder name:', {
				title: 'Rename folder',
				value: name,
				confirmLabel: 'Rename'
			})
		)?.trim();
		if (!input || input === name) return;
		const dir = dirOf(path);
		const cleaned = input.replace(/\/+$/, '');
		const target = cleaned.includes('/') ? cleaned : dir ? `${dir}/${cleaned}` : cleaned;
		if (target !== path) await applyFolderMove(path, target);
	}

	function moveFolderFromMenu(path: string, destFolder: string) {
		ctxMenu = null;
		const name = path.split('/').pop()!;
		applyFolderMove(path, destFolder ? `${destFolder}/${name}` : name);
	}

	async function deleteFolderFromMenu(path: string) {
		ctxMenu = null;
		const inside = vault.notes.filter((n) => n.path.startsWith(path + '/')).map((n) => n.path);
		const count = inside.length;
		const what = count
			? `folder "${path}" and the ${count} note${count === 1 ? '' : 's'} inside`
			: `empty folder "${path}"`;
		if (
			!(await confirmDialog(`Move ${what} to Trash for 30 days?`, {
				title: 'Move folder to Trash',
				confirmLabel: 'Move to Trash',
				danger: true
			}))
		)
			return;
		try {
			await api.deleteFolder(path);
			await afterPathsRemoved(inside);
			closeTabsUnder(path); // the folder's own board tab
			await refreshNotes();
			if (current.startsWith(path + '/')) await goto('/');
			notify('Folder moved to Trash.', {
				tone: 'success',
				actionLabel: 'View Trash',
				action: () => goto('/trash')
			});
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function renameFromMenu(path: string) {
		ctxMenu = null;
		const name = path.split('/').pop()!.replace(/\.md$/, '');
		const input = (
			await promptDialog('Choose a new note name or path:', {
				title: 'Rename note',
				value: name,
				confirmLabel: 'Rename'
			})
		)?.trim();
		if (!input || input === name) return;
		// A plain name renames in place; a path with slashes moves too.
		const dir = dirOf(path);
		let target = input.includes('/') ? input : dir ? `${dir}/${input}` : input;
		if (!target.endsWith('.md')) target += '.md';
		if (target === path) return;
		try {
			await api.renameNote(path, target);
			await afterPathsMoved([[path, target]]);
			await refreshNotes();
			if (current === path) await goto(`/note/${encodePath(target)}`);
		} catch (err) {
			notify((err as Error).message, { tone: 'error' });
		}
	}

	function renameOnDoubleClick(event: MouseEvent, path: string) {
		event.preventDefault();
		event.stopPropagation();
		void renameFromMenu(path);
	}

	async function setPinFromMenu(path: string, pinned: boolean) {
		ctxMenu = null;
		try {
			await api.pinNote(path, pinned);
			await refreshNotes();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	function noteContextMenu(e: MouseEvent, path: string, kind: MenuKind = 'note') {
		e.preventDefault();
		e.stopPropagation();
		openContextMenu(e.clientX, e.clientY, path, kind);
	}

	function sidebarContextMenu(e: MouseEvent) {
		e.preventDefault();
		e.stopPropagation();
		openContextMenu(e.clientX, e.clientY, '', 'root');
	}

	function sidebarKeydown(e: KeyboardEvent) {
		if (e.key !== 'ContextMenu' && !(e.shiftKey && e.key === 'F10')) return;
		e.preventDefault();
		e.stopPropagation();
		const target = e.target as HTMLElement;
		const contextTarget = target.closest<HTMLElement>('[data-context-kind]');
		const kind = (contextTarget?.dataset.contextKind as MenuKind | undefined) ?? 'root';
		const path = contextTarget?.dataset.contextPath ?? '';
		const rect = contextTarget?.getBoundingClientRect() ?? target.getBoundingClientRect();
		openContextMenu(
			rect.left + Math.min(rect.width, 34),
			rect.top + Math.min(rect.height, 30),
			path,
			kind
		);
	}

	function contextMenuKeydown(e: KeyboardEvent) {
		const buttons = Array.from(
			contextMenuElement?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []
		);
		if (e.key === 'Escape') {
			e.preventDefault();
			ctxMenu = null;
			return;
		}
		if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key) || !buttons.length) return;
		e.preventDefault();
		const currentIndex = Math.max(
			0,
			buttons.indexOf(document.activeElement as HTMLButtonElement)
		);
		const nextIndex =
			e.key === 'Home'
				? 0
				: e.key === 'End'
					? buttons.length - 1
					: e.key === 'ArrowDown'
						? (currentIndex + 1) % buttons.length
						: (currentIndex - 1 + buttons.length) % buttons.length;
		buttons[nextIndex]?.focus();
	}

	function createNoteFromMenu(parent: string, template?: string) {
		// The argument is evaluated before the menu is torn down — reading the
		// menu-derived `parent` after `ctxMenu = null` explodes, because
		// template @consts re-derive from the now-null menu.
		ctxMenu = null;
		void newNote(parent, template);
	}

	function createFolderFromMenu(parent: string) {
		ctxMenu = null;
		void newFolder(parent);
	}

	function navigateBack() {
		onClose();
		window.history.back();
	}

	function navigateForward() {
		onClose();
		window.history.forward();
	}

	async function deleteFromMenu(path: string) {
		ctxMenu = null;
		if (
			!(await confirmDialog(`Move “${path}” to Trash for 30 days?`, {
				title: 'Move note to Trash',
				confirmLabel: 'Move to Trash',
				danger: true
			}))
		)
			return;
		try {
			await api.deleteNote(path);
			await afterPathsRemoved([path]);
			await refreshNotes();
			if (current === path) await goto('/');
			notify('Note moved to Trash.', {
				tone: 'success',
				actionLabel: 'View Trash',
				action: () => goto('/trash')
			});
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	/** Give a folder its own task board. No prompt: there is nothing to name,
	 * and only one board is allowed per folder. */
	async function newTaskBoard(folder: string) {
		ctxMenu = null;
		try {
			await api.createTaskBoard(folder);
			await refreshNotes();
			await goto(`/tasks/${encodePath(folder)}`);
			onClose();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	function openTaskBoard(folder: string) {
		goto(`/tasks/${encodePath(folder)}`);
		onClose();
	}

	async function newFolder(parent = '') {
		const name = await promptDialog(
			parent
				? `New folder inside ${parent}:`
				: 'Folder name or nested path:',
			{
				title: 'New folder',
				placeholder: parent ? 'folder name' : 'projects/2026',
				confirmLabel: 'Create'
			}
		);
		if (!name) return;
		const cleaned = name.replace(/\/+$/, '');
		const path = parent ? `${parent}/${cleaned}` : cleaned;
		try {
			await api.createFolder(path);
			await refreshNotes();
			if (parent) expandTo(parent);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

</script>

<aside
	bind:this={asideEl}
	class:mobile-open={mobileOpen}
	class:media-mode={inMediaMode}
	aria-label="Notebook navigation"
>
	<!-- Top-left of the window in the desktop app, so it doubles as title bar:
	     draggable, and inset on macOS to clear the traffic lights. -->
	<header data-tauri-drag-region={isDesktop ? '' : undefined}>
		<div class="header-leading">
			<a
				class="brand"
				href={page.url.pathname === '/settings' ? '/' : '/settings'}
				onclick={onClose}
				title={page.url.pathname === '/settings' ? 'Return to notebook' : 'Open Settings'}
			>
				EXSCALIA
			</a>
			<!-- Live with changes still waiting (a push that failed, an op the
			     server keeps refusing) shows too: 'live' is not 'synced'. -->
			{#if syncState.mode !== 'live' || syncState.pending > 0}
				{@const changes = `${syncState.pending} ${syncState.pending === 1 ? 'change' : 'changes'}`}
				<span
					class="sync-dot"
					class:busy={syncState.mode === 'connecting' || syncState.mode === 'replaying'}
					title={syncState.mode === 'offline'
						? syncState.pending > 0
							? `Offline — ${changes} queued`
							: 'Offline — reading from local copy'
						: syncState.mode === 'live'
							? `${changes} waiting to sync`
							: syncState.progressTotal > 0
								? `Syncing ${syncState.progressDone}/${syncState.progressTotal}`
								: 'Syncing'}
					aria-label="Sync status"
				></span>
			{/if}
			<!-- On media routes the tab strip carries these instead — the themed
			     pages are navigated grid ↔ item from up there. -->
			{#if !page.url.pathname.startsWith('/media')}
				<div class="history-actions" role="group" aria-label="Page history">
					<button
						type="button"
						class="history-button"
						aria-label="Go back to the previously visited page"
						title="Back"
						onclick={navigateBack}
					>
						←
					</button>
					<button
						type="button"
						class="history-button"
						aria-label="Go forward to the next visited page"
						title="Forward"
						onclick={navigateForward}
					>
						→
					</button>
				</div>
			{/if}
		</div>
		<div class="header-actions">
			<button
				class="collapse-btn"
				onclick={onCollapse}
				title="Hide sidebar"
				aria-label="Hide sidebar"
			>
				«
			</button>
		</div>
		<button class="mobile-close" type="button" aria-label="Close navigation" onclick={onClose}>✕</button>
	</header>

	<form class="search" onsubmit={submitSearch}>
		<input type="search" placeholder="Search notes…" bind:value={searchQ} />
	</form>

	<!-- Everything below the search scrolls as one column. On desktop this
	     wrapper is display:contents (invisible to layout, so the tree keeps its
	     own flex:1 scroll); on mobile it becomes the single scroll region, so
	     the universal links scroll away with the tree instead of pinning. -->
	<div class="rail-scroll">
	<div class="special" aria-label="Main sections">
		<a class="item special-link" class:active={page.url.pathname === '/omnia'} href="/omnia" onclick={onClose}>
			OMNIA
		</a>
		<a class="item special-link" class:active={page.url.pathname === '/calendar'} href="/calendar" onclick={onClose}>
			Calendar
		</a>
		<!-- Exact match: the universal board, never a folder's. The cast is
		     because /tasks is now served by the /tasks/[...folder] route, whose
		     generated type has no bare-path literal. -->
		<a
			class="item special-link"
			class:active={(page.url.pathname as string) === '/tasks'}
			href="/tasks"
			onclick={onClose}
		>
			Tasks
		</a>
		<a
			class="item special-link"
			class:active={page.url.pathname.startsWith('/media')}
			href="/media"
			onclick={onClose}
		>
			Media
		</a>
		<!-- Read-only here: notebooks are written with a Pencil on the iPad.
		     Inside the iPad shell the layout intercepts this link and hands
		     off to the native Paper section instead of navigating. -->
		<a
			class="item special-link"
			class:active={page.url.pathname.startsWith('/paper')}
			href="/paper"
			onclick={onClose}
		>
			Paper
		</a>
		{#if vault.pendingCount > 0}
			<!-- The count was already being fetched here and never shown, leaving
			     /pending reachable only by typing the URL. -->
			<a
				class="item special-link"
				class:active={page.url.pathname === '/pending'}
				href="/pending"
				onclick={onClose}
			>
				Review <span class="pending-count">{vault.pendingCount}</span>
			</a>
		{/if}
		<button
			type="button"
			class="item special-link"
			class:active={current === todayPath}
			aria-label="Open today's daily note"
			onclick={openToday}
		>
			Daily
		</button>
	</div>

	<div class="note-sort">
		<select
			class="note-sort-select"
			aria-label="Sort notes by"
			title="Sort notes by"
			value={noteSort}
			onchange={(e) => setNoteSort(e.currentTarget.value as NoteSort)}
		>
			{#each NOTE_SORTS as option (option.value)}
				<option value={option.value}>{option.label}</option>
			{/each}
		</select>
	</div>

	<nav
		class="tree"
		class:drag-active={dragged !== null}
		ondragover={(e) => contentsDragOver(e, '')}
		ondrop={(e) => contentsDrop(e, '')}
	>
		{#if pinnedNotes.length > 0}
			<div class="pinned-sticky">
			<button
				type="button"
				class="item folder"
				aria-expanded={!collapsed['::pinned']}
				onclick={() => toggleFolder('::pinned')}
			>
				<span class="chev" class:open={!collapsed['::pinned']}>›</span>
				Pinned
			</button>
			{#if !collapsed['::pinned']}
				<div class="folder-contents" transition:slide={{ duration: 160, easing: cubicOut }}>
					{#each pinnedNotes as n (n.path)}
						<a
							class="item file pinned"
							data-context-kind="note"
							data-context-path={n.path}
							class:active={n.path === current}
							href="/note/{encodePath(n.path)}"
							onclick={onClose}
							ondblclick={(e) => renameOnDoubleClick(e, n.path)}
							oncontextmenu={(e) => noteContextMenu(e, n.path)}
						>
							{n.name}
						</a>
					{/each}
				</div>
			{/if}
			</div>
		{/if}

		{#snippet levelView(nodes: TreeNode[], depth: number)}
			{#each nodes.filter((n) => n.children !== null) as node (node.path)}
				<div
					class="folder-row"
					data-context-kind="folder"
					data-context-path={node.path}
					class:drag-over={dropTarget === node.path && draggedFolder === null}
					class:dragging={draggedFolder === node.path}
					class:insert-above={dropSlot?.before === node.path && dropSlot.edge === 'above'}
					class:insert-below={dropSlot?.before === node.path && dropSlot.edge === 'below'}
					role="presentation"
					draggable="true"
					oncontextmenu={(e) => noteContextMenu(e, node.path, 'folder')}
					ondragstart={(e) => folderDragStart(e, node.path)}
					ondragend={endDrag}
					ondragover={(e) => {
						folderDragOver(e, node.path);
						dragOver(e, node.path);
					}}
					ondragleave={(e) => {
						// Crossing into the row's own buttons is not leaving it.
						const into = e.relatedTarget as Node | null;
						if (into && (e.currentTarget as HTMLElement).contains(into)) return;
						if (dropTarget === node.path) dropTarget = null;
						if (dropSlot?.before === node.path) dropSlot = null;
					}}
					ondrop={(e) => drop(e, node.path)}
				>
					<button
						type="button"
						class="item folder"
						style:padding-left="{10 + depth * 14}px"
						aria-expanded={!collapsed[node.path]}
						onclick={() => toggleFolder(node.path)}
					>
						<span class="chev" class:open={!collapsed[node.path]}>›</span>
						{node.name}
					</button>
					<span class="folder-actions">
						<button
							type="button"
							class="folder-action"
							title="New note in {node.path}"
							aria-label="New note in {node.path}"
							onclick={() => newNote(node.path)}>+</button
						>
						<button
							type="button"
							class="folder-action"
							title="New folder in {node.path}"
							aria-label="New folder in {node.path}"
							onclick={() => newFolder(node.path)}><FolderIcon /></button
						>
						{#if boardFolders.has(node.path)}
							<button
								type="button"
								class="folder-action"
								title="Task board for {node.path}"
								aria-label="Task board for {node.path}"
								onclick={() => openTaskBoard(node.path)}>☑</button
							>
						{/if}
					</span>
				</div>
				{#if !collapsed[node.path]}
					<div
						class="folder-contents"
						role="presentation"
						transition:slide={{ duration: 160, easing: cubicOut }}
						ondragover={(e) => contentsDragOver(e, node.path)}
						ondrop={(e) => contentsDrop(e, node.path)}
					>
						{@render levelView(node.children ?? [], depth + 1)}
					</div>
				{/if}
			{/each}
			<!-- Files in their own keyed each so animate:flip can slide them,
			     in the preview order while a sibling drag is aiming. -->
			{#each previewFiles(nodes) as node (node.path)}
				<a
					animate:flip={{ duration: 150 }}
					class="item file"
					data-context-kind="note"
					data-context-path={node.path}
					class:active={node.path === current}
					class:dragging={dragged === node.path}
					style:padding-left="{10 + depth * 14}px"
					href="/note/{encodePath(node.path)}"
					draggable={noteSort === 'manual'}
					use:touchDrag={touchDragNote(node.path)}
					onclick={onClose}
					ondblclick={(e) => renameOnDoubleClick(e, node.path)}
					oncontextmenu={(e) => noteContextMenu(e, node.path)}
					ondragstart={(e) => dragStart(e, node.path)}
					ondragend={endDrag}
				>
					{node.name.replace(/\.md$/, '')}
				</a>
			{/each}
		{/snippet}

		{@render levelView(tree, 0)}
		{#if vault.loaded && vault.notes.length === 0}
			<p class="empty">No notes yet.</p>
		{/if}
	</nav>

	<div class="add-anchor">
		<button
			type="button"
			class="history-button add-button"
			title={inMediaMode ? 'Add media' : 'New note or folder'}
			aria-label={inMediaMode ? 'Add media' : 'New note or folder'}
			aria-haspopup="menu"
			onclick={(e) => {
				e.stopPropagation();
				if (ctxMenu || mediaMenu) {
					ctxMenu = null;
					mediaMenu = null;
					return;
				}
				const box = (e.currentTarget as HTMLElement).getBoundingClientRect();
				if (inMediaMode) {
					// Media mode: the + files media, not notes.
					mediaMenu = { x: box.left, y: box.top - 76 };
					return;
				}
				// Anchored at the sidebar's foot, so the menu opens upward.
				openContextMenu(box.left, box.top - 160, '', 'root');
				focusContextMenu();
			}}
		>
			＋
		</button>
	</div>

	{#if mediaMenu}
		<div
			class="ctx-menu"
			style:left="{mediaMenu.x}px"
			style:top="{mediaMenu.y}px"
			role="menu"
			aria-label="Add media"
		>
			<button
				type="button"
				class="ctx-item"
				role="menuitem"
				onclick={() => {
					mediaMenu = null;
					openMediaAdd('active');
					onClose();
				}}
			>
				<span class="ctx-icon" aria-hidden="true">＋</span> Add media
			</button>
			<button
				type="button"
				class="ctx-item"
				role="menuitem"
				onclick={() => {
					mediaMenu = null;
					openMediaAdd('planned');
					onClose();
				}}
			>
				<span class="ctx-icon" aria-hidden="true">＋</span> Add to watchlist
			</button>
		</div>
	{/if}

	{#if vault.tags.length > 0}
		<div class="tags-section">
			<button
				type="button"
				class="item folder"
				aria-expanded={!collapsed['::tags']}
				onclick={() => toggleFolder('::tags')}
			>
				<span class="chev" class:open={!collapsed['::tags']}>›</span>
				Tags
			</button>
			{#if !collapsed['::tags']}
				<div
					class="tag-cloud folder-contents"
					transition:slide={{ duration: 160, easing: cubicOut }}
				>
					{#each vault.tags as t (t.tag)}
						<a class="tag-chip" href="/tags/{encodeURIComponent(t.tag)}" onclick={onClose}>
							#{t.tag} <span class="count">{t.count}</span>
						</a>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
	</div>
</aside>

<svelte:window
	onclick={() => {
		ctxMenu = null;
		mediaMenu = null;
	}}
	oncontextmenu={(e) => {
		const target = e.target;
		if (target instanceof Element && target.closest('aside[aria-label="Notebook navigation"]')) {
			sidebarContextMenu(e);
		} else {
			ctxMenu = null;
		}
	}}
	onkeydown={(e) => {
		if (e.key === 'Escape' && ctxMenu) {
			// Consumed: the layout's Escape (the briefing) must not act too.
			e.preventDefault();
			ctxMenu = null;
		}
		const target = e.target;
		if (target instanceof Element && target.closest('aside[aria-label="Notebook navigation"]')) {
			sidebarKeydown(e);
		}
	}}
/>

{#if ctxMenu}
	{@const menu = ctxMenu}
	{@const isNote = menu.kind === 'note'}
	{@const parent = contextParent(menu.path, menu.kind)}
	<div
		class="ctx-menu"
		style:left="{menu.x}px"
		style:top="{menu.y}px"
		role="menu"
		aria-label="Sidebar actions"
		tabindex="-1"
		bind:this={contextMenuElement}
		onkeydown={contextMenuKeydown}
	>
		<!-- Creation belongs to places (root, folders), not to a note row —
		     right-clicking a note shows only actions on that note. -->
		{#if !isNote}
		<button type="button" class="ctx-item" role="menuitem" onclick={() => createNoteFromMenu(parent)}>
			<span class="ctx-icon" aria-hidden="true">＋</span> New note{parent ? ' here' : ''}
		</button>
		{#if templateNames.length > 0}
			<button
				type="button"
				class="ctx-item"
				role="menuitem"
				aria-expanded={tplOpen}
				onclick={(e) => {
					e.stopPropagation();
					tplOpen = !tplOpen;
				}}
			>
				<span class="ctx-icon" aria-hidden="true">＋</span> New from template
				<span class="ctx-chev">{tplOpen ? '▾' : '▸'}</span>
			</button>
			{#if tplOpen}
				<div class="ctx-folders">
					{#each templateNames as t (t)}
						<button
							type="button"
							class="ctx-item"
							role="menuitem"
							onclick={() => createNoteFromMenu(parent, t)}
						>
							{t}
						</button>
					{/each}
				</div>
			{/if}
		{/if}
		<button type="button" class="ctx-item" role="menuitem" onclick={() => createFolderFromMenu(parent)}>
			<span class="ctx-icon" aria-hidden="true"><FolderIcon /></span> New folder{parent
				? ' here'
				: ''}
		</button>
		{#if menu.kind === 'folder' && !boardFolders.has(menu.path)}
			<!-- Absent once the folder has one: the limit of one board per
			     folder should be visible, not just a rejected request. -->
			<button
				type="button"
				class="ctx-item"
				role="menuitem"
				onclick={() => newTaskBoard(menu.path)}
			>
				<span class="ctx-icon" aria-hidden="true">☑</span> New task board
			</button>
		{/if}
		{/if}
		{#if menu.kind !== 'root'}
			{#if !isNote}
				<div class="ctx-separator" role="separator"></div>
			{/if}
			{#if isNote}
				<!-- Same dedup as cmd+click: an already-open note gains no twin. -->
				<button
					type="button"
					class="ctx-item"
					role="menuitem"
					onclick={() => {
						openBackgroundTab(`/note/${encodePath(menu.path)}`);
						ctxMenu = null;
					}}
				>
					Open in new tab
				</button>
				{@const menuNotePinned = pinnedNotes.some((n) => n.path === menu.path)}
				<!-- Like the one-board-per-folder rule: the 3-pin limit shows as an
				     absent Pin item, not a rejected click. -->
				{#if menuNotePinned}
					<button
						type="button"
						class="ctx-item"
						role="menuitem"
						onclick={() => setPinFromMenu(menu.path, false)}
					>
						Unpin
					</button>
				{:else if pinnedNotes.length < 3}
					<button
						type="button"
						class="ctx-item"
						role="menuitem"
						onclick={() => setPinFromMenu(menu.path, true)}
					>
						Pin
					</button>
				{/if}
			{/if}
			<button
				type="button"
				class="ctx-item"
				role="menuitem"
				onclick={() => (isNote ? renameFromMenu(menu.path) : renameFolderFromMenu(menu.path))}
			>
				Rename…
			</button>
			<button
				type="button"
				class="ctx-item"
				role="menuitem"
				aria-expanded={moveOpen}
				onclick={(e) => {
					e.stopPropagation();
					moveOpen = !moveOpen;
				}}
			>
				Move to folder <span class="ctx-chev">{moveOpen ? '▾' : '▸'}</span>
			</button>
			{#if moveOpen}
				<div class="ctx-folders">
					{#if dirOf(menu.path)}
						<button
							type="button"
							class="ctx-item"
							role="menuitem"
							onclick={() =>
								isNote ? moveFromMenu(menu.path, '') : moveFolderFromMenu(menu.path, '')}
						>
							Vault root
						</button>
					{/if}
					{#each moveTargets(menu.path, menu.kind) as f (f)}
						<button
							type="button"
							class="ctx-item"
							role="menuitem"
							onclick={() =>
								isNote ? moveFromMenu(menu.path, f) : moveFolderFromMenu(menu.path, f)}
						>
							{f}
						</button>
					{/each}
				</div>
			{/if}
			<button
				type="button"
				class="ctx-item danger"
				role="menuitem"
				onclick={() => (isNote ? deleteFromMenu(menu.path) : deleteFolderFromMenu(menu.path))}
			>
				Delete “{menu.path.split('/').pop()?.replace(/\.md$/, '')}”
			</button>
		{/if}
	</div>
{/if}

<style>
	aside {
		width: var(--sidebar-w);
		flex-shrink: 0;
		background: var(--bg-panel);
		border-right: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		height: 100%;
		transition:
			background-color var(--motion-slow) var(--ease-out),
			border-color var(--motion-base) var(--ease-out);
	}
	/* Media mode: the rail drops its panel background and joins the media
	   pages' standard black, so the whole app reads as one surface. The
	   scoped custom props re-skin the search box, rows, and hover pills to
	   match (same values .detail pins). */
	aside.media-mode {
		--bg: #000000;
		--bg-panel: #17171a;
		--border: #2a2a2e;
		--bg-hover: color-mix(in srgb, #ffffff 7%, transparent);
		background: var(--bg);
		border-right-color: var(--border);
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 12px 10px;
		border-bottom: 1px solid var(--border);
	}
	/* In the desktop app this row *is* the title bar, so it takes the shared
	   height — the tab strip beside it uses the same one. */
	:global(html[data-desktop]) header {
		height: var(--titlebar-h);
		padding-top: 0;
		padding-bottom: 0;
		box-sizing: border-box;
	}
	/* macOS floats its traffic lights over this corner; leave them the room. */
	:global(html[data-desktop='mac']) header {
		padding-left: 78px;
	}
	.brand {
		font-weight: 700;
		font-size: 15px;
		color: var(--text);
		letter-spacing: 0.2px;
		display: inline-block;
		text-decoration: none;
		transition:
			color var(--motion-fast) var(--ease-out),
			opacity var(--motion-fast) var(--ease-out);
	}
	.brand:hover {
		color: var(--accent);
	}
	.brand:active {
		opacity: 0.72;
	}
	.sync-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		border: 1.5px solid var(--muted);
		display: inline-block;
		flex: none;
	}
	.sync-dot.busy {
		border-color: var(--accent);
		animation: sync-pulse 1.2s ease-in-out infinite;
	}
	@keyframes sync-pulse {
		50% {
			opacity: 0.35;
		}
	}
	.header-leading,
	.history-actions {
		display: flex;
		align-items: center;
	}
	.header-leading {
		min-width: 0;
		gap: 8px;
	}
	.history-actions {
		gap: 1px;
		flex-shrink: 0;
	}
	.history-button {
		width: 25px;
		height: 27px;
		padding: 0;
		border: 0;
		border-radius: 6px;
		color: var(--text-muted);
		font-size: 17px;
		line-height: 1;
	}
	.history-button:not(:disabled):hover,
	.history-button:focus-visible {
		color: var(--accent);
		background: var(--bg-hover);
	}
	/* Transparent on desktop: the tree keeps its own flex:1 scroll and the
	   universal links stay pinned above it. Mobile overrides this into the
	   sidebar's single scroll region (see the max-width block). */
	.rail-scroll {
		display: contents;
	}
	nav {
		flex: 1;
		overflow-y: auto;
		padding: 8px 6px;
	}
	.item {
		display: block;
		padding: 3px 10px;
		border-radius: 6px;
		font-size: 13.5px;
		color: var(--text);
		text-decoration: none;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		position: relative;
		transition:
			background-color var(--motion-base) var(--ease-out),
			color var(--motion-base) var(--ease-out),
			box-shadow var(--motion-base) var(--ease-out);
	}
	/* Hover keeps rows still — the sliding text read as a bump. */
	.item.file:hover {
		background: var(--bg-hover);
	}
	.item.active {
		background: var(--bg-active);
		color: var(--accent);
	}
	.folder {
		color: var(--text-muted);
		font-weight: 600;
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		margin-top: 6px;
		width: 100%;
		text-align: left;
		display: flex;
		align-items: center;
		gap: 4px;
		cursor: pointer;
	}
	.folder:not(:disabled):hover {
		color: var(--text);
		background: var(--bg-hover);
	}
	.chev {
		font-size: 15px;
		line-height: 1;
		width: 10px;
		flex-shrink: 0;
		transform: rotate(0deg);
		transform-origin: center;
		transition: transform var(--motion-base) var(--ease-snap);
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.folder-contents {
		transform-origin: top;
	}
.pinned-sticky {
		position: sticky;
		top: 0;
		z-index: 3;
		background: var(--bg-panel);
	}
	aside.media-mode .pinned-sticky {
		background: var(--bg);
	}
	.folder-row {
		display: flex;
		align-items: center;
		border-radius: 6px;
		position: relative;
	}
	.folder-row.drag-over {
		background: var(--accent-soft);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	/* Insertion line for folder reordering: a rule where the folder will land,
	   rather than highlighting the row it is passing over — that would read as
	   "drop into", which is not what this does. */
	.folder-row.insert-above::before,
	.folder-row.insert-below::after {
		content: '';
		position: absolute;
		left: 6px;
		right: 6px;
		height: 2px;
		border-radius: 1px;
		background: var(--accent);
		pointer-events: none;
	}
	.folder-row.insert-above::before {
		top: -1px;
	}
	.folder-row.insert-below::after {
		bottom: -1px;
	}
	.item.file.dragging,
	.folder-row.dragging {
		opacity: 0.45;
	}
	nav.drag-active {
		box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 35%, transparent);
		border-radius: 6px;
	}
	.folder-row .folder {
		flex: 1;
		min-width: 0;
	}
	.folder-actions {
		display: flex;
		gap: 2px;
		flex-shrink: 0;
		opacity: 0;
		transition: opacity var(--motion-base) var(--ease-out);
	}
	.folder-row:hover .folder-actions,
	.folder-actions:focus-within {
		opacity: 1;
	}
	.folder-action {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 20px;
		height: 20px;
		padding: 0 4px;
		font-size: 12px;
		line-height: 20px;
		color: var(--text-muted);
		background: none;
		border: none;
		border-radius: 5px;
		cursor: pointer;
	}
	.folder-action:hover {
		color: var(--text);
		background: var(--bg-hover);
	}
	.empty {
		color: var(--text-muted);
		font-size: 13px;
		padding: 10px;
	}
	.header-actions {
		display: flex;
		gap: 4px;
	}
	.search {
		padding: 10px 12px 4px;
	}
	.search input {
		width: 100%;
		font-size: 13px;
	}
	.special {
		flex-shrink: 0;
		padding: 6px 6px 8px;
		border-bottom: 1px solid var(--border);
		margin-bottom: 2px;
	}
	.pending-count {
		margin-left: 4px;
		padding: 0 6px;
		border-radius: 999px;
		background: var(--accent);
		color: var(--accent-contrast);
		font-size: 10.5px;
		font-weight: 700;
	}
	.special-link {
		width: 100%;
		border: 0;
		font-weight: 600;
		display: flex;
		align-items: center;
		gap: 6px;
		text-align: left;
	}
	.special-link:hover {
		background: var(--bg-hover);
	}
	/* The notes sort dropdown: sits below the universal links, above the tree. */
	.note-sort {
		padding: 2px 8px 6px;
		flex-shrink: 0;
	}
	.note-sort-select {
		font: inherit;
		font-size: 11px;
		color: var(--text-muted);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 3px 8px;
		cursor: pointer;
	}
	.note-sort-select:hover {
		color: var(--text);
		border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
	}
	/* The + sits at the sidebar's foot, above the tags, wearing the history
	   buttons' look; a hair larger so the glyph reads at their size. No
	   divider — the tags section below already draws one. */
	.add-anchor {
		flex-shrink: 0;
		padding: 4px 8px;
	}
	.add-button {
		font-size: 14px;
	}
	.tags-section {
		flex-shrink: 0;
		max-height: 45%;
		overflow-y: auto;
		padding: 2px 6px 8px;
		border-top: 1px solid var(--border);
	}
	.tag-cloud {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		padding: 6px 10px;
	}
	.tag-chip {
		font-size: 12px;
		color: var(--accent);
		background: var(--accent-soft);
		border-radius: 999px;
		padding: 1px 8px;
		text-decoration: none;
		transition:
			background-color var(--motion-base) var(--ease-out),
			color var(--motion-base) var(--ease-out);
	}
	.tag-chip:hover {
		background: var(--bg-active);
	}
	.tag-chip .count {
		color: var(--text-muted);
	}
	.mobile-close {
		display: none;
	}
	.collapse-btn {
		padding: 0 7px;
		color: var(--text-muted);
	}
	.collapse-btn:not(:disabled):hover {
		color: var(--accent);
	}
	.ctx-menu {
		position: fixed;
		z-index: 80;
		min-width: 170px;
		padding: 4px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg-panel);
		transform-origin: top left;
		animation: ctx-enter var(--motion-fast) var(--ease-out);
	}
	.ctx-item {
		display: block;
		width: 100%;
		padding: 6px 10px;
		border: none;
		border-radius: 6px;
		background: none;
		text-align: left;
		font-size: 13px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		cursor: pointer;
	}
	.ctx-item:hover,
	.ctx-item:focus-visible {
		background: var(--bg-hover);
	}
	.ctx-item:focus-visible {
		color: var(--accent);
		outline: none;
	}
	.ctx-item.danger {
		color: var(--danger);
	}
	.ctx-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		color: var(--text-muted);
		text-align: center;
	}
	.ctx-separator {
		height: 1px;
		margin: 4px 5px;
		background: var(--border);
	}
	.ctx-chev {
		float: right;
		color: var(--text-muted);
	}
	.ctx-folders {
		max-height: 200px;
		overflow-y: auto;
		margin: 2px 0;
		padding-left: 10px;
		border-left: 2px solid var(--border);
	}
	@keyframes ctx-enter {
		from {
			opacity: 0;
			transform: translateY(-3px) scale(0.98);
		}
		to {
			opacity: 1;
			transform: translateY(0) scale(1);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.ctx-menu {
			animation: none;
		}
	}

	@media (max-width: 760px) {
		aside {
			position: fixed;
			inset: 0 auto 0 0;
			z-index: var(--z-drawer);
			width: min(88vw, 340px);
			height: 100dvh;
			padding-top: env(safe-area-inset-top);
			box-shadow: 18px 0 48px rgb(0 0 0 / 22%);
			transform: translateX(-105%);
			visibility: hidden;
			transition:
				transform 240ms var(--ease-out),
				visibility 0s linear 240ms,
				background-color var(--motion-slow) var(--ease-out),
				border-color var(--motion-base) var(--ease-out);
		}
		aside.mobile-open {
			transform: translateX(0);
			visibility: visible;
			transition-delay: 0s;
		}
		header {
			position: relative;
			min-height: 94px;
			padding: 10px 50px 10px 14px;
			align-items: flex-start;
			flex-direction: column;
			gap: 8px;
		}
		.brand {
			font-size: 16px;
		}
		.header-leading {
			gap: 10px;
		}
		.history-button {
			width: 44px;
			height: 44px;
			font-size: 20px;
		}
		.header-actions button {
			min-height: 38px;
		}
		/* The drawer has its own close button; desktop collapse is meaningless here. */
		.collapse-btn {
			display: none;
		}
		.mobile-close {
			display: block;
			position: absolute;
			top: 6px;
			right: 6px;
			width: 44px;
			height: 44px;
			padding: 0;
			color: var(--text-muted);
		}
		.search {
			padding: 10px 12px 7px;
		}
		.search input {
			min-height: 44px;
			font-size: var(--m-row-text);
		}
		/* Under 16px iOS zooms the whole page as soon as the picker opens, and
		   the drawer is left scrolled sideways behind it. */
		.note-sort-select {
			padding: 0 10px;
			font-size: var(--m-row-text);
		}
		/* The whole rail below the search becomes one scroll region, so the
		   universal links and note-sort scroll away with the tree — no longer
		   pinned to the top on a small screen. */
		.rail-scroll {
			display: flex;
			flex-direction: column;
			flex: 1;
			min-height: 0;
			overflow-y: auto;
			-webkit-overflow-scrolling: touch;
		}
		nav {
			flex: 0 0 auto;
			overflow-y: visible;
			padding: 8px;
		}
		.special {
			padding: 6px 8px 8px;
		}
		.tags-section {
			max-height: none;
			overflow-y: visible;
			padding: 2px 8px calc(12px + env(safe-area-inset-bottom));
		}
		/* A 48px row at the shared row text size. The padding and line-height
		   add to exactly the min-height, so the label sits centred in the
		   target instead of riding at the top of it. */
		.item {
			min-height: 48px;
			padding-top: 14px;
			padding-bottom: 14px;
			font-size: var(--m-row-text);
			line-height: 20px;
		}
		/* The app-wide mobile section label. It follows .item in source on
		   purpose: a folder row keeps the 48px target but not the row type. */
		.folder {
			font-size: var(--m-label);
			font-weight: 600;
			letter-spacing: 0.5px;
		}
		/* No hover on touch — keep the per-folder actions visible. */
		.folder-actions {
			opacity: 1;
		}
		.folder-action {
			min-height: 44px;
			min-width: 44px;
			font-size: 14px;
		}
		.tag-chip {
			display: inline-flex;
			align-items: center;
			min-height: 36px;
			padding: 0 12px;
			font-size: var(--m-label);
		}
	}
</style>
