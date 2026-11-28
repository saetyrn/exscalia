<!--
	Music, Deploy 2: the plain library page (MUSIC.md). The saetyrn.com drum
	and the 3D stage replace this look in Deploy 4; the behaviour here (liked
	part, numbers, dragging, menus, uploads, playlists, trash) carries over.

	Where you are rides in the query (?folder=, ?playlist=, ?view=liked |
	playlists | trash), so moving around never remounts the page.
-->
<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import Glyph from '$lib/Glyph.svelte';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';
	import { musicApi } from '$lib/music/api';
	import EditDialog from '$lib/music/EditDialog.svelte';
	import { coverIn, coverUrl, downloadUrl, formatTime, library } from '$lib/music/library.svelte';
	import Menu, { type MenuItem } from '$lib/music/Menu.svelte';
	import Picker from '$lib/music/Picker.svelte';
	import { player } from '$lib/music/player.svelte';
	import { filesFromDrop, filesFromInput, formatBytes, uploads } from '$lib/music/uploads.svelte';
	import type { MusicFolder, MusicPlaylist, MusicSong } from '$lib/music/types';

	type Context =
		| { type: 'regular'; folder: string }
		| { type: 'folder-liked'; folder: string }
		| { type: 'liked' }
		| { type: 'playlist'; playlist: string }
		| { type: 'trash' };

	type Dragging =
		| { kind: 'song'; id: string; ctx: Context }
		| { kind: 'folder'; id: string }
		| { kind: 'playlist'; id: string };

	const DRAG_TYPE = 'application/x-exscalia-music';

	onMount(() => {
		void library.ensure();
	});

	const params = $derived(page.url.searchParams);
	const folderId = $derived(params.get('folder'));
	const playlistId = $derived(params.get('playlist'));
	const view = $derived(
		folderId ? 'folder' : playlistId ? 'playlist' : (params.get('view') ?? 'root')
	);
	const folder = $derived(folderId ? library.folders[folderId] : undefined);
	const playlist = $derived(playlistId ? library.playlists[playlistId] : undefined);
	const trail = $derived(library.trail(folderId));
	const subfolders = $derived(
		(folder ? folder.children : view === 'root' ? library.root : [])
			.map((id) => library.folders[id])
			.filter((f): f is MusicFolder => !!f)
	);
	const pinned = $derived(Object.values(library.folders).filter((f) => f.pinned));
	const playlists = $derived(
		library.playlistOrder.map((id) => library.playlists[id]).filter((p): p is MusicPlaylist => !!p)
	);
	const pick = (ids: string[]) => ids.map((id) => library.songs[id]).filter((s): s is MusicSong => !!s && !s.trashed);
	const regular = $derived(folder ? pick(folder.songs) : []);
	const likedPart = $derived(folder ? pick(folder.liked) : []);
	const allLiked = $derived(pick(library.liked));
	const playlistSongs = $derived(playlist ? pick(playlist.songs) : []);

	let menu = $state<{ x: number; y: number; items: MenuItem[] } | null>(null);
	let editing = $state<{ kind: 'song' | 'folder' | 'playlist'; id: string } | null>(null);
	let picker = $state<{ title: string; options: { id: string; label: string }[]; onpick: (id: string) => void; oncreate?: (name: string) => void } | null>(null);
	let dragging = $state<Dragging | null>(null);
	let dropMark = $state<string | null>(null);
	let flash = $state<string | null>(null);
	let previewBackground = $state<MusicPlaylist | null>(null);
	let fileInput: HTMLInputElement | undefined = $state();
	let folderInput: HTMLInputElement | undefined = $state();

	const background = $derived(view === 'playlist' ? playlist : view === 'playlists' ? previewBackground : null);

	// -- navigation ---------------------------------------------------------------

	function go(query: Record<string, string> | null) {
		const url = new URL(page.url);
		url.search = '';
		for (const [key, value] of Object.entries(query ?? {})) url.searchParams.set(key, value);
		void goto(url, { keepFocus: true });
	}

	function up() {
		if (folderId) go(library.parents[folderId] ? { folder: library.parents[folderId]! } : null);
		else if (view === 'playlist') go({ view: 'playlists' });
		else if (view !== 'root') go(null);
	}

	// -- playing ------------------------------------------------------------------

	function queueFor(ctx: Context): { ids: string[]; context: string | null } {
		switch (ctx.type) {
			case 'regular':
				return { ids: regular.map((s) => s.id), context: ctx.folder };
			case 'folder-liked': {
				// The liked part, then the rest of the folder without repeats.
				const liked = likedPart.map((s) => s.id);
				const rest = regular.map((s) => s.id).filter((id) => !liked.includes(id));
				return { ids: [...liked, ...rest], context: ctx.folder };
			}
			case 'liked':
				return { ids: allLiked.map((s) => s.id), context: null };
			case 'playlist':
				return { ids: playlistSongs.map((s) => s.id), context: null };
			default:
				return { ids: [], context: null };
		}
	}

	function play(song: MusicSong, ctx: Context) {
		if (song.missing || ctx.type === 'trash') return;
		const { ids, context } = queueFor(ctx);
		if (player.current === song.id && player.context === context) return player.toggle();
		player.playList(ids, ids.indexOf(song.id), context);
	}

	// -- changes --------------------------------------------------------------------

	async function act(work: () => Promise<unknown>, done?: string) {
		try {
			await work();
			await library.load();
			if (done) notify(done);
		} catch (err) {
			notify(err instanceof Error ? err.message : String(err), { tone: 'error' });
			void library.load();
		}
	}

	function folderOptions(exclude?: string) {
		return library
			.allFolders()
			.filter(({ folder }) => folder.id !== exclude)
			.map(({ folder, path }) => ({ id: folder.id, label: path }));
	}

	function addToPlaylist(ids: string[]) {
		picker = {
			title: 'Add to playlist',
			options: playlists.map((p) => ({ id: p.id, label: p.name })),
			onpick: (id) => act(() => musicApi.playlistAdd(id, ids)),
			oncreate: (name) => act(() => musicApi.createPlaylist(name, ids))
		};
	}

	async function deleteSongs(ids: string[]) {
		const ok = await confirmDialog(
			ids.length === 1 ? 'Move this song to the trash?' : `Move ${ids.length} songs to the trash?`,
			{ title: 'Delete', confirmLabel: 'Delete', danger: true }
		);
		if (ok) await act(() => musicApi.trash(ids));
	}

	function songMenu(event: { clientX: number; clientY: number }, song: MusicSong, ctx: Context) {
		const ids = [song.id];
		const items: MenuItem[] = [];
		if (ctx.type === 'trash') {
			items.push({ label: 'Restore', run: () => act(() => musicApi.restore(ids)) });
			items.push({
				label: 'Delete for good',
				danger: true,
				run: async () => {
					if (await confirmDialog('Delete this song and its audio for good?', { title: 'Delete for good', confirmLabel: 'Delete', danger: true }))
						await act(() => musicApi.emptyTrash(ids));
				}
			});
		} else {
			items.push(
				{ label: 'Play next', run: () => player.playNext(ids), disabled: song.missing },
				{ label: 'Add to queue', run: () => player.addToQueue(ids), disabled: song.missing },
				{ label: 'Add to playlist…', run: () => addToPlaylist(ids) }
			);
			if (ctx.type === 'regular' || ctx.type === 'folder-liked') {
				const from = ctx.folder;
				items.push({
					label: 'Move to…',
					run: () =>
						(picker = {
							title: 'Move to',
							options: folderOptions(from),
							onpick: (target) => act(() => musicApi.move({ ids, target, source: from }))
						})
				});
			}
			items.push(
				{
					label: 'Add to folder…',
					run: () =>
						(picker = {
							title: 'Add to folder',
							options: folderOptions(),
							onpick: (target) => act(() => musicApi.move({ ids, target, copy: true }))
						})
				},
				{ label: song.liked ? 'Unlike' : 'Like', run: () => act(() => musicApi.like(song.id, !song.liked)) },
				{ label: 'Edit', run: () => (editing = { kind: 'song', id: song.id }) },
				{ label: 'Download', run: () => download(song) }
			);
			if (ctx.type === 'regular' || ctx.type === 'folder-liked') {
				const from = ctx.folder;
				items.push({ label: 'Remove from this folder', run: () => act(() => musicApi.removeFromFolder(from, ids)) });
			}
			if (ctx.type === 'playlist') {
				const from = ctx.playlist;
				items.push({ label: 'Remove from this playlist', run: () => act(() => musicApi.playlistRemove(from, ids)) });
			}
			items.push({ label: 'Delete', danger: true, run: () => deleteSongs(ids) });
		}
		menu = { x: event.clientX, y: event.clientY, items };
	}

	function folderMenu(event: { clientX: number; clientY: number }, f: MusicFolder) {
		menu = {
			x: event.clientX,
			y: event.clientY,
			items: [
				{ label: 'Open', run: () => go({ folder: f.id }) },
				{ label: 'Edit', run: () => (editing = { kind: 'folder', id: f.id }) },
				{ label: 'New folder inside', run: () => newFolder(f.id) },
				{ label: f.pinned ? 'Unpin' : 'Pin to the top', run: () => act(() => musicApi.editFolder(f.id, { pinned: !f.pinned })) },
				{
					label: 'Move to…',
					run: () =>
						(picker = {
							title: `Move ${f.name} to`,
							options: [{ id: '', label: 'Music (the top level)' }, ...folderOptions(f.id)],
							onpick: (target) => act(() => musicApi.moveFolder(f.id, target || null))
						})
				},
				{
					label: 'Delete',
					danger: true,
					run: async () => {
						if (await confirmDialog(`Delete ${f.name} and the folders in it? Songs that are in no other folder go to the trash.`, { title: 'Delete folder', confirmLabel: 'Delete', danger: true }))
							await act(() => musicApi.deleteFolder(f.id));
					}
				}
			]
		};
	}

	function playlistMenu(event: { clientX: number; clientY: number }, p: MusicPlaylist) {
		menu = {
			x: event.clientX,
			y: event.clientY,
			items: [
				{ label: 'Play', run: () => player.playList(pick(p.songs).map((s) => s.id), 0, null) },
				{ label: 'Edit', run: () => (editing = { kind: 'playlist', id: p.id }) },
				{
					label: 'Delete',
					danger: true,
					run: async () => {
						if (await confirmDialog(`Delete the playlist ${p.name}? Its songs stay in their folders.`, { title: 'Delete playlist', confirmLabel: 'Delete', danger: true }))
							await act(() => musicApi.deletePlaylist(p.id));
					}
				}
			]
		};
	}

	async function newFolder(parent: string | null) {
		const name = await promptDialog('', { title: 'New folder', placeholder: 'Name', confirmLabel: 'Create' });
		if (name?.trim()) await act(() => musicApi.createFolder(name.trim(), parent));
	}

	async function newPlaylist() {
		const name = await promptDialog('', { title: 'New playlist', placeholder: 'Name', confirmLabel: 'Create' });
		if (name?.trim()) await act(() => musicApi.createPlaylist(name.trim()));
	}

	function download(song: MusicSong) {
		const link = document.createElement('a');
		link.href = downloadUrl(song.id);
		link.download = song.file;
		link.click();
	}

	async function jumpTo(songId: string) {
		const row = document.querySelector<HTMLElement>(`[data-regular="${CSS.escape(songId)}"]`);
		if (!row) return;
		row.scrollIntoView({ behavior: 'smooth', block: 'center' });
		flash = songId;
		await tick();
		setTimeout(() => (flash = null), 1400);
	}

	// -- long press (touch) ---------------------------------------------------------

	let press: { timer: ReturnType<typeof setTimeout>; x: number; y: number } | null = null;
	function onPressStart(event: PointerEvent, open: (e: { clientX: number; clientY: number }) => void) {
		if (event.pointerType !== 'touch') return;
		const { clientX, clientY } = event;
		press = { x: clientX, y: clientY, timer: setTimeout(() => { press = null; open({ clientX, clientY }); }, 500) };
	}
	function onPressMove(event: PointerEvent) {
		if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) > 10) onPressEnd();
	}
	function onPressEnd() {
		if (press) clearTimeout(press.timer);
		press = null;
	}

	// -- dragging -------------------------------------------------------------------

	function startDrag(event: DragEvent, drag: Dragging) {
		dragging = drag;
		event.dataTransfer?.setData(DRAG_TYPE, drag.id);
		event.dataTransfer!.effectAllowed = 'copyMove';
	}

	function endDrag() {
		dragging = null;
		dropMark = null;
	}

	function isFiles(event: DragEvent) {
		const types = event.dataTransfer?.types ?? [];
		return !dragging && Array.from(types).includes('Files');
	}

	function half(event: DragEvent): 'before' | 'after' {
		const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
		return event.clientY < rect.top + rect.height / 2 ? 'before' : 'after';
	}

	function reordered(ids: string[], moving: string, target: string, where: 'before' | 'after') {
		const out = ids.filter((id) => id !== moving);
		const at = out.indexOf(target);
		out.splice(where === 'before' ? at : at + 1, 0, moving);
		return out;
	}

	function sameList(a: Context, b: Context) {
		return JSON.stringify(a) === JSON.stringify(b);
	}

	function overRow(event: DragEvent, song: MusicSong, ctx: Context) {
		if (dragging?.kind === 'song' && sameList(dragging.ctx, ctx) && dragging.id !== song.id) {
			event.preventDefault();
			dropMark = `${song.id}:${half(event)}`;
		}
	}

	function dropOnRow(event: DragEvent, song: MusicSong, ctx: Context) {
		const drag = dragging;
		endDrag();
		if (drag?.kind !== 'song' || !sameList(drag.ctx, ctx) || drag.id === song.id) return;
		event.preventDefault();
		const where = half(event);
		if (ctx.type === 'regular' && folder) {
			const songs = reordered(folder.songs, drag.id, song.id, where);
			folder.songs = songs;
			void act(() => musicApi.orderFolder(ctx.folder, { songs }));
		} else if (ctx.type === 'folder-liked' && folder) {
			const liked = reordered(folder.liked, drag.id, song.id, where);
			folder.liked = liked;
			void act(() => musicApi.orderFolder(ctx.folder, { liked }));
		} else if (ctx.type === 'liked') {
			const liked = reordered(library.liked, drag.id, song.id, where);
			library.liked = liked;
			void act(() => musicApi.orderLiked(liked));
		} else if (ctx.type === 'playlist' && playlist) {
			const songs = reordered(playlist.songs, drag.id, song.id, where);
			playlist.songs = songs;
			void act(() => musicApi.editPlaylist(ctx.playlist, { songs }));
		}
	}

	function overFolder(event: DragEvent, target: MusicFolder) {
		if (isFiles(event) || dragging?.kind === 'song' || (dragging?.kind === 'folder' && dragging.id !== target.id)) {
			event.preventDefault();
			if (event.dataTransfer) event.dataTransfer.dropEffect = event.altKey && dragging?.kind === 'song' ? 'copy' : 'move';
			dropMark = `folder:${target.id}`;
		}
	}

	async function dropOnFolder(event: DragEvent, target: MusicFolder) {
		event.preventDefault();
		event.stopPropagation();
		const drag = dragging;
		endDrag();
		if (!drag && event.dataTransfer) return upload(await filesFromDrop(event.dataTransfer, target.id));
		if (drag?.kind === 'song') {
			const source = drag.ctx.type === 'regular' || drag.ctx.type === 'folder-liked' ? drag.ctx.folder : null;
			const copy = event.altKey || source === null;
			await act(() => musicApi.move({ ids: [drag.id], target: target.id, source, copy }), copy ? `Added to ${target.name}` : `Moved to ${target.name}`);
		} else if (drag?.kind === 'folder' && drag.id !== target.id) {
			await act(() => musicApi.moveFolder(drag.id, target.id));
		}
	}

	function overCrumb(event: DragEvent, target: string | null) {
		if (dragging?.kind === 'folder' || (dragging?.kind === 'song' && target && target !== folderId)) {
			event.preventDefault();
			dropMark = `crumb:${target ?? ''}`;
		}
	}

	async function dropOnCrumb(event: DragEvent, target: string | null) {
		event.preventDefault();
		const drag = dragging;
		endDrag();
		if (drag?.kind === 'folder') {
			await act(() => musicApi.moveFolder(drag.id, target));
		} else if (drag?.kind === 'song' && target && target !== folderId) {
			const source = drag.ctx.type === 'regular' || drag.ctx.type === 'folder-liked' ? drag.ctx.folder : null;
			await act(() => musicApi.move({ ids: [drag.id], target, source, copy: event.altKey || !source }));
		}
	}

	function overPlaylistRow(event: DragEvent, target: MusicPlaylist) {
		if (dragging?.kind === 'playlist' && dragging.id !== target.id) {
			event.preventDefault();
			dropMark = `${target.id}:${half(event)}`;
		}
	}

	function dropOnPlaylistRow(event: DragEvent, target: MusicPlaylist) {
		const drag = dragging;
		endDrag();
		if (drag?.kind !== 'playlist' || drag.id === target.id) return;
		event.preventDefault();
		const ids = reordered(library.playlistOrder, drag.id, target.id, half(event));
		library.playlistOrder = ids;
		void act(() => musicApi.orderPlaylists(ids));
	}

	function overPage(event: DragEvent) {
		if (isFiles(event)) event.preventDefault();
	}

	async function dropOnPage(event: DragEvent) {
		if (!isFiles(event) || !event.dataTransfer) return;
		event.preventDefault();
		await upload(await filesFromDrop(event.dataTransfer, folderId));
	}

	async function upload(items: Awaited<ReturnType<typeof filesFromDrop>>) {
		const count = uploads.add(items);
		if (!count) notify('Nothing there EXSCALIA can play', { tone: 'error' });
	}

	$effect(() => {
		// Refresh once a batch of uploads has finished.
		if (!uploads.active && uploads.files > 0) void library.load();
	});

	function onKeydown(event: KeyboardEvent) {
		if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || menu || editing || picker) return;
		const target = event.target as HTMLElement | null;
		if (target?.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"], .cm-editor')) return;
		if (event.key === ' ' && player.current) {
			event.preventDefault();
			player.toggle();
		} else if ((event.key === 'Backspace' || event.key === 'Escape') && view !== 'root') {
			event.preventDefault();
			up();
		}
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#snippet songRow(song: MusicSong, number: string, ctx: Context, regularNumber: number | null = null)}
	{@const context = queueFor(ctx).context}
	{@const current = player.current === song.id && player.context === context}
	<li
		class="row"
		class:current
		class:missing={song.missing}
		class:flash={flash === song.id && ctx.type === 'regular'}
		class:mark-before={dropMark === `${song.id}:before`}
		class:mark-after={dropMark === `${song.id}:after`}
		data-regular={ctx.type === 'regular' ? song.id : undefined}
		draggable={ctx.type !== 'trash'}
		ondragstart={(e) => startDrag(e, { kind: 'song', id: song.id, ctx })}
		ondragend={endDrag}
		ondragover={(e) => overRow(e, song, ctx)}
		ondrop={(e) => dropOnRow(e, song, ctx)}
		oncontextmenu={(e) => { e.preventDefault(); songMenu(e, song, ctx); }}
		onpointerdown={(e) => onPressStart(e, (p) => songMenu(p, song, ctx))}
		onpointermove={onPressMove}
		onpointerup={onPressEnd}
		onpointercancel={onPressEnd}
	>
		{#if regularNumber !== null}
			<button type="button" class="num jump" title="Show in the folder" onclick={() => jumpTo(song.id)}>
				<span class="liked-mark"><Glyph name="heart" filled /></span>
				<span class="hover-num">{String(regularNumber).padStart(2, '0')}</span>
			</button>
		{:else}
			<span class="num">
				{#if current && player.playing}<Glyph name="pause" filled />{:else}{number}{/if}
			</span>
		{/if}
		<button type="button" class="song" disabled={song.missing && ctx.type !== 'trash'} onclick={() => play(song, ctx)}>
			{#if coverIn(song, ctx.type === 'regular' || ctx.type === 'folder-liked' ? ctx.folder : null)}
				<img class="thumb" src={coverIn(song, ctx.type === 'regular' || ctx.type === 'folder-liked' ? ctx.folder : null)} alt="" loading="lazy" />
			{:else}
				<span class="thumb blank"></span>
			{/if}
			<span class="names">
				<span class="title">{song.title}</span>
				<span class="artist">{song.missing ? 'Audio missing' : (song.artist ?? '')}</span>
			</span>
			<span class="len">{formatTime(song.duration)}</span>
		</button>
		{#if ctx.type === 'trash'}
			<button type="button" class="pill" onclick={() => act(() => musicApi.restore([song.id]))}>Restore</button>
		{:else}
			<button type="button" class="more" title="More" aria-label="More" onclick={(e) => songMenu(e, song, ctx)}>
				<Glyph name="dots" />
			</button>
		{/if}
	</li>
{/snippet}

{#snippet folderTile(f: MusicFolder)}
	<li>
		<button
			type="button"
			class="tile"
			class:mark={dropMark === `folder:${f.id}`}
			draggable="true"
			ondragstart={(e) => startDrag(e, { kind: 'folder', id: f.id })}
			ondragend={endDrag}
			ondragover={(e) => overFolder(e, f)}
			ondragleave={() => dropMark === `folder:${f.id}` && (dropMark = null)}
			ondrop={(e) => dropOnFolder(e, f)}
			oncontextmenu={(e) => { e.preventDefault(); folderMenu(e, f); }}
			onpointerdown={(e) => onPressStart(e, (p) => folderMenu(p, f))}
			onpointermove={onPressMove}
			onpointerup={onPressEnd}
			onpointercancel={onPressEnd}
			onclick={() => go({ folder: f.id })}
		>
			{#if coverUrl(f.cover)}
				<img class="art" src={coverUrl(f.cover)} alt="" loading="lazy" />
			{:else}
				<span class="art blank" style:background={f.colour ? `#${f.colour}` : undefined}><Glyph name="folder" /></span>
			{/if}
			<span class="label">{f.name}</span>
			{#if f.pinned}<span class="pin" title="Pinned"><Glyph name="star" filled /></span>{/if}
			<span class="count">{f.songs.length || ''}</span>
		</button>
	</li>
{/snippet}

<div class="music" role="region" aria-label="Music" ondragover={overPage} ondrop={dropOnPage}>
	{#if background?.background}
		<div
			class="backdrop"
			aria-hidden="true"
			style:background-image="url({coverUrl(background.background)})"
			style:filter="blur({background.blur / 4}px) brightness({1 - background.dim / 100})"
		></div>
	{/if}

	<header class="top">
		<nav class="path" aria-label="Folder path">
			<button
				type="button"
				class="crumb"
				class:here={view === 'root'}
				class:mark={dropMark === 'crumb:'}
				ondragover={(e) => overCrumb(e, null)}
				ondrop={(e) => dropOnCrumb(e, null)}
				onclick={() => go(null)}>Music</button
			>
			{#if view === 'folder'}
				{#each trail as step (step.id)}
					<span class="sep" aria-hidden="true">/</span>
					<button
						type="button"
						class="crumb"
						class:here={step.id === folderId}
						class:mark={dropMark === `crumb:${step.id}`}
						ondragover={(e) => overCrumb(e, step.id)}
						ondrop={(e) => dropOnCrumb(e, step.id)}
						onclick={() => go({ folder: step.id })}>{step.name}</button
					>
				{/each}
			{:else if view === 'liked'}
				<span class="sep">/</span><span class="crumb here">Liked</span>
			{:else if view === 'playlists' || view === 'playlist'}
				<span class="sep">/</span>
				<button type="button" class="crumb" class:here={view === 'playlists'} onclick={() => go({ view: 'playlists' })}>Playlists</button>
				{#if playlist}<span class="sep">/</span><span class="crumb here">{playlist.name}</span>{/if}
			{:else if view === 'trash'}
				<span class="sep">/</span><span class="crumb here">Trash</span>
			{/if}
		</nav>
		<div class="tools">
			{#if view === 'root' || view === 'folder'}
				<button type="button" class="icon" title="New folder" aria-label="New folder" onclick={() => newFolder(folderId)}>
					<Glyph name="folder" />
				</button>
				<button type="button" class="icon" title="Add songs" aria-label="Add songs" onclick={() => fileInput?.click()}>
					<Glyph name="plus" />
				</button>
				<button type="button" class="icon" title="Add a folder of songs" aria-label="Add a folder of songs" onclick={() => folderInput?.click()}>
					<Glyph name="grid" />
				</button>
			{:else if view === 'playlists'}
				<button type="button" class="icon" title="New playlist" aria-label="New playlist" onclick={newPlaylist}>
					<Glyph name="plus" />
				</button>
			{:else if view === 'playlist' && playlist}
				<button type="button" class="icon" title="Edit playlist" aria-label="Edit playlist" onclick={() => (editing = { kind: 'playlist', id: playlist.id })}>
					<Glyph name="dots" />
				</button>
			{:else if view === 'trash' && library.trashed.length}
				<button
					type="button"
					class="pill danger"
					onclick={async () => {
						if (await confirmDialog(`Delete ${library.trashed.length} songs and their audio for good?`, { title: 'Empty trash', confirmLabel: 'Empty trash', danger: true }))
							await act(() => musicApi.emptyTrash());
					}}>Empty trash</button
				>
			{/if}
		</div>
		<input bind:this={fileInput} type="file" multiple accept="audio/*,.flac,.opus,.aif,.aiff,.m4a" hidden
			onchange={(e) => { upload(filesFromInput(e.currentTarget.files!, folderId)); e.currentTarget.value = ''; }} />
		<input bind:this={folderInput} type="file" webkitdirectory hidden
			onchange={(e) => { upload(filesFromInput(e.currentTarget.files!, folderId)); e.currentTarget.value = ''; }} />
	</header>

	{#if uploads.files > 0}
		<div class="uploads" role="status">
			{#if uploads.active}
				Uploading {uploads.done} of {uploads.files} · {formatBytes(uploads.sent)} of {formatBytes(uploads.total)}
				<span class="bar"><span style:width="{uploads.total ? (uploads.sent / uploads.total) * 100 : 0}%"></span></span>
			{:else}
				Added {uploads.files - uploads.failed.length - uploads.duplicates.length} of {uploads.files}
				{#if uploads.duplicates.length} · {uploads.duplicates.length} already here{/if}
				{#if uploads.failed.length} · failed: {uploads.failed.slice(0, 3).map((f) => f.name).join(', ')}{/if}
			{/if}
		</div>
	{/if}

	{#if library.error && !library.loaded}
		<p class="quiet">{library.error}</p>
	{:else if view === 'root'}
		<ul class="tiles">
			<li>
				<button type="button" class="tile" onclick={() => go({ view: 'liked' })}>
					<span class="art blank"><Glyph name="heart" filled /></span>
					<span class="label">Liked</span><span class="count">{allLiked.length || ''}</span>
				</button>
			</li>
			<li>
				<button type="button" class="tile" onclick={() => go({ view: 'playlists' })}>
					<span class="art blank"><Glyph name="disc" /></span>
					<span class="label">Playlists</span><span class="count">{playlists.length || ''}</span>
				</button>
			</li>
			{#each pinned as f (f.id)}{@render folderTile(f)}{/each}
		</ul>
		{#if library.loaded && !library.root.length}
			<p class="quiet">No music yet. Drop songs or folders here, or copy them into the drop folder on the server.</p>
		{:else}
			<ul class="tiles">
				{#each subfolders as f (f.id)}{@render folderTile(f)}{/each}
			</ul>
		{/if}
		{#if library.trashed.length}
			<button type="button" class="trash-link" onclick={() => go({ view: 'trash' })}>Trash · {library.trashed.length}</button>
		{/if}
	{:else if view === 'folder'}
		{#if folder}
			{#if subfolders.length}
				<ul class="tiles">{#each subfolders as f (f.id)}{@render folderTile(f)}{/each}</ul>
			{/if}
			{#if likedPart.length}
				<ol class="songs liked-part">
					{#each likedPart as song (song.id)}
						{@render songRow(song, '', { type: 'folder-liked', folder: folder.id }, folder.songs.indexOf(song.id) + 1)}
					{/each}
				</ol>
				<hr class="divider" />
			{/if}
			<ol class="songs">
				{#each regular as song, i (song.id)}
					{@render songRow(song, String(i + 1).padStart(2, '0'), { type: 'regular', folder: folder.id })}
				{/each}
			</ol>
		{:else if library.loaded}
			<p class="quiet">This folder is gone.</p>
		{/if}
	{:else if view === 'liked'}
		<ol class="songs">
			{#each allLiked as song, i (song.id)}
				{@render songRow(song, String(i + 1).padStart(2, '0'), { type: 'liked' })}
			{/each}
		</ol>
	{:else if view === 'playlists'}
		<ol class="songs">
			{#each playlists as p (p.id)}
				<li
					class="row"
					class:mark-before={dropMark === `${p.id}:before`}
					class:mark-after={dropMark === `${p.id}:after`}
					draggable="true"
					ondragstart={(e) => startDrag(e, { kind: 'playlist', id: p.id })}
					ondragend={endDrag}
					ondragover={(e) => overPlaylistRow(e, p)}
					ondrop={(e) => dropOnPlaylistRow(e, p)}
					onmouseenter={() => (previewBackground = p)}
					onmouseleave={() => previewBackground === p && (previewBackground = null)}
					oncontextmenu={(e) => { e.preventDefault(); playlistMenu(e, p); }}
					onpointerdown={(e) => onPressStart(e, (pt) => playlistMenu(pt, p))}
					onpointermove={onPressMove}
					onpointerup={onPressEnd}
					onpointercancel={onPressEnd}
				>
					<button type="button" class="song" onclick={() => go({ playlist: p.id })}>
						{#if coverUrl(p.cover)}
							<img class="thumb" src={coverUrl(p.cover)} alt="" />
						{:else}
							<img class="thumb sticker" src="/music/sleeve-sticker.png" alt="" />
						{/if}
						<span class="names"><span class="title">{p.name}</span><span class="artist">{p.songs.length} songs</span></span>
					</button>
					<button type="button" class="more" title="More" aria-label="More" onclick={(e) => playlistMenu(e, p)}><Glyph name="dots" /></button>
				</li>
			{/each}
		</ol>
	{:else if view === 'playlist'}
		{#if playlist}
			<ol class="songs">
				{#each playlistSongs as song, i (song.id)}
					{@render songRow(song, String(i + 1).padStart(2, '0'), { type: 'playlist', playlist: playlist.id })}
				{/each}
			</ol>
		{/if}
	{:else if view === 'trash'}
		<ol class="songs">
			{#each library.trashed as song (song.id)}
				{@render songRow(song, '', { type: 'trash' })}
			{/each}
		</ol>
	{/if}
</div>

{#if menu}
	<Menu x={menu.x} y={menu.y} items={menu.items} onclose={() => (menu = null)} />
{/if}
{#if picker}
	<Picker title={picker.title} options={picker.options} onpick={picker.onpick} oncreate={picker.oncreate} onclose={() => (picker = null)} />
{/if}
{#if editing}
	<EditDialog kind={editing.kind} id={editing.id} onclose={() => (editing = null)} />
{/if}

<style>
	.music {
		position: relative;
		height: 100%;
		overflow-y: auto;
		padding: 28px 36px 48px;
		box-sizing: border-box;
		isolation: isolate;
	}
	.backdrop {
		position: fixed;
		inset: -40px;
		z-index: -1;
		background-size: cover;
		background-position: center;
		pointer-events: none;
	}
	.top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 22px;
	}
	.path {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		font-size: 20px;
		min-width: 0;
	}
	.crumb {
		padding: 2px 6px;
		border: 1px solid transparent;
		border-radius: 6px;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		cursor: pointer;
		transition: color var(--motion-fast), background-color var(--motion-fast);
	}
	.crumb:hover {
		color: var(--text);
		background: var(--bg-hover);
	}
	.crumb.here {
		color: var(--text);
	}
	.crumb.mark {
		border-color: var(--text);
	}
	.sep {
		color: var(--text-muted);
		opacity: 0.6;
	}
	.tools {
		display: flex;
		gap: 4px;
		flex: none;
	}
	.icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		border: 0;
		border-radius: 8px;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.icon:hover {
		color: var(--text);
		background: var(--bg-hover);
	}
	.icon :global(svg) {
		width: 20px;
		height: 20px;
	}
	.pill {
		padding: 6px 12px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: transparent;
		color: var(--text);
		font: inherit;
		font-size: 13px;
		cursor: pointer;
	}
	.pill.danger {
		color: var(--danger);
	}
	.uploads {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 18px;
		font-size: 13px;
		color: var(--text-muted);
	}
	.uploads .bar {
		flex: 1;
		max-width: 260px;
		height: 2px;
		background: var(--border);
	}
	.uploads .bar span {
		display: block;
		height: 100%;
		background: var(--text);
	}
	.quiet {
		color: var(--text-muted);
	}
	ul,
	ol {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.tiles {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
		gap: 10px;
		margin-bottom: 22px;
	}
	.tile {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: color-mix(in srgb, var(--bg) 70%, transparent);
		color: var(--text);
		font: inherit;
		text-align: left;
		cursor: pointer;
		transition: background-color var(--motion-fast), border-color var(--motion-fast);
	}
	.tile:hover {
		background: var(--bg-hover);
	}
	.tile.mark {
		border-color: var(--text);
		background: var(--bg-active);
	}
	.art {
		width: 44px;
		height: 44px;
		flex: none;
		border-radius: 6px;
		object-fit: cover;
	}
	.art.blank {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		background: var(--bg-hover);
		color: var(--text-muted);
	}
	.art.blank :global(svg) {
		width: 22px;
		height: 22px;
	}
	.label {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pin :global(svg) {
		width: 13px;
		height: 13px;
		color: var(--text-muted);
	}
	.count {
		color: var(--text-muted);
		font-size: 12px;
	}
	.trash-link {
		padding: 6px 0;
		border: 0;
		background: none;
		color: var(--text-muted);
		font: inherit;
		font-size: 13px;
		cursor: pointer;
	}
	.trash-link:hover {
		color: var(--text);
	}
	.divider {
		margin: 10px 0;
		border: 0;
		border-top: 1px solid var(--border);
	}
	.row {
		display: flex;
		align-items: center;
		gap: 6px;
		border-radius: 8px;
		border-top: 2px solid transparent;
		border-bottom: 2px solid transparent;
		transition: background-color var(--motion-fast);
	}
	.row:hover {
		background: var(--bg-hover);
	}
	.row.current {
		background: var(--bg-active);
	}
	.row.missing {
		opacity: 0.5;
	}
	.row.flash {
		background: var(--accent-soft);
	}
	.row.mark-before {
		border-top-color: var(--text);
	}
	.row.mark-after {
		border-bottom-color: var(--text);
	}
	.num {
		width: 34px;
		flex: none;
		display: inline-flex;
		justify-content: flex-end;
		color: var(--text-muted);
		font-size: 13px;
		font-variant-numeric: tabular-nums;
	}
	.num :global(svg) {
		width: 14px;
		height: 14px;
	}
	.jump {
		padding: 0;
		border: 0;
		background: none;
		font: inherit;
		cursor: pointer;
	}
	.jump .hover-num {
		display: none;
		text-decoration: underline;
		text-underline-offset: 3px;
	}
	.row:hover .jump .hover-num,
	.jump:focus-visible .hover-num {
		display: inline;
	}
	.row:hover .jump .liked-mark,
	.jump:focus-visible .liked-mark {
		display: none;
	}
	.song {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 14px;
		padding: 6px 4px;
		border: 0;
		background: transparent;
		color: var(--text);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.song:disabled {
		cursor: default;
	}
	.thumb {
		width: 38px;
		height: 38px;
		flex: none;
		border-radius: 4px;
		object-fit: cover;
	}
	.thumb.blank {
		background: var(--bg-hover);
	}
	.thumb.sticker {
		object-fit: contain;
		background: var(--bg-hover);
	}
	.names {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
	}
	.title,
	.artist {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.artist {
		font-size: 12px;
		color: var(--text-muted);
	}
	.len {
		flex: none;
		color: var(--text-muted);
		font-size: 13px;
		font-variant-numeric: tabular-nums;
	}
	.more {
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
		opacity: 0;
	}
	.row:hover .more,
	.more:focus-visible {
		opacity: 1;
	}
	.more :global(svg) {
		width: 18px;
		height: 18px;
	}

	@media (max-width: 760px) {
		.music {
			padding: 16px var(--m-gutter) 32px;
		}
		.path {
			font-size: var(--m-title);
		}
		.tiles {
			grid-template-columns: 1fr;
		}
		.tile,
		.row {
			min-height: var(--m-row-h);
		}
		.more {
			opacity: 1;
			width: 44px;
			height: 44px;
		}
		.icon {
			width: 44px;
			height: 44px;
		}
	}
</style>
