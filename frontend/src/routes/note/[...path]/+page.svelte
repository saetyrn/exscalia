<script lang="ts">
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import { holdRouteKey } from '$lib/routekey';
	import { onDestroy, onMount, tick } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from '$lib/motion';
	import ActivityLog from '$lib/ActivityLog.svelte';
	import MediaPanel from '$lib/MediaPanel.svelte';
	import ChatPanel from '$lib/ChatPanel.svelte';
	import NoteChatSheet from '$lib/NoteChatSheet.svelte';
	import ProofreadPanel, { type Card } from '$lib/ProofreadPanel.svelte';
	import Editor from '$lib/Editor.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import MobileEditBar from '$lib/MobileEditBar.svelte';
	import type { Extension } from '@codemirror/state';
	import GraphView from '$lib/GraphView.svelte';
	import {
		api,
		encodePath,
		type Backlink,
		type MonthView,
		type Note,
		type RelatedNote
	} from '$lib/api';
	import {
		addIgnored,
		applyCorrections,
		autocorrectExtension,
		ignoredFor,
		isAutocorrectEnabled,
		markAutocorrectDisabled
	} from '$lib/autocorrect';
	import { moveDraft, readDraft, removeDraft, writeDraft } from '$lib/drafts';
	import { CLIENT_ID, type LiveNoteEvent } from '$lib/live';
	import { createNoteSession } from '$lib/notesession.svelte';
	import {
		applySuggestion,
		dismissSuggestion,
		isStaleIn,
		liveRangeIn,
		loadSuggestions,
		proofreadExtension,
		revealSuggestion,
		type Suggestion
	} from '$lib/proofread';
	import {
		addFrontmatterTag,
		extractLiveTags,
		parseFrontmatterTags,
		removeFrontmatterTag,
		sanitizeTag
	} from '$lib/markdown';
	import {
		afterPathsMoved,
		afterPathsRemoved,
		findNoteByName,
		ensureNoteThread,
		noteChatFor,
		restoreNoteThread,
		overseerStatus,
		refreshNotes,
		rememberNote,
		vault
	} from '$lib/stores.svelte';
	import {
		formatMode,
		formattedMetadata,
		taskStatusControls,
		taskStatusTheme
	} from '$lib/formatmode';
	import { extractHeadings, noteStats } from '$lib/render';
	import { noteHref } from '$lib/recipes/links';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';
	import { wikilinkAutocomplete, wikilinkClick } from '$lib/wikilink';

	let { data } = $props();

	let path = $derived(page.params.path ?? '');

	// Saving, merging, recovery and conflicts: lib/notesession.svelte.ts.
	const session = createNoteSession({
		path: () => path,
		api,
		drafts: { writeDraft, removeDraft, moveDraft },
		editor: () => editor,
		notify,
		goto: (url) => goto(url),
		refreshNotes,
		refreshRelated: () => void refreshRelated(),
		// Deliberately NOT `exscalia:data-changed` — this page listens for
		// that one and would reload the note it just saved.
		announceSaved: () => window.dispatchEvent(new CustomEvent('exscalia:note-saved')),
		clientId: CLIENT_ID,
		now: () => Date.now(),
		setTimeout: (callback, ms) => setTimeout(callback, ms),
		clearTimeout: (timer) => clearTimeout(timer)
	});

	let note = $derived(session.note);
	let backlinks: Backlink[] = $state([]);
	let related: RelatedNote[] = $state([]);
	let linking: string | null = $state(null);
	let loadError: string | null = $state(null);
	let saveState = $derived(session.saveState);

	// Remember whatever note is open so the home shortcuts can jump back to it.
	// Reacting to `path` (not just mount) also catches a rename moving the URL
	// under the open note.
	$effect(() => {
		if (path) rememberNote(path);
	});

	onMount(() => {
		const handleOmniaChange = async (event: Event) => {
			const detail = (event as CustomEvent<{ refresh?: string[]; paths?: string[] }>).detail;
			if (detail?.refresh?.includes('proofread') && detail.paths?.includes(path)) {
				await loadReview(path);
				if (suggestions.length > 0) setPanel(true, 'review');
				return;
			}
			if (!detail?.refresh?.includes('notes') || !detail.paths?.includes(path)) return;
			await refreshNotes();
			if (vault.notes.some((item) => item.path === path)) {
				// In-place adoption/merge instead of load(): load() unmounts the
				// editor, losing cursor, scroll, and undo history mid-chat.
				if (note) await session.pull();
				else await load();
				return;
			}
			const moved = detail.paths.find(
				(candidate) => candidate !== path && vault.notes.some((item) => item.path === candidate)
			);
			await goto(moved ? `/note/${encodePath(moved)}` : '/');
		};
		// Mod+S at the window, in the capture phase: the editor's own binding only
		// fires while the editor has focus, and Read mode has no editor at all —
		// without this the browser's Save dialog opens over the note.
		const handleSaveKey = (event: KeyboardEvent) => {
			if (event.key !== 's' && event.key !== 'S') return;
			if (!(event.metaKey || event.ctrlKey) || event.altKey) return;
			event.preventDefault();
			void session.flush();
		};
		window.addEventListener('exscalia:data-changed', handleOmniaChange);
		window.addEventListener('keydown', handleSaveKey, true);
		scanTimer = setInterval(() => void maybeScan(), 1000);
		return () => {
			window.removeEventListener('exscalia:data-changed', handleOmniaChange);
			window.removeEventListener('keydown', handleSaveKey, true);
		};
	});

	// One view, always editable: a live-preview editor. Markdown syntax renders
	// in place (headings sized, bold/quote/code styled, markers hidden) and the
	// caret's own line reveals its raw markdown so everything stays editable.
	// The old Read/Formatted/Raw cycle — and its `omnia.viewMode`/`omnia.format`
	// localStorage keys — is gone.

	type PanelTab = 'tags' | 'info' | 'omnia' | 'calendar' | 'activity' | 'review' | 'media';

	// Right panel: Tags (default), Info (outline/backlinks/related), OMNIA
	// chat, and daily-note Calendar/Activity tools share one tabbed,
	// collapsible box.
	let panelOpen = $state(true);
	let panelTab = $state<PanelTab>('tags');

	// Mobile only. The panel above is a column beside the editor on a laptop
	// and a slab under a full-height editor on a phone, where reaching the
	// OMNIA tab costs a scroll past the whole note. The sheet is the same
	// conversation, hinged to the bottom of the screen — see NoteChatSheet.
	let chatSheetOpen = $state(false);

	if (typeof localStorage !== 'undefined') {
		try {
			const saved = JSON.parse(localStorage.getItem('omnia.notePanel') ?? 'null');
			if (saved) {
				panelOpen = saved.open !== false;
				panelTab = saved.tab === 'omnia' || saved.tab === 'info' ? saved.tab : 'tags';
			}
		} catch {
			// Corrupt state — keep defaults.
		}
	}

	function setPanel(open: boolean, tab: PanelTab = panelTab) {
		panelOpen = open;
		panelTab = tab;
		try {
			// Calendar is specific to daily notes (and Media to media notes).
			// Keep the ordinary-note preference stable rather than carrying
			// them to another note.
			if (tab !== 'calendar' && tab !== 'activity' && tab !== 'review' && tab !== 'media') {
				localStorage.setItem('omnia.notePanel', JSON.stringify({ open, tab }));
			}
		} catch {
			// Still applies for this page when storage is unavailable.
		}
	}

	let dailyDate = $derived(/^daily\/(\d{4}-\d{2}-\d{2})\.md$/i.exec(path)?.[1] ?? null);
	let isMediaNote = $derived(path.startsWith('media/'));
	let calendarYear = $state(new Date().getFullYear());
	let calendarMonth = $state(new Date().getMonth() + 1);
	let calendarView: MonthView | null = $state(null);
	let calendarLoading = $state(false);
	let calendarError = $state<string | null>(null);
	let calendarLoadGeneration = 0;
	let panelPath = '';

	const today = new Date();
	const todayIso = `${today.getFullYear()}-${(today.getMonth() + 1)
		.toString()
		.padStart(2, '0')}-${today.getDate().toString().padStart(2, '0')}`;

	let calendarMonthName = $derived(
		new Date(calendarYear, calendarMonth - 1, 1).toLocaleString('en', {
			month: 'long',
			year: 'numeric'
		})
	);

	let calendarWeeks = $derived.by(() => {
		const first = new Date(calendarYear, calendarMonth - 1, 1);
		const daysInMonth = new Date(calendarYear, calendarMonth, 0).getDate();
		const cells: (number | null)[] = [
			...Array(first.getDay()).fill(null),
			...Array.from({ length: daysInMonth }, (_, index) => index + 1)
		];
		while (cells.length % 7 !== 0) cells.push(null);
		const weeks: (number | null)[][] = [];
		for (let index = 0; index < cells.length; index += 7) {
			weeks.push(cells.slice(index, index + 7));
		}
		return weeks;
	});

	function calendarIso(day: number) {
		return `${calendarYear.toString().padStart(4, '0')}-${calendarMonth
			.toString()
			.padStart(2, '0')}-${day.toString().padStart(2, '0')}`;
	}

	async function loadCalendarMonth(year: number, month: number) {
		const generation = ++calendarLoadGeneration;
		calendarLoading = true;
		calendarError = null;
		try {
			const loaded = await api.monthView(year, month);
			if (generation === calendarLoadGeneration) calendarView = loaded;
		} catch (cause) {
			if (generation === calendarLoadGeneration) {
				calendarView = null;
				calendarError = (cause as Error).message;
			}
		} finally {
			if (generation === calendarLoadGeneration) calendarLoading = false;
		}
	}

	function shiftCalendarMonth(delta: number) {
		const next = new Date(calendarYear, calendarMonth - 1 + delta, 1);
		calendarYear = next.getFullYear();
		calendarMonth = next.getMonth() + 1;
	}

	function showCurrentCalendarMonth() {
		calendarYear = today.getFullYear();
		calendarMonth = today.getMonth() + 1;
	}

	async function openCalendarDay(day: number) {
		try {
			const { path: dailyPath } = await api.openDaily(calendarIso(day));
			await refreshNotes();
			await goto(`/note/${encodePath(dailyPath)}`);
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	$effect(() => {
		const currentPath = path;
		const currentDailyDate = dailyDate;
		if (panelPath === currentPath) return;
		panelPath = currentPath;

		if (currentDailyDate) {
			const [year, month] = currentDailyDate.split('-').map(Number);
			calendarYear = year;
			calendarMonth = month;
			panelOpen = true;
			panelTab = 'calendar';
		} else if (currentPath.startsWith('media/')) {
			// A media note's panel opens on its tracker controls.
			panelOpen = true;
			panelTab = 'media';
		} else if (
			panelTab === 'calendar' ||
			panelTab === 'activity' ||
			panelTab === 'review' ||
			panelTab === 'media'
		) {
			panelTab = 'tags';
		}
	});

	// Opening the OMNIA tab showed an empty transcript even when the note had a
	// conversation behind it — nothing loaded it until you sent a message, so it
	// looked like the history had been deleted.
	$effect(() => {
		if (!panelOpen || panelTab !== 'omnia' || !path) return;
		void restoreNoteThread(path);
	});

	$effect(() => {
		if (!dailyDate || !panelOpen || panelTab !== 'calendar') return;
		const year = calendarYear;
		const month = calendarMonth;
		void loadCalendarMonth(year, month);
	});

	/** Jump to a heading from the outline. */
	function scrollToLine(line: number) {
		editor?.revealLine(line);
	}

	// No `capture` on the input: with it phones open the camera and nothing
	// else. Without it iOS and Android offer Take Photo next to the photo
	// library and Files, so a whiteboard shot is still one tap away and a
	// screenshot, a PDF or several files can be attached too.
	let fileInput: HTMLInputElement | undefined = $state();

	function pickAttachment() {
		fileInput?.click();
	}

	function onFilesPicked(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const files = [...(input.files ?? [])];
		input.value = ''; // let the same file be picked again
		if (files.length) editor?.insertAttachments(files);
	}


	// Fixed for the life of the editor view. Each of these carries a StateField
	// (completion sources, autocorrect flashes, proofreading anchors) whose
	// state would be discarded if it ever left the configuration.
	const editorExtensions = [
		wikilinkAutocomplete,
		wikilinkClick(openWikilink),
		// Always present: whether a scan runs is decided per tick, and the flash
		// field must exist before the first corrections land. An undone
		// correction is filed under the note open at the time — `path` is read
		// per call, since this outlives renames.
		autocorrectExtension((before) => addIgnored(path, before)),
		proofreadExtension((id) => {
			activeSuggestion = id;
			setPanel(true, 'review');
		})
	];

	// The live-preview bundle, handed to the editor through its compartment. It
	// is always on now — there is only one view — and built once so its identity
	// never changes, so the compartment is never needlessly reconfigured.
	// `formatMode` carries the inline images and math too — one plugin, one
	// decoration set (it was two, walking the viewport twice per keystroke).
	const dynamicEditorExtensions: Extension[] = [
		formatMode,
		formattedMetadata,
		taskStatusControls(updateInlineTask),
		taskStatusTheme
	];


	async function updateInlineTask(id: string, status: 'todo' | 'in_progress' | 'done') {
		if (!note) return;
		try {
			if (!(await session.settledForMutation())) return;
			await api.updateTask(id, { status });
			// The task lives in this note, so the write changed it on the
			// server. Folded in place like any outside edit; the write's live
			// echo may already be doing exactly that.
			await session.pull();
			notify(
				status === 'done'
					? 'Task completed.'
					: status === 'in_progress'
						? 'Task started.'
						: 'Task moved to To do.',
				{ tone: 'success' }
			);
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function createTaskFromCommand(text: string) {
		try {
			// Persist the disappearing command line before touching Tasks Inbox,
			// which also avoids two writes racing if that note is currently open.
			// False puts the command line back in the editor.
			if (!(await session.settledForMutation())) return false;
			const task = await api.createTask({
				text,
				due: null,
				due_visible: false
			});
			await refreshNotes();

			if (note?.path === task.path) await session.pull();

			notify(`Task added: ${text}`, {
				tone: 'success',
				actionLabel: 'Open Tasks',
				action: () => goto('/tasks')
			});
			return true;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
			return false;
		}
	}

	async function openWikilink(target: string) {
		const existing = findNoteByName(target);
		if (existing) {
			goto(noteHref(existing.path));
			return;
		}
		if (await confirmDialog(`“${target}” does not exist yet. Create it?`, { title: 'Create linked note', confirmLabel: 'Create' })) {
			const created = await api.createNote(`${target}.md`);
			await refreshNotes();
			goto(`/note/${encodePath(created.path)}`);
		}
	}

	// Reactive so Read mode, the outline, and word count track live edits.
	let doc = $derived(session.doc);

	let headings = $derived(extractHeadings(doc));
	let stats = $derived(noteStats(doc));
	let recoveryPending = $derived(session.recoveryPending);
	let loadGeneration = 0;
	let preloadConsumed = false;
	let editor: Editor | undefined = $state();
	let editingTitle = $state(false);
	let titleDraft = $state('');
	let titleSaving = $state(false);
	let titleInput: HTMLInputElement | undefined = $state();

	// Set just before the post-rename goto: that navigation must NOT tear the
	// page down and reload — the note is already rebound in place, only the
	// URL moves underneath it. Deliberately not $state: the effect below must
	// re-run on path changes only.
	let renamedInPlace: string | null = null;

	$effect(() => {
		path;
		if (renamedInPlace === path) {
			renamedInPlace = null;
			return;
		}
		editingTitle = false;
		titleDraft = '';
		titleSaving = false;
		load();
	});

	async function load() {
		const generation = ++loadGeneration;
		session.reset();
		suggestions = [];
		activeSuggestion = null;
		loadError = null;
		try {
			// The route load() already fetched this note during navigation. Use
			// it exactly once — later calls are RELOADS after edits and must hit
			// the server, or they would resurrect the stale prefetched copy.
			const usePreload = !preloadConsumed && data.path === path;
			preloadConsumed = true;
			if (usePreload && data.loadError) throw new Error(data.loadError);
			const loaded = usePreload && data.note ? data.note : await api.readNote(path);
			if (generation !== loadGeneration) return;
			const draft = await readDraft(path);
			if (generation !== loadGeneration) return;
			await session.adoptLoaded(loaded, draft);
			// A note never edited in this session never scans.
			lastScannedText = session.doc;
			lastScannedVersion = session.editVersion;
			// A prefetched body was fetched before the click, so it may already be
			// a revision behind. pull() is the same reconciliation the live
			// stream runs: identical revision costs nothing, a moved one is
			// adopted or merged in place without unmounting the editor.
			if (usePreload && data.prefetched) void session.pull();
			// Off the critical path, like refreshRelated(). Awaited here, a
			// failed side-panel request landed in the catch below and replaced
			// the loaded note with 'Could not open' — every note, in the desktop
			// app offline, where api.backlinks has no cached fallback.
			void loadBacklinks(path, generation);
			refreshRelated();
			// A staged review survives a reload, and shows up on the other device.
			void loadReview(path);
		} catch (e) {
			// A late failure from a load the user has since navigated away from
			// must not blank the note they opened instead.
			if (generation !== loadGeneration) return;
			loadError = (e as Error).message;
		}
	}

	async function loadBacklinks(target: string, generation: number) {
		try {
			const list = await api.backlinks(target);
			if (generation === loadGeneration) backlinks = list;
		} catch {
			if (generation === loadGeneration) backlinks = [];
		}
	}

	// Related and the local graph are independent: the graph used to wait
	// behind Related, which waits on the server's embedding model after a
	// restart.
	async function refreshRelated() {
		await Promise.all([
			loadRelated(),
			api.graph().then(
				(graph) => (graphData = graph),
				() => (graphData = null)
			)
		]);
	}

	let relatedTimer: ReturnType<typeof setTimeout> | null = null;
	// While the server's model loads it answers empty. A cold load can take
	// tens of seconds, so ask again with backoff (2 s, 4 s, 8 s, 16 s) until
	// it is ready or off; opening another note starts over.
	const RELATED_RETRIES = 4;

	async function loadRelated(attempt = 0) {
		if (relatedTimer) clearTimeout(relatedTimer);
		relatedTimer = null;
		const forPath = path;
		try {
			const answer = await api.relatedNotes(forPath);
			if (forPath !== path) return;
			related = answer.related;
			if (answer.state === 'warming' && attempt < RELATED_RETRIES) {
				relatedTimer = setTimeout(() => {
					if (forPath === path) void loadRelated(attempt + 1);
				}, 2000 * 2 ** attempt);
			}
		} catch {
			if (forPath === path) related = [];
		}
	}

	let graphData = $state<Awaited<ReturnType<typeof api.graph>> | null>(null);
	// Only draw the local graph when this note is actually joined to something.
	let inGraph = $derived(
		graphData !== null &&
			graphData.edges.some((e) => e.source === path || e.target === path)
	);

	async function linkRelated(r: RelatedNote) {
		if (linking) return;
		linking = r.path;
		try {
			if (!(await session.settledForMutation())) return;
			// With the base revision, so a note that moved on since gets a 409
			// instead of a link appended to a version this editor never saw.
			await api.addLink(path, r.path, session.serverRevision || null);
			// In place: load() unmounted the editor and dropped a pending save.
			// Nothing announces this write, so the pull is explicit.
			await session.pull();
			void refreshRelated(); // the row's linked mark and the local graph
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			linking = null;
		}
	}

	// Tags from the live editor text, so an add shows up instantly instead of
	// after the autosave round-trip.
	let noteTags = $derived.by(() => {
		const n: Note | null = note;
		if (!n) return [];
		return extractLiveTags(doc || n.content);
	});

	let tagInput = $state('');
	let tagSuggestions = $derived(
		vault.tags.map((t) => t.tag).filter((t) => !noteTags.includes(t))
	);
	// Only frontmatter tags can be removed from the panel — an inline #tag is
	// part of the user's prose.
	let removableTags = $derived.by(() => {
		const n: Note | null = note;
		return n ? parseFrontmatterTags(doc || n.content) : [];
	});

	function addTag(e: Event) {
		e.preventDefault();
		const tag = sanitizeTag(tagInput);
		tagInput = '';
		if (!tag || !note || noteTags.includes(tag)) return;
		const next = addFrontmatterTag(doc || note.content, tag);
		if (next === (doc || note.content)) return;
		// Through the editor, like typing: onDocChange schedules the draft and
		// the autosave, so the revision chain and offline queue stay correct.
		editor?.replaceDoc(next, { programmatic: false });
	}

	function removeTag(tag: string) {
		if (!note) return;
		const next = removeFrontmatterTag(doc || note.content, tag);
		if (next === (doc || note.content)) return;
		editor?.replaceDoc(next, { programmatic: false });
	}

	function onDocChange(newDoc: string, programmatic: boolean) {
		session.onEdit(newDoc, programmatic);
		if (!programmatic) lastEditAt = Date.now();
	}

	// --- background autocorrect --------------------------------------------
	//
	// A 1s tick that almost always returns immediately; the guards keep actual
	// scans to at most one per SCAN_GAP_MS, and only while the user is writing
	// (edited since the last scan, but idle for SCAN_IDLE_MS).
	const SCAN_IDLE_MS = 2_000;
	const SCAN_GAP_MS = 5_000;
	// A failed scan waits longer, so an outage is not hit every few seconds.
	const SCAN_RETRY_MS = 30_000;
	let scanning = false;
	let lastScannedText = '';
	// editVersion at the last clean scan, so the idle tick can tell nothing was
	// edited without copying the whole note to compare it.
	let lastScannedVersion = 0;
	let nextScanAt = 0;
	let lastEditAt = 0;
	let scanTimer: ReturnType<typeof setInterval> | null = null;

	async function maybeScan() {
		if (scanning) return;
		scanning = true;
		try {
			if (!(await isAutocorrectEnabled())) return;
			if (document.visibilityState !== 'visible') return;
			if (remoteConflict || recoveryPending) return;
			// Never scan or splice mid-composition: the IME owns an uncommitted
			// range in the DOM, and a document change under it drops the
			// half-formed word. The tick comes back in a second.
			if (editor?.getView()?.composing) return;
			// Cheap guards before the snapshot: this runs every second, and
			// getDoc() copies the whole note just to find it unchanged.
			const now = Date.now();
			if (now - lastEditAt < SCAN_IDLE_MS) return;
			if (now < nextScanAt) return;
			if (session.editVersion === lastScannedVersion) return;
			const snapshot: string = editor?.getDoc() ?? doc;
			if (snapshot === lastScannedText) return;
			const scanPath = path;
			try {
				const result = await api.autocorrect(scanPath, snapshot, ignoredFor(scanPath));
				nextScanAt = Date.now() + SCAN_GAP_MS;
				if (path !== scanPath) return;
				if (!result.enabled) {
					markAutocorrectDisabled();
					return;
				}
				const view = editor?.getView() ?? null;
				if (!view) return;
				// A composition can have started while the request was in flight.
				// Leave `lastScannedText` alone so the same text is scanned again
				// rather than losing these corrections.
				if (view.composing) return;
				const clean = view.state.doc.toString() === snapshot;
				applyCorrections(view, result.suggestions, snapshot);
				// After a drifted apply the doc differs from the snapshot, so
				// leaving lastScannedText at the snapshot keeps the note
				// eligible for a follow-up scan of the dropped spans.
				lastScannedText = clean ? view.state.doc.toString() : snapshot;
				if (clean) lastScannedVersion = session.editVersion;
			} catch {
				nextScanAt = Date.now() + SCAN_RETRY_MS;
			}
		} finally {
			scanning = false;
		}
	}

	// --- proofreading review -----------------------------------------------
	//
	// OMNIA stages suggestions server-side; the editor holds the live anchors.
	// Cards are rebuilt from those anchors whenever the document changes, so
	// context and staleness always describe the note as it is now.
	let suggestions: Suggestion[] = $state([]);
	let activeSuggestion: string | null = $state(null);

	const CONTEXT = 46;

	let reviewCards: Card[] = $derived.by(() => {
		// `doc` is read so these recompute as the note is edited.
		doc;
		const view = editor?.getView() ?? null;
		return suggestions
			.map((s) => {
				const range = view ? liveRangeIn(view.state, s.id) : null;
				const text = view?.state.doc;
				// Context is clipped to the suggestion's own line. A raw character
				// window pulled in frontmatter fences and headings from the lines
				// above, which reads as noise rather than context.
				let pre = '';
				let post = '';
				if (range && text) {
					const startOfLine = text.lineAt(range.from).from;
					const endOfLine = text.lineAt(range.to).to;
					const preFrom = Math.max(startOfLine, range.from - CONTEXT);
					const postTo = Math.min(endOfLine, range.to + CONTEXT);
					pre = (preFrom > startOfLine ? '…' : '') + text.sliceString(preFrom, range.from);
					post = text.sliceString(range.to, postTo) + (postTo < endOfLine ? '…' : '');
				}
				return {
					id: s.id,
					category: s.category,
					before: s.before,
					after: s.after,
					reason: s.reason,
					stale: view ? isStaleIn(view.state, s.id) : false,
					pre,
					post
				} satisfies Card;
			})
			.filter(Boolean);
	});

	async function loadReview(target = path) {
		try {
			const review = await api.proofread(target);
			if (target !== path) return;
			suggestions = review.suggestions;
			loadSuggestions(editor?.getView() ?? null, review.suggestions);
			activeSuggestion = review.suggestions[0]?.id ?? null;
		} catch {
			suggestions = [];
		}
	}

	async function acceptSuggestion(id: string) {
		if (!applySuggestion(editor?.getView() ?? null, id)) {
			notify('That text changed since the review.', { tone: 'error' });
			return;
		}
		// The edit flows through onDocChange -> autosave like anything typed.
		await forgetSuggestion(id);
	}

	async function forgetSuggestion(id: string) {
		const remaining = suggestions.filter((s) => s.id !== id);
		const next = remaining[0]?.id ?? null;
		suggestions = remaining;
		if (activeSuggestion === id) activeSuggestion = next;
		dismissSuggestion(editor?.getView() ?? null, id);
		try {
			await api.dismissSuggestion(path, id);
		} catch {
			// Already gone server-side; the local list is what the user sees.
		}
		if (remaining.length === 0 && panelTab === 'review')
			setPanel(true, dailyDate ? 'calendar' : 'tags');
	}

	async function clearReview() {
		suggestions = [];
		activeSuggestion = null;
		loadSuggestions(editor?.getView() ?? null, []);
		try {
			await api.clearProofread(path);
		} catch {
			// Nothing staged server-side.
		}
		if (panelTab === 'review') setPanel(true, dailyDate ? 'calendar' : 'tags');
	}

	function selectSuggestion(id: string) {
		activeSuggestion = id;
		revealSuggestion(editor?.getView() ?? null, id);
	}

	// --- live sync with the same note open elsewhere -----------------------
	//
	// Every write is announced; the session folds another device's edit in
	// (lib/notesession.svelte.ts, pull()).
	let remoteConflict = $derived(session.remoteConflict);

	async function onLiveEvent(event: LiveNoteEvent) {
		// Our own save, echoed back to every listener including us.
		if (event.origin === CLIENT_ID) return;
		if (!note) return;
		// Renamed or moved on another device: follow it in place, the way a
		// rename made here does. The layout moves the draft, thread and tabs.
		if (event.type === 'note_moved') {
			if (event.path === path && event.new_path) await followMove(event.path, event.new_path);
			return;
		}
		if (event.path !== path) return;
		if (event.type === 'note_removed') {
			// The removal half of a move being followed, not a deletion.
			if (event.path === followingMove) return;
			notify('This note was deleted on another device.', { tone: 'error' });
			await goto('/');
			return;
		}
		// Already holding this version, or busy with the previous notification.
		if (event.revision && event.revision === session.serverRevision) return;
		await session.pull();
	}

	/** Set while a remote move is being followed. The move's note_removed
	 * trails it on the wire and lands while the fresh copy is still being
	 * read, when `path` is still the old one. */
	let followingMove: string | null = null;

	async function followMove(from: string, to: string) {
		followingMove = from;
		try {
			let fresh: Note | null = null;
			try {
				fresh = await api.readNote(to);
			} catch {
				// Unreachable for now. Rebind to the path alone; the guarded
				// pull below (or the resync after reconnecting) brings the rest.
			}
			await rebindAfterRename(
				from,
				to,
				fresh
					? { path: fresh.path, revision: fresh.revision, content: fresh.content }
					: { path: to }
			);
			// Unsaved work kept its old base through the rebind; the pull
			// merges the moved copy into it now rather than at the next save.
			if (!fresh || session.saveState !== 'saved') void session.pull();
		} finally {
			followingMove = null;
		}
	}

	$effect(() => {
		const handler = (event: Event) =>
			void onLiveEvent((event as CustomEvent<LiveNoteEvent>).detail);
		// Desktop: the manifest pull on (re)connect updates the cache without
		// per-note events, so an open note may still show pre-sync text.
		// pull() compares revisions first — unchanged notes cost one read.
		const onSyncMode = (event: Event) => {
			if ((event as CustomEvent).detail === 'live') void session.pull();
		};
		window.addEventListener('exscalia:live', handler);
		window.addEventListener('exscalia:sync-mode', onSyncMode);
		return () => {
			window.removeEventListener('exscalia:live', handler);
			window.removeEventListener('exscalia:sync-mode', onSyncMode);
		};
	});

	beforeNavigate(() => {
		void session.flush();
	});

	// The page had no teardown at all, so a save-retry loop outlived the route.
	onDestroy(() => {
		session.dispose();
		if (relatedTimer) clearTimeout(relatedTimer);
		if (scanTimer) clearInterval(scanTimer);
	});

	$effect(() => {
		const handler = (event: BeforeUnloadEvent) => {
			// Unsaved work is drafted first; see guardUnload().
			if (session.guardUnload()) {
				event.preventDefault();
				event.returnValue = '';
			}
		};
		window.addEventListener('beforeunload', handler);
		return () => window.removeEventListener('beforeunload', handler);
	});

	// A backgrounded PWA that the OS later kills never sees beforeunload;
	// hiding is the last moment that reliably runs. flush() lands the draft
	// first, and its PUT is keepalive.
	$effect(() => {
		const onVisibility = () => {
			if (document.visibilityState === 'hidden') session.suspend();
		};
		const onPageHide = () => session.suspend();
		document.addEventListener('visibilitychange', onVisibility);
		window.addEventListener('pagehide', onPageHide);
		return () => {
			document.removeEventListener('visibilitychange', onVisibility);
			window.removeEventListener('pagehide', onPageHide);
		};
	});

	let isPinned = $derived.by(() => {
		const n: Note | null = note;
		return !!n?.meta?.pinned;
	});

	async function togglePin() {
		if (!note) return;
		try {
			if (!(await session.settledForMutation())) return;
			await api.pinNote(note.path, !isPinned);
			// The flag is frontmatter: adopting the server copy updates the text
			// and note.meta without remounting the editor. Not announced live.
			await session.pull();
			await refreshNotes();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	/** This device's calendar date, for deciding whether a Today shortcut is
	 *  worth showing. The note it opens comes from the server's timezone —
	 *  around midnight the two may briefly disagree, which only means the
	 *  button shows a moment early or late. */
	function localToday(): string {
		const now = new Date();
		return `${now.getFullYear()}-${(now.getMonth() + 1).toString().padStart(2, '0')}-${now
			.getDate()
			.toString()
			.padStart(2, '0')}`;
	}

	async function openTodayDaily() {
		try {
			const opened = await api.openDaily();
			await goto(`/note/${encodePath(opened.path)}`);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	/** Yesterday/Tomorrow on a daily note. Noon avoids the DST edge where
	 *  midnight±24h lands on the same calendar day. */
	async function openAdjacentDaily(delta: -1 | 1) {
		if (!dailyDate) return;
		const at = new Date(`${dailyDate}T12:00:00`);
		at.setDate(at.getDate() + delta);
		const target = `${at.getFullYear()}-${(at.getMonth() + 1).toString().padStart(2, '0')}-${at
			.getDate()
			.toString()
			.padStart(2, '0')}`;
		try {
			const opened = await api.openDaily(target);
			await goto(`/note/${encodePath(opened.path)}`);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	// A rename never reloads the open note. The path (and the realigned
	// frontmatter, if the server rewrote it) are swapped into the live state
	// and the URL moves underneath — the editor stays mounted, so the note
	// doesn't blink out for a round-trip.
	async function rebindAfterRename(
		sourcePath: string,
		target: string,
		result: { path: string; revision?: string; content?: string }
	) {
		session.rebind(sourcePath, target, result);
		// Clicking another note while the rename is saving keeps that
		// navigation; otherwise follow the renamed file so the URL and path
		// label update.
		if (path !== sourcePath) return;
		renamedInPlace = target;
		holdRouteKey(); // the layout's {#key} must not remount the page under us
		await goto(`/note/${encodePath(target)}`, { keepFocus: true, noScroll: true });
		// Renaming changes what other notes' wikilinks resolve to, so the
		// side panels do refresh — quietly, behind the intact editor.
		try {
			backlinks = await api.backlinks(target);
		} catch {
			/* panel keeps its previous state */
		}
		void refreshRelated();
		void loadReview(target);
	}

	async function rename() {
		if (!note) return;
		const sourcePath = note.path;
		const newPath = await promptDialog('Choose the new note path:', {
			title: 'Rename or move note',
			value: sourcePath,
			confirmLabel: 'Rename'
		});
		if (!newPath || newPath === sourcePath) return;
		const target = newPath.endsWith('.md') ? newPath : `${newPath}.md`;
		try {
			await session.flush();
			const result = await api.renameNote(sourcePath, target);
			await afterPathsMoved([[sourcePath, target]]);
			void refreshNotes();
			await rebindAfterRename(sourcePath, target, result);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function beginTitleEdit() {
		if (!note || titleSaving) return;
		titleDraft = title;
		editingTitle = true;
		await tick();
		titleInput?.focus();
		titleInput?.select();
	}

	function cancelTitleEdit() {
		editingTitle = false;
		titleDraft = title;
	}

	async function commitTitleEdit() {
		if (!editingTitle || titleSaving || !note) return;
		const sourcePath = note.path;
		const input = titleDraft.trim();
		if (!input) {
			cancelTitleEdit();
			return;
		}
		const newTitle = input.replace(/\.md$/i, '').trim();
		if (!newTitle) {
			cancelTitleEdit();
			return;
		}
		if (newTitle.includes('/') || newTitle.includes('\\')) {
			notify('Note names cannot contain slashes.', { tone: 'error' });
			return;
		}

		const parts = sourcePath.split('/');
		parts[parts.length - 1] = `${newTitle}.md`;
		const target = parts.join('/');
		if (target === sourcePath && newTitle === title) {
			cancelTitleEdit();
			return;
		}
		// A wikilink target ends at these, so such a note could never be linked.
		// Checked after the no-op above: an older note may already have them.
		if (/[[\]#|]/.test(newTitle)) {
			notify('Note names cannot contain [ ] # |.', { tone: 'error' });
			return;
		}

		titleSaving = true;
		try {
			await session.flush();
			const result = await api.renameNote(sourcePath, target, newTitle);
			await afterPathsMoved([[sourcePath, target]]);
			void refreshNotes();
			editingTitle = false;
			await rebindAfterRename(sourcePath, target, result);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			titleSaving = false;
		}
	}

	function titleInputKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter') {
			event.preventDefault();
			(event.currentTarget as HTMLInputElement).blur();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancelTitleEdit();
		}
	}

	async function remove() {
		if (!note) return;
		if (
			!(await confirmDialog(`Move “${note.path}” to Trash for 30 days?`, {
				title: 'Move note to Trash',
				confirmLabel: 'Move to Trash',
				danger: true
			}))
		)
			return;
		try {
			await api.deleteNote(note.path);
			await afterPathsRemoved([note.path]);
			await refreshNotes();
			notify('Note moved to Trash.', {
				tone: 'success',
				actionLabel: 'View Trash',
				action: () => goto('/trash')
			});
			goto('/');
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	$effect(() => {
		const renameCurrent = () => void rename();
		const deleteCurrent = () => void remove();
		window.addEventListener('exscalia:rename-note', renameCurrent);
		window.addEventListener('exscalia:delete-note', deleteCurrent);
		return () => {
			window.removeEventListener('exscalia:rename-note', renameCurrent);
			window.removeEventListener('exscalia:delete-note', deleteCurrent);
		};
	});

	let title = $derived.by(() => {
		const n: Note | null = note;
		if (!n) return '';
		return n.path.split('/').pop()?.replace(/\.md$/, '') ?? '';
	});
</script>

<svelte:head>
	{#if note}<title>{title} — EXSCALIA</title>{/if}
</svelte:head>

{#if loadError}
	<div class="msg">Could not open <code>{path}</code>: {loadError}</div>
{:else if note}
	{#if recoveryPending}
		<div class="draft-banner" role="status">
			<div>
				<strong>Recovered a newer draft from this device.</strong>
				<span>Review it before deciding whether to save or discard it.</span>
			</div>
			<button onclick={() => void session.discardDraft()}>Discard draft</button>
			<button class="primary" onclick={() => session.keepDraft()}>Keep and save</button>
		</div>
	{/if}
	{#if remoteConflict}
		<div class="draft-banner" role="status">
			<div>
				<strong>Edited on another device, in the same place.</strong>
				<span>Both versions changed the same lines, so pick one.</span>
			</div>
			<button onclick={() => session.takeTheirs()}>Take theirs</button>
			<button class="primary" onclick={() => session.keepMine()}>Keep mine</button>
		</div>
	{/if}
	<header class="notebar">
		<div class="meta">
			<span class="path">{note.path}</span>
		</div>
		<div class="actions">
			{#if overseerStatus.available}
				<!-- First in the strip and never scrolled out of reach: this is
				     the phone's whole path to OMNIA-on-this-note. Mobile only —
				     a laptop has the panel's OMNIA tab a click away. -->
				<button
					type="button"
					class="chat-trigger"
					title="Ask OMNIA about this note"
					aria-label="Ask OMNIA about this note"
					aria-haspopup="dialog"
					onclick={() => (chatSheetOpen = true)}
				>
					<Glyph name="chat" />
				</button>
			{/if}
			<span class="save-state" class:error={saveState === 'error'}>
				{#key saveState}
					<span class="save-text" transition:fade={{ duration: 110 }}>
						{#if saveState === 'saved'}Saved{:else if saveState === 'error'}Save failed{:else}Saving…{/if}
					</span>
				{/key}
			</span>
			{#if dailyDate}
				<!-- Dailies get neighbours instead of file actions: they're not
				     renamed, pinned or deleted day to day, and History still
				     lives in the calendar panel. -->
				<button onclick={() => void openAdjacentDaily(-1)}>Yesterday</button>
				{#if dailyDate !== localToday()}
					<button onclick={() => void openTodayDaily()}>Today</button>
				{/if}
				<button onclick={() => void openAdjacentDaily(1)}>Tomorrow</button>
			{:else}
				<button onclick={pickAttachment} title="Attach an image or PDF">Attach</button>
				<button onclick={togglePin} title={isPinned ? 'Unpin this note' : 'Pin to sidebar'}>
					{isPinned ? 'Unpin' : 'Pin'}
				</button>
				<button onclick={() => goto(`/history?path=${encodeURIComponent(note!.path)}`)}>History</button>
				<button onclick={rename}>Rename</button>
				<button class="danger" onclick={remove}>Delete</button>
			{/if}
		</div>
	</header>
	<div class="body">
		<div class="editor-wrap">
			<!-- The note's name as its heading, Obsidian-style: editing it in
			     place renames the file. Dailies keep a plain heading — their
			     name is their date. -->
			<div class="note-heading">
				{#if editingTitle}
					<input
						class="heading-input"
						bind:this={titleInput}
						bind:value={titleDraft}
						aria-label="Note name"
						aria-busy={titleSaving}
						disabled={titleSaving}
						onkeydown={titleInputKeydown}
						onblur={commitTitleEdit}
					/>
				{:else if dailyDate}
					<h1 class="heading-text">{title}</h1>
				{:else}
					<button
						type="button"
						class="heading-text heading-editable"
						title="Click to rename"
						onclick={beginTitleEdit}
					>
						{title}
					</button>
				{/if}
			</div>
			<input
				bind:this={fileInput}
				class="file-input"
				type="file"
				accept="image/*,application/pdf"
				multiple
				onchange={onFilesPicked}
			/>
			<!-- The one view: an always-live editor. -->
			<div class="editor-host">
				<Editor
					bind:this={editor}
					path={note.path}
					content={doc}
					onChange={onDocChange}
					onReplacementConflict={() => session.replacementConflict()}
					onTaskCommand={createTaskFromCommand}
					onSave={() => void session.flush()}
					extensions={editorExtensions}
					dynamicExtensions={dynamicEditorExtensions}
				/>
			</div>
			<!-- Touch only, and only while the soft keyboard is up: indent,
			     outdent, bullet, checkbox and `[[`. It decides that for itself. -->
			<MobileEditBar getView={() => editor?.getView() ?? null} />
		</div>
		<aside
			class="panel"
			class:collapsed={!panelOpen}
			class:chat-mode={panelOpen && panelTab === 'omnia' && overseerStatus.available}
			class:calendar-mode={panelOpen && panelTab === 'calendar' && dailyDate}
			class:activity-mode={panelOpen && panelTab === 'activity' && dailyDate}
		>
			{#if panelOpen}
				<header class="panel-tabs">
					{#if isMediaNote}
						<button
							class="ptab"
							class:active={panelTab === 'media'}
							onclick={() => setPanel(true, 'media')}
						>
							Media
						</button>
					{/if}
					<!-- Dailies do without Tags and Info: their name is their date,
					     and they aren't the notes that get tagged. -->
					{#if !dailyDate}
						<button
							class="ptab"
							class:active={panelTab === 'tags'}
							onclick={() => setPanel(true, 'tags')}
						>
							Tags
						</button>
						<button
							class="ptab"
							class:active={panelTab === 'info'}
							onclick={() => setPanel(true, 'info')}
						>
							Info
						</button>
					{/if}
					{#if overseerStatus.available}
						<button
							class="ptab"
							class:active={panelTab === 'omnia'}
							onclick={() => setPanel(true, 'omnia')}
						>
							OMNIA
						</button>
					{/if}
					{#if reviewCards.length > 0}
						<button
							class="ptab"
							class:active={panelTab === 'review'}
							onclick={() => setPanel(true, 'review')}
						>
							Review ({reviewCards.length})
						</button>
					{/if}
					{#if dailyDate}
						<button
							class="ptab"
							class:active={panelTab === 'calendar'}
							onclick={() => setPanel(true, 'calendar')}
						>
							Calendar
						</button>
						<button
							class="ptab"
							class:active={panelTab === 'activity'}
							onclick={() => setPanel(true, 'activity')}
						>
							Activity
						</button>
					{/if}
					<button
						class="panel-collapse"
						title="Collapse panel"
						aria-label="Collapse panel"
						onclick={() => setPanel(false)}
					>
						»
					</button>
				</header>
			{:else}
				<div class="panel-rail">
					<button
						title="Tags on this note"
						aria-label="Expand tags panel"
						data-label="Tags"
						onclick={() => setPanel(true, 'tags')}
					>
						<span aria-hidden="true">#</span>
						<span class="rail-label">Tags</span>
					</button>
					<button
						title="Info: outline, backlinks, related"
						aria-label="Expand info panel"
						data-label="Info"
						onclick={() => setPanel(true, 'info')}
					>
						<span aria-hidden="true">i</span>
						<span class="rail-label">Info</span>
					</button>
					{#if overseerStatus.available}
						<button
							title="Chat with OMNIA about this note"
							aria-label="Expand OMNIA chat"
							data-label="OMNIA"
							onclick={() => setPanel(true, 'omnia')}
						>
							<span aria-hidden="true">∗</span>
							<span class="rail-label">OMNIA</span>
						</button>
					{/if}
					{#if dailyDate}
						<button
							title="Calendar"
							aria-label="Expand calendar"
							data-label="Calendar"
							onclick={() => setPanel(true, 'calendar')}
						>
							<span aria-hidden="true">▦</span>
							<span class="rail-label">Calendar</span>
						</button>
						<button
							title="Activity log"
							aria-label="Expand activity log"
							data-label="Activity"
							onclick={() => setPanel(true, 'activity')}
						>
							<span aria-hidden="true">◷</span>
							<span class="rail-label">Activity</span>
						</button>
					{/if}
					<span class="rail-sep" aria-hidden="true"></span>
					<button
						onclick={togglePin}
						title={isPinned ? 'Unpin this note' : 'Pin to sidebar'}
						aria-label={isPinned ? 'Unpin this note' : 'Pin to sidebar'}
						data-label={isPinned ? 'Unpin' : 'Pin'}
					>
						<span aria-hidden="true">⌖</span>
						<span class="rail-label">{isPinned ? 'Unpin' : 'Pin'}</span>
					</button>
					<button
						title="Version history"
						aria-label="Open version history"
						data-label="History"
						onclick={() => goto(`/history?path=${encodeURIComponent(note!.path)}`)}
					>
						<span aria-hidden="true">↶</span>
						<span class="rail-label">History</span>
					</button>
					<button
						onclick={rename}
						title="Rename"
						aria-label="Rename note"
						data-label="Rename"
					>
						<span aria-hidden="true">✎</span>
						<span class="rail-label">Rename</span>
					</button>
					<button
						class="rail-danger"
						onclick={remove}
						title="Delete"
						aria-label="Delete note"
						data-label="Delete"
					>
						<span aria-hidden="true">×</span>
						<span class="rail-label">Delete</span>
					</button>
				</div>
			{/if}
			{#if panelOpen && panelTab === 'omnia' && overseerStatus.available && note}
				{@const notePath = note.path}
				<ChatPanel
					chat={noteChatFor(notePath)}
					ensureThread={() => ensureNoteThread(notePath)}
					contextPath={notePath}
					compact
					placeholder="Ask about this note…"
					starters={[
						'Summarize this note',
						'Suggest tags for it',
						'Make flashcards from it',
						'What should this link to?'
					]}
					beforeSend={session.flushBeforeOmnia}
				/>
			{:else if panelOpen && panelTab === 'review'}
				<ProofreadPanel
					cards={reviewCards}
					activeId={activeSuggestion}
					onSelect={selectSuggestion}
					onAccept={acceptSuggestion}
					onDismiss={forgetSuggestion}
					onClear={clearReview}
				/>
			{:else if panelOpen && panelTab === 'media' && isMediaNote}
				<MediaPanel {path} meta={note?.meta ?? {}} />
			{:else if panelOpen && panelTab === 'activity' && dailyDate}
				<ActivityLog date={dailyDate} />
			{:else if panelOpen && panelTab === 'calendar' && dailyDate}
				<div class="mini-calendar">
					<header class="mini-calendar-head">
						<button
							type="button"
							aria-label="Previous month"
							title="Previous month"
							onclick={() => shiftCalendarMonth(-1)}
						>
							←
						</button>
						<button
							type="button"
							class="mini-month-name"
							title="Return to the current month"
							onclick={showCurrentCalendarMonth}
						>
							{calendarMonthName}
						</button>
						<button
							type="button"
							aria-label="Next month"
							title="Next month"
							onclick={() => shiftCalendarMonth(1)}
						>
							→
						</button>
					</header>
					<div class="mini-calendar-grid" aria-label={calendarMonthName}>
						{#each ['S', 'M', 'T', 'W', 'T', 'F', 'S'] as weekday, index (`${weekday}-${index}`)}
							<span class="mini-weekday" aria-hidden="true">{weekday}</span>
						{/each}
						{#each calendarWeeks as week, weekIndex (weekIndex)}
							{#each week as day, dayIndex (dayIndex)}
								{#if day === null}
									<span class="mini-day empty" aria-hidden="true"></span>
								{:else}
									{@const date = calendarIso(day)}
									{@const taskCount = calendarView?.task_activity[date] ?? 0}
									{@const eventCount = calendarView?.event_activity?.[date] ?? 0}
									<button
										type="button"
										class="mini-day"
										class:today={date === todayIso}
										class:active-day={date === dailyDate}
										class:has-note={calendarView?.daily_notes.includes(date)}
										aria-label={`Open daily note for ${date}${taskCount ? `, ${taskCount} task event${taskCount === 1 ? '' : 's'}` : ''}${eventCount ? `, ${eventCount} calendar event${eventCount === 1 ? '' : 's'}` : ''}`}
										title={`Open ${date}`}
										onclick={() => openCalendarDay(day)}
									>
										<span>{day}</span>
										{#if taskCount}
											<span class="mini-task-count" aria-hidden="true">{Math.min(taskCount, 9)}</span>
										{:else if eventCount}
											<span class="mini-task-count event" aria-hidden="true">{Math.min(eventCount, 9)}</span>
										{/if}
									</button>
								{/if}
							{/each}
						{/each}
					</div>
					{#if calendarLoading}
						<p class="mini-calendar-state">Loading…</p>
					{:else if calendarError}
						<p class="mini-calendar-state error">{calendarError}</p>
					{:else}
						<div class="mini-calendar-key">
							<span><i class="note-key"></i> Daily note</span>
							<span><i class="task-key"></i> Task activity</span>
							<span><i class="event-key"></i> Event</span>
						</div>
					{/if}
					<a class="full-calendar-link" href="/calendar">Open full calendar</a>
				</div>
			{:else if panelOpen && panelTab === 'tags'}
				<div class="panel-scroll">
					<h3>Tags</h3>
					{#if noteTags.length === 0}
						<p class="none">No tags yet.</p>
					{:else}
						<div class="chips">
							{#each noteTags as t (t)}
								<span class="chip-group" in:fly={{ y: 3, duration: 150, easing: cubicOut }}>
									<a
										class="chip"
										class:has-x={removableTags.includes(t)}
										href="/tags/{encodeURIComponent(t)}"
										title={removableTags.includes(t)
											? undefined
											: 'Inline #tag — edit the note text to remove it'}
									>
										#{t}
									</a>
									{#if removableTags.includes(t)}
										<button
											class="chip-x"
											title="Remove tag"
											aria-label="Remove tag {t}"
											onclick={() => removeTag(t)}
										>
											✕
										</button>
									{/if}
								</span>
							{/each}
						</div>
					{/if}
					<form class="tag-add" onsubmit={addTag}>
						<input
							bind:value={tagInput}
							placeholder="Add tag…"
							list="note-tag-options"
							autocapitalize="none"
							spellcheck="false"
							enterkeyhint="done"
						/>
						<datalist id="note-tag-options">
							{#each tagSuggestions as t (t)}
								<option value={t}></option>
							{/each}
						</datalist>
					</form>
				</div>
			{:else if panelOpen}
				<div class="panel-scroll">
			<p class="stats">
				{stats.words.toLocaleString()} words · {stats.characters.toLocaleString()} chars ·
				{stats.readingMinutes} min read
			</p>
			{#if headings.length > 0}
				<h3>Outline</h3>
				<ul class="outline">
					{#each headings as heading (heading.line + heading.text)}
						<li>
							<button
								style:padding-left="{(heading.level - 1) * 11}px"
								onclick={() => scrollToLine(heading.line)}
								title="Jump to “{heading.text}”"
							>
								{heading.text}
							</button>
						</li>
					{/each}
				</ul>
			{/if}
			<h3>Backlinks</h3>
			{#if backlinks.length === 0}
				<p class="none">No notes link here.</p>
			{:else}
				<ul>
					{#each backlinks as b (b.path)}
						<li><a href={noteHref(b.path)}>{b.title || b.path}</a></li>
					{/each}
				</ul>
			{/if}

			{#if related.length > 0}
				<h3>Related</h3>
				<ul class="related">
					{#each related as r (r.path)}
						<li in:fly={{ y: 4, duration: 170, easing: cubicOut }}>
							<div class="rel-head">
								<a href={noteHref(r.path)}>{r.title}</a>
								{#if !r.linked}
									<button
										class="link-btn"
										title="Add a [[wikilink]] to this note"
										disabled={linking !== null}
										onclick={() => linkRelated(r)}
									>
										{linking === r.path ? '…' : '+ link'}
									</button>
								{:else}
									<span class="linked-mark" title="Already linked">✓</span>
								{/if}
							</div>
							{#if r.snippet}
								<p class="rel-snippet">{r.snippet}</p>
							{/if}
							<div class="rel-strength" title="Similarity">
								<span style:width="{Math.round(r.score * 100)}%"></span>
							</div>
						</li>
					{/each}
				</ul>
			{/if}
			{#if inGraph && graphData}
				<h3>Graph</h3>
				<!-- This note's two-hop neighborhood; the full map lives at /graph. -->
				<GraphView
					nodes={graphData.nodes}
					edges={graphData.edges}
					center={path}
					height={240}
				/>
				<a class="graph-link" href="/graph">Whole vault →</a>
			{/if}
				</div>
			{/if}
		</aside>
	</div>
	{#if chatSheetOpen && overseerStatus.available}
		<!-- Outside .body, which scrolls on mobile: the sheet is fixed to the
		     viewport, not to the note. Mounting it does not touch the editor,
		     so autosave and the caret are untouched by opening or closing. -->
		<NoteChatSheet
			path={note.path}
			{title}
			beforeSend={session.flushBeforeOmnia}
			onclose={() => (chatSheetOpen = false)}
		/>
	{/if}
{:else}
	<div class="msg">Loading…</div>
{/if}

<style>
	.draft-banner {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 9px 14px;
		border-bottom: 1px solid var(--border);
		background: var(--accent-soft);
		color: var(--text);
		font-size: 12px;
	}
	.draft-banner > div {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}
	.draft-banner span {
		color: var(--text-muted);
	}
	.notebar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 20px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
		transition:
			background-color var(--motion-slow) var(--ease-out),
			border-color var(--motion-base) var(--ease-out);
	}
	.meta {
		display: flex;
		align-items: baseline;
		gap: 10px;
		min-width: 0;
	}
	/* The in-note heading. Shares the editor's centered 760px measure so the
	   name reads as the first line of the document. */
	.note-heading {
		flex: none;
		max-width: 760px;
		width: 100%;
		margin: 0 auto;
		padding: 20px 32px 0;
	}
	.heading-text {
		display: block;
		width: 100%;
		margin: 0 0 0 -4px;
		padding: 2px 4px;
		border: 0;
		border-radius: 6px;
		background: transparent;
		color: var(--text);
		font-family: inherit;
		font-size: 28px;
		font-weight: 700;
		line-height: 1.25;
		text-align: left;
		overflow-wrap: anywhere;
	}
	.heading-editable {
		cursor: text;
		transition: background-color var(--motion-base) var(--ease-out);
	}
	.heading-editable:hover,
	.heading-editable:focus-visible {
		background: var(--bg-hover);
	}
	.heading-input {
		display: block;
		width: calc(100% + 4px);
		margin: 0 0 0 -4px;
		padding: 2px 4px;
		border: 0;
		border-radius: 6px;
		outline: 1px solid var(--accent);
		background: var(--bg-hover);
		color: var(--text);
		font-family: inherit;
		font-size: 28px;
		font-weight: 700;
		line-height: 1.25;
	}
	.path {
		color: var(--text-muted);
		font-size: 12px;
		font-family: var(--font-mono);
		white-space: nowrap;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	/* Phone-only: the sheet it opens is a phone affordance, and a laptop
	   already has the panel's OMNIA tab. */
	.chat-trigger {
		display: none;
	}
	.save-state {
		color: var(--text-muted);
		font-size: 12px;
		margin-right: 6px;
		/* Fixed box + absolute text: the keyed crossfade overlaps in place
		   instead of laying both spans side by side and shifting the row. */
		position: relative;
		width: 68px;
		height: 1.3em;
		min-width: 48px;
		text-align: right;
	}
	.save-text {
		position: absolute;
		inset: 0;
		text-align: right;
		white-space: nowrap;
	}
	.save-state.error {
		color: var(--danger);
	}
	.body {
		flex: 1;
		min-height: 0;
		display: flex;
	}
	.editor-wrap {
		flex: 1;
		min-width: 0;
		/* Column: the heading sits above the internally-scrolling editor and
		   reading views, which take the remaining height. */
		display: flex;
		flex-direction: column;
	}
	.editor-host {
		flex: 1;
		min-height: 0;
	}
	.file-input {
		display: none;
	}
	.panel {
		width: 230px;
		flex-shrink: 0;
		border-left: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		min-height: 0;
		font-size: 13px;
		transition:
			width 240ms var(--ease-out),
			background-color var(--motion-slow) var(--ease-out),
			border-color var(--motion-base) var(--ease-out);
	}
	.panel.chat-mode {
		width: 320px;
	}
	.panel.calendar-mode,
	.panel.activity-mode {
		width: 360px;
	}
	.panel.collapsed {
		position: relative;
		z-index: 10;
		width: 34px;
		overflow: visible;
	}
	.panel-tabs {
		display: flex;
		align-items: center;
		gap: 2px;
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
		white-space: nowrap;
	}
	.ptab {
		font-size: 12px;
		font-weight: 600;
		padding: 3px 10px;
		border: none;
		border-radius: 999px;
		color: var(--text-muted);
	}
	.ptab.active {
		background: var(--accent-soft);
		color: var(--accent);
	}
	.panel-collapse {
		margin-left: auto;
		padding: 0 6px;
		border: none;
		color: var(--text-muted);
	}
	.panel-collapse:not(:disabled):hover {
		color: var(--accent);
	}
	.stats {
		margin: 0 0 12px;
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.outline {
		margin: 0 0 4px;
		padding: 0;
		list-style: none;
	}
	.outline button {
		display: block;
		width: 100%;
		padding: 3px 6px;
		border: none;
		border-radius: 5px;
		color: var(--text-muted);
		font-size: 12.5px;
		text-align: left;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.outline button:hover {
		background: var(--bg-hover);
		color: var(--accent);
	}
	.panel-scroll {
		overflow-y: auto;
		min-height: 0;
		padding: 14px 16px;
	}
	.panel-rail {
		position: relative;
		z-index: 11;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		padding: 8px 0;
		overflow: visible;
	}
	.panel-rail button {
		position: relative;
		z-index: 12;
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		padding: 0;
		border: none;
		border-radius: 6px;
		color: var(--text-muted);
	}
	.rail-label {
		position: absolute;
		top: 50%;
		right: calc(100% + 8px);
		z-index: 30;
		padding: 4px 8px;
		border: 1px solid var(--border);
		border-radius: 5px;
		background: color-mix(in srgb, var(--bg-panel) 92%, var(--accent));
		color: var(--text);
		font-size: 11px;
		font-weight: 600;
		line-height: 1.2;
		opacity: 0;
		pointer-events: none;
		white-space: nowrap;
		box-shadow:
			0 0 0 1px color-mix(in srgb, var(--accent) 38%, transparent),
			0 5px 16px color-mix(in srgb, var(--text) 18%, transparent);
		transform: translateY(-50%);
		transition: opacity var(--motion-fast) var(--ease-out);
	}
	.panel-rail button:hover .rail-label,
	.panel-rail button:focus-visible .rail-label {
		opacity: 1;
	}
	.panel-rail button:not(:disabled):hover {
		color: var(--accent);
		background: var(--bg-hover);
	}
	.rail-sep {
		width: 18px;
		border-top: 1px solid var(--border);
		margin: 4px 0;
	}
	.panel-rail .rail-danger:not(:disabled):hover {
		color: var(--danger);
	}
	.panel-rail .rail-danger:hover .rail-label,
	.panel-rail .rail-danger:focus-visible .rail-label {
		box-shadow:
			0 0 0 1px color-mix(in srgb, var(--danger) 48%, transparent),
			0 5px 16px color-mix(in srgb, var(--text) 18%, transparent);
	}
	.panel h3 {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.5px;
		color: var(--text-muted);
		margin: 12px 0 6px;
	}
	.panel h3:first-child {
		margin-top: 0;
	}
	.panel ul {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.panel li {
		margin: 3px 0;
	}
	.panel a {
		color: var(--accent);
		text-decoration: none;
	}
	.panel a:hover {
		text-decoration: underline;
	}
	.mini-calendar {
		display: flex;
		flex-direction: column;
		gap: 12px;
		min-height: 0;
		overflow-y: auto;
		padding: 14px;
	}
	.mini-calendar-head {
		display: grid;
		grid-template-columns: 30px 1fr 30px;
		align-items: center;
		gap: 4px;
	}
	.mini-calendar-head button {
		min-width: 0;
		height: 30px;
		padding: 0;
		border: 0;
		border-radius: 7px;
		color: var(--text-muted);
	}
	.mini-calendar-head button:hover {
		background: var(--bg-hover);
		color: var(--accent);
	}
	.mini-calendar-head .mini-month-name {
		color: var(--text);
		font-size: 13px;
		font-weight: 650;
	}
	.mini-calendar-grid {
		display: grid;
		grid-template-columns: repeat(7, minmax(0, 1fr));
		gap: 4px;
	}
	.mini-weekday {
		padding: 2px 0 4px;
		color: var(--text-muted);
		font-size: 10px;
		font-weight: 700;
		text-align: center;
	}
	.mini-day {
		position: relative;
		display: grid;
		place-items: center;
		aspect-ratio: 1;
		min-width: 0;
		padding: 0;
		border: 1px solid transparent;
		border-radius: 8px;
		background: transparent;
		color: var(--text);
		font-size: 11px;
	}
	button.mini-day:hover {
		border-color: var(--border);
		background: var(--bg-hover);
	}
	.mini-day.empty {
		pointer-events: none;
	}
	.mini-day.has-note::after {
		content: '';
		position: absolute;
		bottom: 3px;
		left: 50%;
		width: 3px;
		height: 3px;
		border-radius: 50%;
		background: var(--accent);
		transform: translateX(-50%);
	}
	.mini-day.today {
		border-color: var(--accent);
	}
	.mini-day.active-day {
		background: var(--accent);
		color: var(--accent-contrast);
		font-weight: 700;
	}
	.mini-day.active-day::after {
		background: var(--accent-contrast);
	}
	.mini-task-count {
		position: absolute;
		top: 1px;
		right: 2px;
		min-width: 10px;
		height: 10px;
		padding: 0 2px;
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent);
		font-size: 7px;
		font-weight: 800;
		line-height: 10px;
	}
	.active-day .mini-task-count {
		background: color-mix(in srgb, #fff 22%, transparent);
		color: var(--accent-contrast);
	}
	.mini-task-count.event {
		background: var(--bg-hover);
		color: var(--text);
		box-shadow: inset 0 0 0 1px var(--border);
	}
	.mini-calendar-key {
		display: flex;
		justify-content: center;
		gap: 14px;
		color: var(--text-muted);
		font-size: 10px;
	}
	.mini-calendar-key span {
		display: inline-flex;
		align-items: center;
		gap: 5px;
	}
	.mini-calendar-key i {
		display: inline-block;
		width: 6px;
		height: 6px;
		border-radius: 50%;
	}
	.note-key {
		background: var(--accent);
	}
	.task-key {
		border-radius: 999px !important;
		background: var(--accent-soft);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.event-key {
		background: var(--bg-hover);
		box-shadow: inset 0 0 0 1px var(--border);
	}
	.mini-calendar-state {
		margin: 0;
		color: var(--text-muted);
		font-size: 11px;
		text-align: center;
	}
	.mini-calendar-state.error {
		color: var(--danger);
	}
	.full-calendar-link {
		align-self: center;
		padding: 5px 9px;
		border-radius: 7px;
		font-size: 11px;
		font-weight: 600;
	}
	.full-calendar-link:hover {
		background: var(--bg-hover);
		text-decoration: none !important;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.chip-group {
		display: inline-flex;
		align-items: stretch;
	}
	.chip-group .chip.has-x {
		border-top-right-radius: 0;
		border-bottom-right-radius: 0;
	}
	.chip-x {
		padding: 0 6px;
		border: 1px solid var(--border);
		border-left: 0;
		border-radius: 0 999px 999px 0;
		background: var(--bg-panel);
		color: var(--text-muted);
		font-size: 9px;
		cursor: pointer;
	}
	.chip-x:hover {
		color: var(--danger);
	}
	.tag-add {
		margin-top: 10px;
	}
	.tag-add input {
		width: 100%;
		padding: 6px 9px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: var(--bg);
		color: var(--text);
		font-size: 12px;
	}
	.tag-add input::placeholder {
		color: var(--text-muted);
	}
	.chip {
		font-size: 12px;
		background: var(--accent-soft);
		border-radius: 999px;
		padding: 1px 8px;
		transition: background-color var(--motion-base) var(--ease-out);
	}
	.chip:hover {
		background: var(--bg-active);
	}
	.none {
		color: var(--text-muted);
		margin: 0;
	}
	.related li {
		margin: 0 0 10px;
		padding: 4px;
		border-radius: 6px;
		transition: background-color var(--motion-base) var(--ease-out);
	}
	.related li:hover {
		background: color-mix(in srgb, var(--bg-panel) 78%, transparent);
	}
	.rel-head {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.rel-head a {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.link-btn {
		font-size: 11px;
		padding: 0 6px;
		border: 1px solid var(--border);
		flex-shrink: 0;
		margin-left: auto;
	}
	.linked-mark {
		color: var(--added);
		font-size: 12px;
		margin-left: auto;
	}
	.rel-snippet {
		color: var(--text-muted);
		font-size: 12px;
		margin: 2px 0 4px;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.rel-strength {
		height: 3px;
		background: var(--bg-hover);
		border-radius: 2px;
		overflow: hidden;
	}
	.rel-strength span {
		display: block;
		height: 100%;
		background: var(--accent);
		border-radius: 2px;
		transition: width 420ms var(--ease-out);
	}
	.graph-link {
		display: inline-block;
		margin-top: 6px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.graph-link:hover {
		color: var(--accent);
	}
	.msg {
		margin: auto;
		color: var(--text-muted);
	}
	code {
		font-family: var(--font-mono);
	}

	@media (max-width: 760px) {
		.draft-banner {
			align-items: stretch;
			flex-wrap: wrap;
		}
		.draft-banner > div {
			width: 100%;
			flex-basis: 100%;
		}
		.notebar {
			align-items: stretch;
			flex-direction: column;
			gap: 5px;
			/* The row is one 44px touch target tall and nothing else: 4px of
			   breathing room either side keeps the whole bar at a --m-row-h
			   plus its hairline. */
			padding: 4px var(--m-gutter);
		}
		.meta {
			display: none;
		}
		.path {
			display: none;
		}
		.actions {
			width: 100%;
			overflow-x: auto;
			gap: 6px;
			padding: 0;
			scrollbar-width: none;
			-webkit-overflow-scrolling: touch;
		}
		.actions::-webkit-scrollbar {
			display: none;
		}
		.actions > * {
			flex: 0 0 auto;
		}
		.actions button {
			min-height: 44px;
			padding-inline: 12px;
		}
		.chat-trigger {
			display: grid;
			place-items: center;
			width: 44px;
			min-height: 44px;
			padding: 0;
			color: var(--accent);
		}
		.chat-trigger :global(svg) {
			width: var(--m-icon-bar);
			height: var(--m-icon-bar);
		}
		.save-state {
			width: 64px;
			min-width: 44px;
			margin-right: 2px;
		}
		/* The name is the first line of the document, so it starts on the same
		   left edge the editor's text does. */
		.note-heading {
			padding: 16px var(--m-gutter) 0;
		}
		.body {
			position: relative;
			flex-direction: column;
			overflow-y: auto;
			overflow-x: hidden;
			-webkit-overflow-scrolling: touch;
			scrollbar-width: none;
		}
		.body::-webkit-scrollbar {
			display: none;
		}
		.editor-wrap {
			width: 100%;
			height: auto;
			min-height: 430px;
			flex: 0 0 auto;
		}
		.panel,
		.panel.chat-mode,
		.panel.calendar-mode,
		.panel.activity-mode,
		.panel.collapsed {
			width: 100%;
			max-height: none;
			border-top: 1px solid var(--border);
			border-left: 0;
			flex: 0 0 auto;
		}
		.panel.chat-mode {
			min-height: 420px;
		}
		.panel-scroll {
			overflow: visible;
			/* No safe-area inset here: the tab bar is in flow below this and
			   already carries it. Adding it again just padded dead space. */
			padding: 16px var(--m-gutter) 24px;
		}
		/* Four tabs plus the collapse chevron do not fit a phone once the pills
		   are touch-sized, so the row scrolls the way the note actions do. */
		.panel-tabs {
			padding: 6px var(--m-gutter);
			overflow-x: auto;
			scrollbar-width: none;
			-webkit-overflow-scrolling: touch;
		}
		.panel-tabs::-webkit-scrollbar {
			display: none;
		}
		.panel-tabs > * {
			flex: 0 0 auto;
		}
		.ptab {
			padding: 9px 14px;
			font-size: var(--m-meta);
		}
		/* Same story one row down — a daily note puts nine targets on the rail.
		   Scrolling is safe here because the hover labels, the reason the rail
		   overflows visibly on a laptop, are hidden on a phone. */
		.panel-rail {
			flex-direction: row;
			justify-content: flex-start;
			padding: 8px var(--m-gutter);
			overflow-x: auto;
			scrollbar-width: none;
			-webkit-overflow-scrolling: touch;
		}
		.panel-rail::-webkit-scrollbar {
			display: none;
		}
		.panel-rail > * {
			flex: 0 0 auto;
		}
		.panel-rail button {
			width: 44px;
			height: 44px;
		}
		.rail-label {
			display: none;
		}
		.rail-sep {
			width: 1px;
			height: 20px;
			align-self: center;
			margin: 0 4px;
			border-top: 0;
			border-left: 1px solid var(--border);
		}
		/* The app-wide mobile section label. */
		.panel h3 {
			margin-top: 18px;
			font-size: var(--m-label);
			font-weight: 600;
			letter-spacing: 0.5px;
		}
		.panel h3:first-child {
			margin-top: 0;
		}
		.panel li {
			padding-block: 4px;
		}
		.related li {
			padding: 8px 4px;
		}
		.link-btn {
			min-height: 44px;
			padding-inline: 10px;
			font-size: var(--m-label);
		}
		/* Below 16px iOS zooms the whole page on focus; the field also has no
		   `type`, so the app-wide input floor never matched it. */
		.tag-add input {
			min-height: 44px;
			padding: 9px 12px;
			font-size: var(--m-row-text);
		}
	}
</style>
