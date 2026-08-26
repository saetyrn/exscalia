<script lang="ts">
	import { beforeNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import { holdRouteKey } from '$lib/routekey';
	import { onDestroy, onMount, tick } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from 'svelte/transition';
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
		applyCorrections,
		autocorrectExtension,
		isAutocorrectEnabled,
		markAutocorrectDisabled
	} from '$lib/autocorrect';
	import { moveDraft, readDraft, removeDraft, writeDraft, type LocalDraft } from '$lib/drafts';
	import { CLIENT_ID, type LiveNoteEvent } from '$lib/live';
	import { alignManagedStamps, sameExceptManagedStamps } from '$lib/frontmatter';
	import { mergeThreeWay } from '$lib/merge';
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
		removeFrontmatterTag
	} from '$lib/markdown';
	import {
		findNoteByName,
		ensureNoteThread,
		forgetNoteThread,
		moveNoteThread,
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
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';
	import { wikilinkAutocomplete, wikilinkClick } from '$lib/wikilink';

	let { data } = $props();

	let path = $derived(page.params.path ?? '');
	let note: Note | null = $state(null);
	let backlinks: Backlink[] = $state([]);
	let related: RelatedNote[] = $state([]);
	let linking: string | null = $state(null);
	let loadError: string | null = $state(null);
	let saveState: 'saved' | 'dirty' | 'saving' | 'error' = $state('saved');

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
				if (note) await safePull();
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
			void flush();
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

	// On phones `capture` offers the camera directly — photograph a whiteboard
	// or a slide straight into the note.
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
		// field must exist before the first corrections land.
		autocorrectExtension(),
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
			await flush();
			await api.updateTask(id, { status });
			const loaded = await api.readNote(note.path);
			serverRevision = loaded.revision;
			doc = loaded.content;
			note = loaded;
			editVersion += 1;
			acknowledgedVersion = editVersion;
			suppressEditorChange = true;
			editor?.replaceDoc(loaded.content);
			await removeDraft(note.path);
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
			await flush();
			if (saveState === 'error') throw new Error('The note could not be saved.');
			const task = await api.createTask({
				text,
				due: null,
				due_visible: false
			});
			await refreshNotes();

			if (note?.path === task.path) {
				const loaded = await api.readNote(task.path);
				serverRevision = loaded.revision;
				doc = loaded.content;
				note = loaded;
				editVersion += 1;
				acknowledgedVersion = editVersion;
				suppressEditorChange = true;
				editor?.replaceDoc(loaded.content);
				await removeDraft(task.path);
			}

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
			goto(`/note/${encodePath(existing.path)}`);
			return;
		}
		if (await confirmDialog(`“${target}” does not exist yet. Create it?`, { title: 'Create linked note', confirmLabel: 'Create' })) {
			const created = await api.createNote(`${target}.md`);
			await refreshNotes();
			goto(`/note/${encodePath(created.path)}`);
		}
	}

	// Reactive so Read mode, the outline, and word count track live edits.
	let doc = $state('');

	let headings = $derived(extractHeadings(doc));
	let stats = $derived(noteStats(doc));
	let saveTimer: ReturnType<typeof setTimeout> | null = null;
	let retryTimer: ReturnType<typeof setTimeout> | null = null;
	let retryAttempts = 0;
	const MAX_SAVE_RETRIES = 6;
	// A note being hammered from another device must not spin here forever;
	// after this many merge-and-retry rounds, fall back to a conflict copy.
	let staleRetries = 0;
	const MAX_STALE_RETRIES = 3;
	let serverRevision = '';
	let editVersion = 0;
	let acknowledgedVersion = 0;
	let savePromise: Promise<void> | null = null;
	let recoveryPending = $state(false);
	let serverContentBeforeRecovery = '';
	let loadGeneration = 0;
	let preloadConsumed = false;
	let editor: any = $state();
	let suppressEditorChange = false;
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
		if (saveTimer) {
			clearTimeout(saveTimer);
			saveTimer = null;
		}
		if (retryTimer) {
			clearTimeout(retryTimer);
			retryTimer = null;
		}
		retryAttempts = 0;
		suggestions = [];
		activeSuggestion = null;
		note = null;
		loadError = null;
		saveState = 'saved';
		try {
			// The route load() already fetched this note during navigation. Use
			// it exactly once — later calls are RELOADS after edits and must hit
			// the server, or they would resurrect the stale prefetched copy.
			const usePreload = !preloadConsumed && data.path === path;
			preloadConsumed = true;
			if (usePreload && data.loadError) throw new Error(data.loadError);
			const loaded = usePreload && data.note ? data.note : await api.readNote(path);
			if (generation !== loadGeneration) return;
			serverRevision = loaded.revision;
			serverContentBeforeRecovery = loaded.content;
			editVersion = 0;
			acknowledgedVersion = 0;
			const draft = await readDraft(path);
			if (generation !== loadGeneration) return;
			if (draft && draft.content !== loaded.content) {
				doc = draft.content;
				recoveryPending = true;
				editVersion = 1;
				note = { ...loaded, content: draft.content };
				saveState = 'dirty';
			} else {
				doc = loaded.content;
				recoveryPending = false;
				note = loaded;
				if (draft) await removeDraft(path);
			}
			// A note never edited in this session never scans.
			lastScannedText = doc;
			// A prefetched body was fetched before the click, so it may already be
			// a revision behind. safePull() is the same reconciliation the live
			// stream runs: identical revision costs nothing, a moved one is
			// adopted or merged in place without unmounting the editor.
			if (usePreload && data.prefetched) void safePull();
			backlinks = await api.backlinks(path);
			refreshRelated();
			// A staged review survives a reload, and shows up on the other device.
			void loadReview(path);
		} catch (e) {
			loadError = (e as Error).message;
		}
	}

	async function keepRecoveredDraft() {
		recoveryPending = false;
		saveState = 'dirty';
		scheduleSave(120);
	}

	async function discardRecoveredDraft() {
		recoveryPending = false;
		doc = serverContentBeforeRecovery;
		suppressEditorChange = true;
		editor?.replaceDoc(doc);
		editVersion += 1;
		acknowledgedVersion = editVersion;
		note = note ? { ...note, content: doc } : note;
		saveState = 'saved';
		await removeDraft(path);
	}

	async function refreshRelated() {
		try {
			related = (await api.relatedNotes(path)).related;
		} catch {
			related = [];
		}
		try {
			graphData = await api.graph();
		} catch {
			graphData = null;
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
			await api.addLink(path, r.path);
			await load(); // reload so the editor shows the appended link
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
		const tag = tagInput
			.trim()
			.replace(/^#+/, '')
			.replace(/\s+/g, '-')
			.replace(/[^\w/-]/g, '');
		tagInput = '';
		if (!tag || !note || noteTags.includes(tag)) return;
		const next = addFrontmatterTag(doc || note.content, tag);
		if (next === (doc || note.content)) return;
		// Through the editor, like typing: onDocChange schedules the draft and
		// the autosave, so the revision chain and offline queue stay correct.
		editor?.replaceDoc(next);
	}

	function removeTag(tag: string) {
		if (!note) return;
		const next = removeFrontmatterTag(doc || note.content, tag);
		if (next === (doc || note.content)) return;
		editor?.replaceDoc(next);
	}

	function onDocChange(newDoc: string) {
		if (suppressEditorChange) {
			suppressEditorChange = false;
			doc = newDoc;
			return;
		}
		doc = newDoc;
		editVersion += 1;
		saveState = 'dirty';
		lastEditAt = Date.now();
		scheduleDraft({
			path,
			content: newDoc,
			baseRevision: serverRevision,
			updatedAt: Date.now()
		});
		if (!recoveryPending) scheduleSave(800);
	}

	// --- background autocorrect --------------------------------------------
	//
	// A 1s tick that almost always returns immediately; the guards keep actual
	// scans to at most one per 30s, and only while the user is writing (edited
	// since the last scan, but idle for a few seconds).
	let scanning = false;
	let lastScannedText = '';
	let lastScanAt = 0;
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
			const snapshot: string = editor?.getDoc() ?? doc;
			if (snapshot === lastScannedText) return;
			const now = Date.now();
			if (now - lastEditAt < 4000) return;
			if (now - lastScanAt < 30_000) return;
			const scanPath = path;
			try {
				const result = await api.autocorrect(scanPath, snapshot);
				lastScanAt = Date.now();
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
			} catch {
				// Failed scan: the 30s floor doubles as the retry backoff.
				lastScanAt = Date.now();
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
	// The other device's save used to be discovered only when this one tried to
	// save on top of it, and the server parked the loser in a `(conflict …).md`
	// note. Now every write is announced, and this side folds it in while the
	// two versions are still a few characters apart.
	let remoteConflict: Note | null = $state(null);
	let pulling = false;

	async function onLiveEvent(event: LiveNoteEvent) {
		// Our own save, echoed back to every listener including us.
		if (event.origin === CLIENT_ID) return;
		if (!note || event.path !== path) return;
		if (event.type === 'note_removed') {
			notify('This note was deleted on another device.', { tone: 'error' });
			await goto('/');
			return;
		}
		// Already holding this version, or busy with the previous notification.
		if (event.revision && event.revision === serverRevision) return;
		await safePull();
	}

	/** Guarded pull, shared by the live stream and OMNIA's data-changed events
	 * so the two never race each other into a double merge. */
	async function safePull() {
		if (pulling || recoveryPending || remoteConflict) return;
		pulling = true;
		try {
			await pullRemote();
		} catch {
			// Offline or mid-deploy; the next change (or a reload) recovers.
		} finally {
			pulling = false;
		}
	}

	/** Adopt the server's version wholesale — used when we have nothing local
	 * worth protecting, and when the user picks "Take theirs". */
	function adoptRemote(incoming: Note) {
		suppressEditorChange = true;
		editor?.replaceDoc(incoming.content);
		doc = incoming.content;
		note = incoming;
		serverRevision = incoming.revision;
		editVersion += 1;
		acknowledgedVersion = editVersion;
		saveState = 'saved';
	}

	async function pullRemote() {
		if (!note) return;
		const incoming = await api.readNote(path);
		if (incoming.revision === serverRevision) return;

		// `note.content` is what this device last had in sync with the server,
		// which makes it the common ancestor of both sides' edits.
		const base = note.content;
		const mine = editor?.getDoc() ?? doc;
		const dirty = acknowledgedVersion < editVersion || saveState === 'error';
		if (!dirty || mine === base) {
			adoptRemote(incoming);
			return;
		}

		const merged = mergeThreeWay(base, alignManagedStamps(mine, base), incoming.content);
		if (!merged.ok) {
			remoteConflict = incoming;
			return;
		}
		if (merged.text !== mine) {
			suppressEditorChange = true;
			editor?.replaceDoc(merged.text);
			doc = merged.text;
		}
		// The base for the *next* merge is what the server now holds; our own
		// unsaved work rides on top and is pushed back by the save below.
		note = { ...note, content: incoming.content };
		serverRevision = incoming.revision;
		editVersion += 1;
		saveState = 'dirty';
		scheduleSave(300);
		notify('Merged an outside edit into your draft.');
	}

	function keepMineOverRemote() {
		const incoming = remoteConflict;
		remoteConflict = null;
		if (!incoming || !note) return;
		// Re-base onto their revision so the save is accepted instead of being
		// turned into a conflict copy.
		note = { ...note, content: incoming.content };
		serverRevision = incoming.revision;
		editVersion += 1;
		saveState = 'dirty';
		scheduleSave(120);
	}

	function takeRemoteOverMine() {
		const incoming = remoteConflict;
		remoteConflict = null;
		if (incoming) adoptRemote(incoming);
	}

	$effect(() => {
		const handler = (event: Event) =>
			void onLiveEvent((event as CustomEvent<LiveNoteEvent>).detail);
		window.addEventListener('exscalia:live', handler);
		return () => window.removeEventListener('exscalia:live', handler);
	});

	// Draft writes were unbatched, so every keystroke ran a full IndexedDB
	// round trip. The draft only has to be current enough to survive a crash,
	// and it is flushed explicitly before saving, navigating, or unloading.
	let draftTimer: ReturnType<typeof setTimeout> | null = null;
	let pendingDraft: LocalDraft | null = null;

	function scheduleDraft(draft: LocalDraft) {
		pendingDraft = draft;
		if (draftTimer) return;
		draftTimer = setTimeout(flushDraft, 300);
	}

	function flushDraft() {
		if (draftTimer) {
			clearTimeout(draftTimer);
			draftTimer = null;
		}
		if (pendingDraft) {
			void writeDraft(pendingDraft);
			pendingDraft = null;
		}
	}

	function scheduleSave(delay: number) {
		if (remoteConflict) return;
		if (saveTimer) {
			clearTimeout(saveTimer);
		}
		saveTimer = setTimeout(() => void flush(), delay);
	}

	async function flush(): Promise<void> {
		// Order matters: the draft must land before the save that removes it.
		flushDraft();
		if (saveTimer) {
			clearTimeout(saveTimer);
			saveTimer = null;
		}
		// An unresolved conflict must freeze saving. Without this the loop below
		// re-fired straight after showing the banner and wrote our version over
		// theirs — deciding the conflict for the user before they had answered.
		if (recoveryPending || remoteConflict || !note) return;
		if (acknowledgedVersion >= editVersion) return;
		if (savePromise) return savePromise;
		savePromise = (async () => {
			while (note && acknowledgedVersion < editVersion && !recoveryPending && !remoteConflict) {
				const savingPath: string = note.path;
				const savingContent = doc;
				const savingVersion = editVersion;
				const editorHadFocus = editor?.hasFocus() ?? false;
				saveState = 'saving';
				try {
					// Ask the server to report a newer version rather than parking
					// ours in a conflict copy: a save scheduled before a remote
					// change landed would otherwise create one for edits that
					// merge perfectly well.
					const result = await api.saveNote(
						savingPath,
						savingContent,
						serverRevision,
						staleRetries < MAX_STALE_RETRIES ? 'reject' : 'copy'
					);
					if (note?.path !== savingPath) {
						if (result.status === 'saved') await removeDraft(savingPath);
						return;
					}
					if (result.status === 'stale' && note) {
						// The note moved under us mid-save. Fold their change into
						// ours and try again; only a genuine overlap stops here.
						staleRetries += 1;
						const merged = mergeThreeWay(
							note.content,
							alignManagedStamps(savingContent, note.content),
							result.content
						);
						note = { ...note, content: result.content };
						serverRevision = result.revision;
						if (!merged.ok) {
							remoteConflict = result;
							saveState = 'dirty';
							return;
						}
						if (merged.text !== doc) {
							suppressEditorChange = true;
							editor?.replaceDoc(merged.text);
							doc = merged.text;
						}
						editVersion += 1;
						continue;
					}
					if (result.status === 'conflict_copy' && result.conflict_path) {
						await moveDraft(savingPath, result.conflict_path);
						await refreshNotes();
						notify('Another device changed this note. Your version was saved as a conflict copy.', {
							tone: 'error',
							actionLabel: 'Open copy',
							action: () => goto(`/note/${encodePath(result.conflict_path!)}`),
							timeout: 9000
						});
						await goto(`/note/${encodePath(result.conflict_path)}`);
						return;
					}
					serverRevision = result.revision;
					if (
						savingVersion === editVersion &&
						result.content !== doc &&
						// The server stamps `modified` on every save, so the
						// response always differs from what we sent. Rewriting the
						// document for that alone blurred the editor to <body> on
						// every autosave — losing focus, and losing any keystroke
						// that landed before the blur guard restored it.
						!sameExceptManagedStamps(result.content, doc)
					) {
						doc = result.content;
						suppressEditorChange = true;
						editor?.replaceDoc(result.content, editorHadFocus);
					}
					note = {
						...note,
						content: savingVersion === editVersion ? result.content : note.content,
						meta: result.meta,
						revision: result.revision
					};
					acknowledgedVersion = Math.max(acknowledgedVersion, savingVersion);
					if (acknowledgedVersion === editVersion) {
						saveState = 'saved';
						retryAttempts = 0;
						staleRetries = 0;
						await removeDraft(savingPath);
						void refreshRelated();
						// Tell the sidebar its tag histogram may have moved. This
						// is deliberately NOT `exscalia:data-changed` — this
						// page listens for that one and would reload the note
						// it just saved.
						window.dispatchEvent(new CustomEvent('exscalia:note-saved'));
					}
				} catch (error) {
					saveState = 'error';
					console.error('save failed', error);
					// Backed off and capped. This used to retry every 2.2s with
					// no limit and nothing cancelling it on unmount, so leaving
					// a note while the backend was down left a PUT firing for
					// that note forever — one leaked loop per note visited.
					if (retryTimer) clearTimeout(retryTimer);
					retryAttempts += 1;
					if (retryAttempts <= MAX_SAVE_RETRIES) {
						const delay = Math.min(2200 * 2 ** (retryAttempts - 1), 30_000);
						retryTimer = setTimeout(() => void flush(), delay);
					} else {
						notify('Could not save — check your connection, then edit to retry.', {
							tone: 'error'
						});
					}
					return;
				} finally {
					if (note?.path === savingPath) {
						editor?.restoreFocusIfLost(editorHadFocus);
					}
				}
			}
		})().finally(() => {
			savePromise = null;
			if (
				!recoveryPending &&
				!remoteConflict &&
				acknowledgedVersion < editVersion &&
				saveState !== 'error'
			) {
				void flush();
			}
		});
		return savePromise;
	}

	async function flushBeforeOmnia(): Promise<void> {
		await flush();
		if (saveState === 'error' || acknowledgedVersion < editVersion) {
			throw new Error('Save this note successfully before sending it to OMNIA.');
		}
	}

	beforeNavigate(() => {
		void flush();
	});

	// The page had no teardown at all, so a save-retry loop outlived the route.
	onDestroy(() => {
		flushDraft();
		if (saveTimer) clearTimeout(saveTimer);
		if (retryTimer) clearTimeout(retryTimer);
		if (scanTimer) clearInterval(scanTimer);
	});

	$effect(() => {
		const handler = (event: BeforeUnloadEvent) => {
			if (saveState === 'dirty' || saveState === 'saving' || saveState === 'error') {
				void writeDraft({
					path,
					content: doc,
					baseRevision: serverRevision,
					updatedAt: Date.now()
				});
				event.preventDefault();
				event.returnValue = '';
			}
		};
		window.addEventListener('beforeunload', handler);
		return () => window.removeEventListener('beforeunload', handler);
	});

	let isPinned = $derived.by(() => {
		const n: Note | null = note;
		return !!n?.meta?.pinned;
	});

	async function togglePin() {
		if (!note) return;
		try {
			await flush();
			await api.pinNote(note.path, !isPinned);
			await load(); // reload so the editor doc includes the frontmatter change
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
		if (note && note.path === sourcePath) {
			note = { ...note, path: target };
			// Before the keying effect sees the new path — else it rebuilds the
			// EditorView and cursor, scroll and undo history go with it.
			editor?.adoptPath(target);
			if (result.revision) serverRevision = result.revision;
			if (
				result.content !== undefined &&
				result.content !== doc &&
				saveState === 'saved'
			) {
				doc = result.content;
				note = { ...note, content: result.content };
				suppressEditorChange = true;
				editor?.replaceDoc(result.content);
			}
		}
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
			await flush();
			const result = await api.renameNote(sourcePath, target);
			await moveDraft(sourcePath, target);
			moveNoteThread(sourcePath, target);
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

		titleSaving = true;
		try {
			await flush();
			const result = await api.renameNote(sourcePath, target, newTitle);
			await moveDraft(sourcePath, target);
			moveNoteThread(sourcePath, target);
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
			await removeDraft(note.path);
			forgetNoteThread(note.path);
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
			<button onclick={discardRecoveredDraft}>Discard draft</button>
			<button class="primary" onclick={keepRecoveredDraft}>Keep and save</button>
		</div>
	{/if}
	{#if remoteConflict}
		<div class="draft-banner" role="status">
			<div>
				<strong>Edited on another device, in the same place.</strong>
				<span>Both versions changed the same lines, so pick one.</span>
			</div>
			<button onclick={takeRemoteOverMine}>Take theirs</button>
			<button class="primary" onclick={keepMineOverRemote}>Keep mine</button>
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
				capture="environment"
				multiple
				onchange={onFilesPicked}
			/>
			<!-- The one view: an always-live editor. -->
			<div class="editor-host">
				<Editor
					bind:this={editor}
					path={note.path}
					content={note.content}
					onChange={onDocChange}
					onTaskCommand={createTaskFromCommand}
					onSave={() => void flush()}
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
					beforeSend={flushBeforeOmnia}
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
						<li><a href="/note/{encodePath(b.path)}">{b.title || b.path}</a></li>
					{/each}
				</ul>
			{/if}

			{#if related.length > 0}
				<h3>Related</h3>
				<ul class="related">
					{#each related as r (r.path)}
						<li in:fly={{ y: 4, duration: 170, easing: cubicOut }}>
							<div class="rel-head">
								<a href="/note/{encodePath(r.path)}">{r.title}</a>
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
			beforeSend={flushBeforeOmnia}
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
		transform: translateX(2px);
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
		transform: translate(4px, -50%) scale(0.96);
		transform-origin: right center;
		transition:
			opacity var(--motion-fast) var(--ease-out),
			transform var(--motion-fast) var(--ease-snap);
	}
	.panel-rail button:hover .rail-label,
	.panel-rail button:focus-visible .rail-label {
		opacity: 1;
		transform: translate(0, -50%) scale(1);
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
		color: #fff;
		font-weight: 700;
	}
	.mini-day.active-day::after {
		background: #fff;
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
		color: #fff;
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
		transition:
			transform var(--motion-base) var(--ease-snap),
			background-color var(--motion-base) var(--ease-out);
	}
	.related li:hover {
		transform: translateX(2px);
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
			padding: 7px 10px;
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
			gap: 4px;
			padding: 1px 0 3px;
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
			min-height: 36px;
			padding-inline: 9px;
		}
		.chat-trigger {
			display: grid;
			place-items: center;
			width: 40px;
			min-height: 36px;
			padding: 0;
			color: var(--accent);
		}
		.chat-trigger :global(svg) {
			width: 19px;
			height: 19px;
		}
		.save-state {
			width: 64px;
			min-width: 44px;
			margin-right: 2px;
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
			padding: 16px 14px 24px;
		}
		.panel-rail {
			flex-direction: row;
			justify-content: flex-start;
			padding: 8px 14px;
		}
		.panel-rail button {
			width: 38px;
			height: 34px;
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
		.panel h3 {
			margin-top: 18px;
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
			min-height: 34px;
		}
	}
</style>
