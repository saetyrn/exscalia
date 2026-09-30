<script lang="ts">
	import { goto } from '$app/navigation';
	import { onDestroy, onMount, tick, untrack, type Snippet } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from '$lib/motion';
	import {
		api,
		ApiError,
		streamOmniaMessage,
		uploadAttachment,
		type OmniaAction,
		type OmniaConfirmation,
		type OmniaUsage,
		type ThreadMessage
	} from './api';
	import Graphic from './Graphic.svelte';
	import ChatGallery from './media/ChatGallery.svelte';
	import { renderChatMarkdown } from './markdown';
	import { applyAppearanceSettings } from './theme';
	import { confirmDialog, notify } from './ui.svelte';
	import {
		findNoteByName,
		ensureOmniaThread,
		hasUnsettledMessages,
		isChatDirty,
		loadThreadInto,
		MEDIA_TOOLS,
		omniaActivity,
		omniaContext,
		overseerStatus,
		refreshNotes,
		refreshThreads,
		settleReply,
		toChatMsg,
		vault,
		type ChatMsg,
		type ChatState
	} from './stores.svelte';
	import { tabs, routeTitle, closeTab } from './tabs.svelte';

	let {
		chat,
		contextPath = null,
		compact = false,
		media = false,
		placeholder = '',
		empty,
		starters = [],
		ensureThread,
		beforeSend,
		handoff
	}: {
		chat: ChatState;
		contextPath?: string | null;
		compact?: boolean;
		/** This panel is the media assistant — its turns light the (M) badge. */
		media?: boolean;
		placeholder?: string;
		empty?: Snippet;
		/** Example prompts shown on an empty thread. The only discovery path to
		 * ~70 tools is knowing what to type; a blank textarea offers none. */
		starters?: string[];
		/** Resolves which backend thread this panel writes to. Defaults to the
		 * global conversation; the note drawer passes its own so note questions
		 * never land in today's daily thread. */
		ensureThread?: () => Promise<string>;
		beforeSend?: () => Promise<void>;
		/** A question typed somewhere with no thread behind it — the command
		 * palette's free-text row. The owner sets `.text` once its surface has
		 * settled (setting it earlier would race the page's own reset, which
		 * empties the transcript); this panel asks it and clears it. */
		handoff?: { text: string };
	} = $props();

	let input = $state('');
	let busy = $state(false);
	// From the moment a send is accepted until its turn starts (`busy`). On a
	// fresh chat that spans creating the thread, and neither the Send button
	// nor send() itself looked at anything that changed in the meantime.
	let sending = $state(false);
	// Messages typed while OMNIA is still answering. They are held here rather
	// than fired at the server, so they stay visible and cancellable, and this
	// panel only ever has one generation in flight.
	let queue: { id: string; text: string; attachments: string[] }[] = $state([]);
	let scroller: HTMLDivElement | undefined = $state();
	let controller: AbortController | null = $state(null);
	// Set by onDestroy, read by the abort handler in send(): leaving a surface
	// and pressing Stop both land there, and they mean different things.
	let unmounting = false;
	// The thread the running turn writes to. Stop ends that one, not whichever
	// conversation is on screen by the time it is pressed.
	let activeTurnThread: string | null = null;
	let workingCards: string[] = $state([]);

	/** Above this many action cards in one turn, they fold into a tray. */
	const TRAY_THRESHOLD = 3;
	let openTrays: (string | number)[] = $state([]);

	function toggleTray(key: string | number) {
		openTrays = openTrays.includes(key)
			? openTrays.filter((k) => k !== key)
			: [...openTrays, key];
	}
	let queued = $state(false);

	function resolveNote(name: string): string | null {
		return findNoteByName(name)?.path ?? null;
	}

	/** Images staged for the next message. Uploaded to the vault immediately so
	 * the request carries paths, not base64 — thread JSON is git-committed. */
	let pending = $state<
		{ path: string; url: string; name: string; pdf?: boolean; doc?: boolean }[]
	>([]);
	let uploading = $state(0);
	let dragging = $state(false);
	let fileInput: HTMLInputElement | undefined = $state();

	const IMAGE_TYPES = /^image\/(png|jpeg|gif|webp|avif)$/;
	const PDF_TYPE = 'application/pdf';
	const DOCX_TYPE =
		'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
	const MAX_IMAGES = 4;

	// Some browsers report an empty type for a dropped .docx, so fall back to the
	// filename extension.
	const isDocx = (file: File) =>
		file.type === DOCX_TYPE || /\.docx$/i.test(file.name || '');

	const attachable = (file: File) =>
		IMAGE_TYPES.test(file.type) || file.type === PDF_TYPE || isDocx(file);

	async function attachFiles(files: Iterable<File>) {
		const images = [...files].filter(attachable);
		if (!images.length) return;
		const room = MAX_IMAGES - pending.length;
		if (room <= 0) {
			notify(`OMNIA can take ${MAX_IMAGES} files at a time.`, { tone: 'error' });
			return;
		}
		for (const file of images.slice(0, room)) {
			uploading += 1;
			try {
				const { path, url } = await uploadAttachment(file);
				// Filenames are content-hashed, so the same file twice is one blob.
				if (!pending.some((p) => p.path === path)) {
					pending = [
						...pending,
						{
							path,
							url,
							name: file.name || 'file',
							pdf: file.type === PDF_TYPE,
							doc: isDocx(file)
						}
					];
				}
			} catch (cause) {
				notify((cause as Error).message, { tone: 'error' });
			} finally {
				uploading -= 1;
			}
		}
	}

	function onPaste(event: ClipboardEvent) {
		const files = [...(event.clipboardData?.items ?? [])]
			.filter((item) => item.kind === 'file')
			.map((item) => item.getAsFile())
			.filter((file): file is File => file !== null && attachable(file));
		if (!files.length) return;
		event.preventDefault(); // otherwise the image also pastes as a filename
		void attachFiles(files);
	}

	function onDrop(event: DragEvent) {
		dragging = false;
		const files = event.dataTransfer?.files;
		if (!files?.length) return;
		event.preventDefault();
		void attachFiles(files);
	}

	async function send(e?: Event, retryText?: string, retryAttachments?: string[]) {
		e?.preventDefault();
		// A queued message draining, Retry, a starter and a hand-off bring their
		// own text (possibly empty, for an image on its own). Only a send from
		// the box takes what is in it, and only that one may clear it.
		const fromComposer = retryText === undefined;
		// Enter twice, or a double tap, while the first is still resolving its
		// thread: the box still holds the same text, and it went out twice —
		// on a fresh chat, into two new threads.
		if (sending && fromComposer) return;
		const text = fromComposer ? input.trim() : retryText;
		// A retry re-sends the original attachments; without this, asking again
		// silently dropped every image from the question.
		const attached = fromComposer ? pending.map((p) => p.path) : (retryAttachments ?? []);
		// An image on its own is a valid message.
		if (!text && !attached.length) return;
		if (uploading > 0) {
			notify('Still uploading…', { tone: 'error' });
			return;
		}
		if (busy || sending) {
			// Queue it. Sending now would either be dropped (the old behaviour)
			// or race the reply already streaming into this thread — or, for a
			// Retry landing while another send starts, the reply about to.
			queue = [
				...queue,
				{ id: `q${Date.now()}${queue.length}`, text, attachments: attached }
			];
			if (fromComposer) {
				input = '';
				pending = [];
			}
			void scrollDown();
			return;
		}
		sending = true;
		if (beforeSend) {
			try {
				await beforeSend();
			} catch (cause) {
				notify((cause as Error).message, { tone: 'error' });
				sending = false;
				return;
			}
		}
		// Resolve the thread before clearing the composer: if this throws, the
		// typed message and staged attachments are still there to retry with.
		let threadId: string;
		try {
			threadId = chat.sessionId ?? (await (ensureThread ?? ensureOmniaThread)());
		} catch (cause) {
			notify((cause as Error).message || 'Could not start that conversation.', {
				tone: 'error'
			});
			sending = false;
			return;
		}
		// A queue drain or a Retry used to empty the box too, losing whatever
		// had been typed or attached since — and the bubble showed those staged
		// files as sent with a message that never carried them.
		if (fromComposer) {
			input = '';
			pending = [];
		}
		const userMessage: ChatMsg = {
			id: `local-user-${Date.now()}`,
			role: 'user' as const,
			text,
			status: 'complete' as const,
			contextNote: contextPath,
			// Vault paths, as the server stores them; the template maps them to
			// the attachment route.
			images: attached
		};
		const assistantMessage: ChatMsg = {
			id: `local-assistant-${Date.now()}`,
			role: 'assistant' as const,
			text: '',
			status: 'streaming' as const,
			trace: [] as { tool: string; input: string; ok?: boolean; error?: string }[],
			// Declared up front so the running-tool line has a proxied property to
			// track from the first render, not one added mid-stream.
			running: null,
			graphics: [],
			actions: [],
			confirmations: []
		};
		chat.messages.push(userMessage, assistantMessage);
		// Pushing into a $state array stores a *proxy* of the object, not the
		// object itself. Mutating `assistantMessage` from here on would write to
		// the raw copy and never reach the template — the reply would sit at
		// "Working…" forever. Read the elements back out and mutate those.
		const live = chat.messages[chat.messages.length - 1];
		const liveQuestion = chat.messages[chat.messages.length - 2];
		let actionsFired = false;
		busy = true;
		sending = false;
		// The chat this turn writes into, for the finally below: the note page
		// hands its panel another note's chat without remounting it.
		const turnChat = chat;
		turnChat.streaming = true;
		activeTurnThread = threadId;
		controller = new AbortController();

		// Light the tab-bar indicator for as long as this turn runs, and flag it
		// as media work if this is the media assistant or a media tool fires.
		omniaActivity.turns++;
		let turnIsMedia = false;
		const markMedia = () => {
			if (!turnIsMedia) {
				turnIsMedia = true;
				omniaActivity.mediaTurns++;
			}
		};
		if (media) markMedia();

		// Tokens arrive far faster than frames, and each one re-rendered the
		// whole reply so far (marked, KaTeX, DOMPurify) and forced a layout to
		// follow it down. They collect here and reach the bubble once a frame.
		let streamed = '';
		let frame = 0;
		let frameTimer: ReturnType<typeof setTimeout> | undefined;
		const flushStreamed = () => {
			cancelAnimationFrame(frame);
			clearTimeout(frameTimer);
			frame = 0;
			frameTimer = undefined;
			if (!streamed) return;
			live.text += streamed;
			streamed = '';
		};
		const onFrame = () => {
			flushStreamed();
			if (chat.sessionId === threadId) void scrollDown();
		};

		await scrollDown();
		try {
			await streamOmniaMessage(
				threadId,
				text,
				contextPath,
				(event, value) => {
					if (event === 'delta') {
						queued = false;
						streamed += String(value.text ?? '');
						// A hidden page gets no frames, so it takes a timer instead.
						if (!frame && frameTimer === undefined) {
							if (document.hidden) frameTimer = setTimeout(onFrame, 50);
							else frame = requestAnimationFrame(onFrame);
						}
						return;
					}
					// Every other event can read or replace the reply's text, so
					// what has streamed so far lands first.
					flushStreamed();
					if (event === 'status') queued = value.status === 'queued';
					// Live-only: fired just before a tool runs, so the turn says what
					// it is doing rather than "Working…". The `tool` event below is
					// the same call finishing, and clears it.
					if (event === 'tool_start') {
						queued = false;
						live.running = {
							tool: String(value.tool ?? 'tool'),
							summary: String(value.summary ?? '')
						};
					}
					if (event === 'tool') {
						const toolName = String(value.tool ?? 'tool');
						if (MEDIA_TOOLS.has(toolName)) markMedia();
						live.running = null;
						live.trace?.push({
							tool: toolName,
							input: String(value.input ?? ''),
							ok: value.ok !== false,
							error: value.error ? String(value.error) : undefined
						});
					}
					if (event === 'action') {
						const action = value as unknown as OmniaAction;
						live.actions?.push(action);
						actionsFired = true;
						refreshFor(action.refresh, action.paths);
						// Some tools put you somewhere — e.g. "pull up my microecon
						// flashcards" opens the study screen.
						if (action.navigate) void goto(action.navigate);
						// close_tabs closes editor tabs by name-match or all-but-current.
						if (action.close_tabs) {
							const dir = action.close_tabs;
							for (const t of [...tabs.list]) {
								if (dir.others && t.id === tabs.activeId) continue;
								if (
									dir.match &&
									!routeTitle(t.url).toLowerCase().includes(dir.match.toLowerCase())
								)
									continue;
								if (!dir.others && !dir.match) continue;
								closeTab(t.id);
							}
						}
					}
					if (event === 'confirmation_required') {
						live.confirmations?.push(value as unknown as OmniaConfirmation);
					}
					if (event === 'done') vault.pendingCount = Number(value.pending_count ?? 0);
					// Everything below writes into the transcript on screen, which
					// is only this turn's while its thread is the one showing.
					if (chat.sessionId !== threadId) return;
					if (event === 'done') {
						// Settled in place from what the server saved. Reloading the
						// whole thread here also pulled the view back to it after the
						// user had moved on to another conversation mid-reply.
						const saved = value.message as ThreadMessage | undefined;
						const question = value.user_message as ThreadMessage | undefined;
						live.running = null;
						if (saved) Object.assign(live, settleReply(live, saved));
						else live.status = 'complete';
						if (question) Object.assign(liveQuestion, toChatMsg(question));
					}
					if (event === 'error') {
						live.status = 'error';
						live.running = null;
						live.text =
							String((value.message as { text?: string } | undefined)?.text ?? value.detail) ||
							'OMNIA could not finish that response.';
					}
					void scrollDown();
				},
				controller.signal,
				attached,
				{
					selection: omniaContext.selection || undefined,
					view: omniaContext.view || undefined,
					open_tabs: tabs.list.map((t) => routeTitle(t.url)).slice(0, 20),
					selection_range: omniaContext.path
						? {
								from: omniaContext.from,
								to: omniaContext.to,
								head: omniaContext.head,
								doc_len: omniaContext.docLen,
								path: omniaContext.path
							}
						: undefined
				}
			);
			flushStreamed();
			// These were awaited one after another, so the user watched four
			// serial round-trips of dead time after the answer was already on
			// screen. They are independent, and refreshPendingCount is dropped
			// entirely — the `done` event already carried the count.
			const after: Promise<unknown>[] = [
				refreshThreads(),
				// refreshFor() already refreshed notes if any action fired.
				actionsFired ? Promise.resolve() : refreshNotes()
			];
			// `done` settled the reply in place. Without it — a Stop from another
			// device ends the stream bare — or with the transcript reloaded under
			// this turn, only the server's copy says how the turn ended. So it
			// does for an earlier reply left partial: one this turn was queued
			// behind, which finished on the server while `done` settled only
			// this turn's messages.
			if (
				chat.sessionId === threadId &&
				(live.status === 'streaming' ||
					!chat.messages.includes(live) ||
					hasUnsettledMessages(chat, [live, liveQuestion]))
			) {
				after.push(loadThreadInto(chat, threadId));
			}
			await Promise.all(after);
		} catch (err) {
			flushStreamed();
			// Another conversation is on screen: this one reads whole from the
			// server when it is reopened. (The finally below still runs.)
			if (chat.sessionId !== threadId) return;
			if ((err as Error).name === 'AbortError') {
				live.status = 'interrupted';
				// Stop means stopped. Unmounting only ends the local read — the
				// turn runs on the server, which finishes and persists it — so
				// say so rather than claiming a stop that never happened.
				live.text ||= unmounting
					? 'Reply continued on the server.'
					: 'Response stopped.';
			} else {
				live.status = 'error';
				// What streamed before the failure stays. It used to be replaced
				// by the error, though the server keeps running the turn and saved
				// it as it went; the reason goes to a toast instead, or into the
				// bubble when nothing had arrived.
				const reason = (err as Error).message || 'OMNIA could not finish that response.';
				if (live.text) notify(reason, { tone: 'error' });
				else live.text = `Error: ${reason}`;
				// Then read the server's copy, with its ids and how the turn
				// ended. Not after a refusal: an ApiError is the request turned
				// away before anything was saved, and the reload would take the
				// question off the screen.
				if (!(err instanceof ApiError)) {
					try {
						await loadThreadInto(chat, threadId);
					} catch {
						// Still unreachable: what is on screen stays, and the next
						// open reads it again (isChatDirty).
					}
				}
			}
		} finally {
			flushStreamed();
			busy = false;
			turnChat.streaming = false;
			queued = false;
			// Every exit passes here — done, error, Stop, unmount — so the running
			// tool can never be left pulsing under a finished reply.
			live.running = null;
			controller = null;
			activeTurnThread = null;
			omniaActivity.turns = Math.max(0, omniaActivity.turns - 1);
			if (turnIsMedia) omniaActivity.mediaTurns = Math.max(0, omniaActivity.mediaTurns - 1);
			if (chat.sessionId === threadId) await scrollDown();
			// The server's copy says its reply is still being written — the
			// stream dropped while the turn ran on — so keep reading it.
			if (
				!unmounting &&
				chat === turnChat &&
				turnChat.sessionId === threadId &&
				turnChat.messages[turnChat.messages.length - 1]?.status === 'streaming'
			) {
				follow(turnChat, threadId, FOLLOW_EVERY_MS);
			}
			drainQueue();
		}
	}

	function stop() {
		// Abort locally for instant feedback, and tell the server too — the
		// generation runs there and used to keep going until the socket died.
		controller?.abort();
		if (activeTurnThread) void api.stopOmniaThread(activeTurnThread).catch(() => {});
		// Anything queued goes back to the composer. Letting it fire straight
		// after would look like Stop had failed; dropping it would lose typing.
		if (queue.length > 0) {
			const pendingText = queue.map((item) => item.text).filter(Boolean);
			input = [input.trim(), ...pendingText].filter(Boolean).join('\n\n');
			queue = [];
		}
	}

	// A reply the server is still writing with no stream of this panel's
	// behind it: its surface closed, the thread was switched or the stream
	// dropped, and follow() is reading it. The server's Stop is the only thing
	// that ends such a run, so the composer offers it here too.
	let detachedRun = $derived(
		!busy &&
			!chat.streaming &&
			Boolean(chat.sessionId) &&
			chat.messages[chat.messages.length - 1]?.status === 'streaming'
	);
	let stoppingRun = $state(false);

	async function stopDetached() {
		const threadId = chat.sessionId;
		if (!threadId || stoppingRun) return;
		stoppingRun = true;
		try {
			await api.stopOmniaThread(threadId);
		} catch (cause) {
			notify((cause as Error).message || 'Could not stop that reply.', { tone: 'error' });
		} finally {
			stoppingRun = false;
		}
		// The server saves the reply as interrupted; read it in now rather than
		// at the next poll, which may already have given up.
		if (chat.sessionId === threadId) follow(chat, threadId);
	}

	/** Send the next queued message, once the current one is done. Never from
	 * a panel that is gone: nothing could stop that stream. What is still
	 * queued then goes to the chat's draft (onDestroy). */
	function drainQueue() {
		if (unmounting || busy || queue.length === 0) return;
		const [next, ...rest] = queue;
		queue = rest;
		void send(undefined, next.text, next.attachments);
	}

	function unqueue(id: string) {
		const item = queue.find((q) => q.id === id);
		queue = queue.filter((q) => q.id !== id);
		// Put it back where the user can edit it instead of just deleting it.
		if (item?.text) input = [input.trim(), item.text].filter(Boolean).join('\n\n');
	}

	/** Re-ask the question behind `message`, replacing that exchange instead of
	 * appending a second copy of it.
	 *
	 * The old version ignored its anchor entirely and always re-sent the newest
	 * question, so Retry on message 3 of 20 answered message 19. It also sliced
	 * the client array only — the server still held the original exchange, so
	 * the next reload brought it back alongside the duplicate. */
	async function retryFrom(message: ChatMsg) {
		if (busy || sending) return;
		const threadId = chat.sessionId;
		if (!threadId) return;
		// Ids minted locally during an interrupted send don't exist server-side;
		// reload first so we truncate from a message the backend knows about.
		if (chat.messages.some((m) => !m.id || m.id.startsWith('local-'))) {
			await loadThreadInto(chat, threadId);
		}
		const anchor = chat.messages.findIndex((m) => m.id === message.id);
		let start = anchor === -1 ? chat.messages.length - 1 : anchor;
		while (start > 0 && chat.messages[start]?.role !== 'user') start -= 1;
		const question = chat.messages[start];
		if (!question || question.role !== 'user' || !question.id) return;

		// Truncating drops the transcript only: whatever this exchange already
		// did to the vault stays, and a re-run of a non-idempotent tool
		// (add_tasks) does it twice. Say so before replacing it.
		const landed = chat.messages
			.slice(start)
			.flatMap((m) => (m.actions ?? []).filter((a) => a.undoable && !a.undone));
		if (landed.length) {
			const ok = await confirmDialog(
				landed.length === 1
					? 'The change this reply made stays applied and can be undone from OMNIA history.'
					: `The ${landed.length} changes this reply made stay applied and can be undone from OMNIA history.`,
				{ title: 'Retry anyway?', confirmLabel: 'Retry', danger: true }
			);
			// The dialog is a wait: a turn may have started, or the thread
			// reloaded under it, in the meantime.
			if (!ok || busy || sending || chat.messages[start]?.id !== question.id) return;
		}

		const text = question.text;
		const images = [...(question.images ?? [])];
		try {
			await api.truncateOmniaThread(threadId, question.id);
		} catch (cause) {
			notify((cause as Error).message || 'Could not clear that reply.', { tone: 'error' });
			return;
		}
		chat.messages = chat.messages.slice(0, start);
		await send(undefined, text, images);
	}

	// A turn with several tool actions fired one identical refreshNotes() per
	// action, mid-stream, with no coalescing.
	let notesRefresh: ReturnType<typeof setTimeout> | null = null;

	function refreshNotesSoon() {
		if (notesRefresh) return;
		notesRefresh = setTimeout(() => {
			notesRefresh = null;
			void refreshNotes();
		}, 150);
	}

	function refreshFor(refresh: string[], paths: string[] = []) {
		if (refresh.some((item) => ['notes', 'tasks', 'search', 'calendar'].includes(item))) {
			refreshNotesSoon();
		}
		window.dispatchEvent(
			new CustomEvent('exscalia:data-changed', { detail: { refresh, paths } })
		);
		if (refresh.includes('appearance')) {
			void api
				.getAppearanceSettings()
				.then((appearance) => applyAppearanceSettings(appearance));
		}
	}

	function addActions(message: ChatMsg, actions: OmniaAction[]) {
		if (!actions.length) return;
		message.actions ??= [];
		// An approval that lands as its reply finishes is already on the saved
		// copy `done` applied; a second card with the same id breaks the list.
		const known = new Set(message.actions.map((action) => action.id));
		message.actions.push(...actions.filter((action) => !known.has(action.id)));
		refreshFor(
			[...new Set(actions.flatMap((action) => action.refresh))],
			[...new Set(actions.flatMap((action) => action.paths))]
		);
	}

	// Confirmation tokens live ten minutes server-side. `now` moves on only
	// while a live card is on screen, so a lapsed one settles as Expired
	// without anyone pressing Confirm into a 404.
	let now = $state(Date.now());
	$effect(() => {
		const live = chat.messages.some((m) =>
			m.confirmations?.some(
				(c) => (c.status ?? 'pending') === 'pending' && Date.parse(c.expires_at) > now
			)
		);
		if (!live) return;
		const timer = setTimeout(() => (now = Date.now()), 30_000);
		return () => clearTimeout(timer);
	});

	/** What a confirmation card shows, or null once it has settled away. */
	function cardState(confirmation: OmniaConfirmation): 'pending' | 'failed' | 'expired' | null {
		const status = confirmation.status ?? 'pending';
		if (status === 'pending') {
			return Date.parse(confirmation.expires_at) <= now ? 'expired' : 'pending';
		}
		return status === 'failed' || status === 'expired' ? status : null;
	}

	/** The copy of a card on screen now. `done` swaps in the saved reply's
	 * cards, and that can land while a Confirm or Cancel request is out. */
	function cardOnScreen(confirmation: OmniaConfirmation, message: ChatMsg): OmniaConfirmation {
		return message.confirmations?.find((c) => c.token === confirmation.token) ?? confirmation;
	}

	async function approve(confirmation: OmniaConfirmation, message: ChatMsg) {
		if (!chat.sessionId || workingCards.includes(confirmation.token)) return;
		if (Date.parse(confirmation.expires_at) <= Date.now()) {
			confirmation.status = 'expired';
			return;
		}
		const threadId = chat.sessionId;
		workingCards = [...workingCards, confirmation.token];
		try {
			const result = await api.approveOmniaConfirmation(confirmation.token, threadId);
			cardOnScreen(confirmation, message).status = 'approved';
			addActions(message, result.actions);
		} catch (cause) {
			notify((cause as Error).message || 'Could not apply that.', { tone: 'error' });
			const status = cause instanceof ApiError ? cause.status : 0;
			// 403 (another thread or device): the server left the card pending,
			// so it keeps its buttons here too.
			if (status !== 403) {
				cardOnScreen(confirmation, message).status = status === 404 ? 'expired' : 'failed';
			}
			// A bulk confirmation that failed part-way kept its earlier
			// operations, and the server stored their actions on this message.
			// Fetch them so they get their Undo cards now, not on the next reload.
			if (status === 409) {
				const thread = await api.getThread(threadId).catch(() => null);
				const stored = thread?.messages.find((m) =>
					m.confirmations?.some((c) => c.token === confirmation.token)
				);
				const known = new Set((message.actions ?? []).map((action) => action.id));
				addActions(
					message,
					(stored?.actions ?? []).filter((action) => !known.has(action.id))
				);
			}
		} finally {
			workingCards = workingCards.filter((token) => token !== confirmation.token);
		}
	}

	async function cancel(confirmation: OmniaConfirmation, message: ChatMsg) {
		if (!chat.sessionId || workingCards.includes(confirmation.token)) return;
		workingCards = [...workingCards, confirmation.token];
		try {
			await api.cancelOmniaConfirmation(confirmation.token, chat.sessionId);
			cardOnScreen(confirmation, message).status = 'cancelled';
		} catch (cause) {
			notify((cause as Error).message || 'Could not cancel that.', { tone: 'error' });
		} finally {
			workingCards = workingCards.filter((token) => token !== confirmation.token);
		}
	}

	async function undo(action: OmniaAction) {
		if (!chat.sessionId || !action.undoable || workingCards.includes(action.id)) return;
		workingCards = [...workingCards, action.id];
		try {
			const result = await api.undoOmniaAction(action.id, chat.sessionId);
			action.undoable = false;
			action.undone = true;
			refreshFor(result.refresh, action.paths);
		} catch (cause) {
			const reason = (cause as Error).message;
			notify(reason || 'Could not undo that.', { tone: 'error' });
			// Undone elsewhere (OMNIA history, another device) or never
			// undoable: settle the card rather than offer a button that can only
			// fail again. A merge conflict keeps it, since that can be resolved.
			if (cause instanceof ApiError && cause.status === 409) {
				if (/already been undone/i.test(reason)) action.undone = true;
				if (action.undone || /cannot be undone/i.test(reason)) action.undoable = false;
			}
		} finally {
			workingCards = workingCards.filter((id) => id !== action.id);
		}
	}

	// Stick to the bottom only while the user is already there. This fired on
	// every streamed token, so scrolling up to re-read during a long reply
	// yanked you straight back down.
	let stuckToBottom = $state(true);

	function onScroll() {
		if (!scroller) return;
		const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
		stuckToBottom = distance < 60;
	}

	async function scrollDown(force = false) {
		if (!force && !stuckToBottom) return;
		await tick();
		scroller?.scrollTo({ top: scroller.scrollHeight });
		stuckToBottom = true;
	}

	async function copyReply(text: string) {
		try {
			await navigator.clipboard.writeText(text);
			notify('Copied.');
		} catch {
			notify('Could not copy — your browser blocked clipboard access.', { tone: 'error' });
		}
	}

	/** Re-run the last question, replacing the previous answer rather than
	 * appending a second one. */
	async function regenerate() {
		const lastUser = [...chat.messages].reverse().find((m) => m.role === 'user');
		if (lastUser) await retryFrom(lastUser);
	}

	// A transcript behind the server's copy (isChatDirty) is read again when a
	// panel opens on it — keyed on the thread only, since a reply the server
	// saved as interrupted stays "dirty" and must not reload in a loop. While
	// its latest reply is being written somewhere this panel is not reading
	// (another device or view, or the server finishing a turn whose reader
	// closed), it is read every few seconds until it settles: nothing pushes
	// thread changes, the live channel carries notes only.
	const FOLLOW_EVERY_MS = 3000;
	const FOLLOW_FOR_MS = 5 * 60_000;
	let following = 0;
	let followTimer: ReturnType<typeof setTimeout> | undefined;

	function stopFollowing() {
		following += 1;
		clearTimeout(followTimer);
	}

	function follow(target: ChatState, id: string, wait = 0) {
		stopFollowing();
		const run = following;
		const until = Date.now() + FOLLOW_FOR_MS;
		const step = async () => {
			// A turn of this chat's own settles itself from `done`.
			if (run !== following || target.streaming) return;
			try {
				await loadThreadInto(target, id);
			} catch (cause) {
				// Gone for good. Anything else (offline, a restart) tries again.
				if ((cause as { status?: number }).status === 404) return;
			}
			if (run !== following || Date.now() >= until) return;
			if (target.messages[target.messages.length - 1]?.status !== 'streaming') return;
			followTimer = setTimeout(step, FOLLOW_EVERY_MS);
		};
		followTimer = setTimeout(step, wait);
	}

	$effect(() => {
		const target = chat;
		const id = target.sessionId;
		untrack(() => {
			if (id && isChatDirty(target)) follow(target, id);
		});
		// Another thread, another chat, or this panel closing.
		return stopFollowing;
	});

	// Open a conversation at its latest message. Only send() and the jump
	// button scrolled, so a thread picked from the rail, a `?t=` link or a
	// drawer opened on its oldest messages, with the jump button hidden
	// (stuckToBottom starts true). Reading `chat.messages` tracks the array
	// being replaced — a thread opened or read again — never a streamed push.
	let shownThread: string | null | undefined;
	let shownMessages: ChatMsg[] | undefined;
	$effect(() => {
		const id = chat.sessionId;
		const messages = chat.messages;
		untrack(() => {
			const opened =
				id !== shownThread || (!shownMessages?.length && messages.length > 0);
			const replaced = messages !== shownMessages;
			shownThread = id;
			shownMessages = messages;
			if (!opened && !replaced) return;
			// A transcript read again keeps a reader who scrolled up where they are.
			void scrollDown(opened).then(onScroll);
		});
	});

	// A thread opened or swapped in mounts every message at once, and each one
	// flying in was a hundred intros playing together. Messages arriving a few
	// at a time — a send and its reply, one written elsewhere — still do.
	// Before the DOM update, so the flag is up when this update's intros start;
	// they start within the same flush, and it is down again for the next one.
	const INTRO_LIMIT = 2;
	let quietIntros = false;
	let introMessages: ChatMsg[] | undefined;
	$effect.pre(() => {
		const messages = chat.messages;
		untrack(() => {
			const before = introMessages;
			introMessages = messages;
			if (!before || before === messages) return;
			const known = new Set(before.map((m) => m.id));
			const arriving = messages.filter((m) => !m.id || !known.has(m.id)).length;
			if (arriving <= INTRO_LIMIT) return;
			quietIntros = true;
			queueMicrotask(() => (quietIntros = false));
		});
	});

	function messageIntro() {
		return { y: 5, duration: quietIntros ? 0 : 170, easing: cubicOut };
	}

	// Ask what was handed in from outside. An effect, not onMount: the owner
	// sets the text after its own async setup, which is long after this mounts.
	$effect(() => {
		if (!handoff?.text) return;
		const text = handoff.text;
		handoff.text = '';
		void send(undefined, text);
	});

	// The stream is a plain fetch, and it kept reading — and writing into chat
	// state shared with every other mount of this panel — long after the panel
	// that started it was gone. Abort the reader only: unlike Stop, the server
	// is never told to give up, so the answer still finishes and persists and
	// the thread reads whole when it is next opened.
	onDestroy(() => {
		unmounting = true;
		controller?.abort();
		const draft = [input.trim(), ...queue.map((item) => item.text)]
			.filter(Boolean)
			.join('\n\n');
		chat.draft = draft || undefined;
	});

	// onMount, not the script body: a panel remounted for another thread is
	// created before the old one's onDestroy has written the draft.
	onMount(() => {
		if (chat.draft) input = chat.draft;
		chat.draft = undefined;
	});

	/** What the turn cost, for the trace tooltip: "in 12.4k · out 850 ·
	 * cached 9.1k". Empty for the older messages that carry no usage. */
	function usageLabel(usage?: OmniaUsage): string {
		if (!usage) return '';
		const round = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
		const parts: string[] = [];
		if (usage.input !== undefined) parts.push(`in ${round(usage.input)}`);
		if (usage.output !== undefined) parts.push(`out ${round(usage.output)}`);
		if (usage.cache_read) parts.push(`cached ${round(usage.cache_read)}`);
		return parts.join(' · ');
	}
</script>

<div class="chat-panel" class:compact>
	<div class="messages" bind:this={scroller} onscroll={onScroll}>
		{#if chat.messages.length === 0 && empty}
			{@render empty()}
		{/if}
		{#if chat.messages.length === 0 && starters.length > 0 && overseerStatus.available}
			<div class="starters">
				{#each starters as prompt (prompt)}
					<button type="button" disabled={busy || sending} onclick={() => send(undefined, prompt)}>
						{prompt}
					</button>
				{/each}
			</div>
		{/if}
		<!-- Keyed by id, not index: retry/regenerate slice this array and
		     loadThreadInto replaces it wholesale, so index keys would reuse a
		     message's DOM node — including its {@html} body — for a different
		     message's content. -->
		{#each chat.messages as m, i (m.id ?? i)}
			<div class="msg {m.role}" in:fly={messageIntro()}>
				{#if m.trace && m.trace.length > 0}
					<details class="trace">
						<!-- The token cost rides along as a tooltip: worth having,
						     never worth a row of its own. -->
						<summary title={usageLabel(m.usage) || undefined}>{m.trace.length} tool call{m.trace.length === 1 ? '' : 's'}</summary>
						<ul>
							{#each m.trace as t, j (j)}
								<li class:failed={t.ok === false}>
									<code>{t.tool}</code>
									<span>{t.input}</span>
									{#if t.ok === false}
										<span class="tool-error" title={t.error}>failed</span>
									{/if}
								</li>
							{/each}
						</ul>
					</details>
				{/if}
				{#if m.contextNote}
					<span class="context-note">Context: {m.contextNote}</span>
				{/if}
				{#if m.images && m.images.length > 0}
					<div class="sent-images">
						{#each m.images as image (image)}
							{@const href = image.startsWith('/') ? image : `/api/${image}`}
							<!-- Stored as a vault path; the attachment route serves it. -->
							<a {href} target="_blank" rel="noreferrer">
								{#if /\.pdf$/i.test(image)}
									<span class="sent-doc">PDF · {image.split('/').pop()}</span>
								{:else if /\.docx$/i.test(image)}
									<span class="sent-doc">DOC · {image.split('/').pop()}</span>
								{:else}
									<img src={href} alt="Attached" />
								{/if}
							</a>
						{/each}
					</div>
				{/if}
				{#if m.role === 'assistant'}
					<!-- renderChatMarkdown sanitizes all output before injection -->
					{#if m.text}
						<div class="bubble">{@html renderChatMarkdown(m.text, resolveNote, {
							cache: m.status !== 'streaming'
						})}</div>
						<!-- A later round in the same turn: the text is already there
						     and a tool is running again, so the name goes under it. -->
						{#if m.running}
							<span
								class="running thinking"
								title={m.running.summary || undefined}><code>{m.running.tool}</code></span
							>
						{/if}
						{#if m.status !== 'streaming'}
							<div class="reply-tools">
								<button type="button" title="Copy reply" onclick={() => copyReply(m.text)}>
									Copy
								</button>
								<!-- An interrupted or failed reply has its own Retry below;
								     with this row always shown on touch, two read as a glitch. -->
								{#if i === chat.messages.length - 1 && !busy && m.status !== 'interrupted' && m.status !== 'error'}
									<button type="button" title="Ask again" onclick={regenerate}>Retry</button>
								{/if}
							</div>
						{/if}
					{:else if m.status === 'streaming'}
						<div class="bubble thinking" title={m.running?.summary || undefined}>
							{#if m.running}<code>{m.running.tool}</code>{:else}{queued
									? 'Waiting for the current reply…'
									: 'Working…'}{/if}
						</div>
					{/if}
					{#each m.graphics ?? [] as g, gi (gi)}
						<Graphic spec={g} />
					{/each}
					{#each m.galleries ?? [] as gallery, ci (ci)}
						<ChatGallery
							{gallery}
							onpick={(text) => {
								input = [input.trim(), text].filter(Boolean).join('\n');
							}}
						/>
					{/each}
					{#each m.confirmations ?? [] as confirmation (confirmation.token)}
						{@const shown = cardState(confirmation)}
						<!-- A settled confirmation vanishes: approval is answered by the
						     action cards that follow, cancellation by their absence.
						     Only a failure or an expiry stays visible — that needs to
						     be seen. -->
						{#if shown}
							<!-- in: only — this file has already been bitten by outros on
							     keyed-each content leaving orphaned nodes. -->
							<div class="action-card destructive" in:fade={{ duration: 140 }}>
								<div>
									<strong>Confirm action</strong>
									<span>{confirmation.summary}</span>
									{#if confirmation.details?.length}
										<ul class="card-details">
											{#each confirmation.details as line, k (k)}
												<li>{line}</li>
											{/each}
										</ul>
									{/if}
									{#if confirmation.reason}
										<span class="card-reason">{confirmation.reason}</span>
									{/if}
								</div>
								{#if shown === 'pending'}
									<div class="card-buttons">
										<button
											type="button"
											disabled={workingCards.includes(confirmation.token)}
											onclick={() => approve(confirmation, m)}>Confirm</button>
										<button
											type="button"
											disabled={workingCards.includes(confirmation.token)}
											onclick={() => cancel(confirmation, m)}>Cancel</button>
									</div>
								{:else}
									<small>{shown === 'expired' ? 'Expired' : 'Failed'}</small>
								{/if}
							</div>
						{/if}
					{/each}
					<!-- A turn that fired many actions folds them into one tray row;
					     a short burst still shows plain cards. Confirmations never
					     fold — they may need input. -->
					{#if (m.actions?.length ?? 0) > TRAY_THRESHOLD}
						<button
							type="button"
							class="action-card action-tray"
							aria-expanded={openTrays.includes(m.id ?? i)}
							onclick={() => toggleTray(m.id ?? i)}
						>
							<div>
								<strong>Done</strong>
								<span>Edited notes — {m.actions?.length} changes</span>
							</div>
							<span class="tray-chev">{openTrays.includes(m.id ?? i) ? '▾' : '▸'}</span>
						</button>
					{/if}
					{#if (m.actions?.length ?? 0) <= TRAY_THRESHOLD || openTrays.includes(m.id ?? i)}
						{#each m.actions ?? [] as action (action.id)}
							<div class="action-card" class:in-tray={(m.actions?.length ?? 0) > TRAY_THRESHOLD}>
								<div>
									<strong>{action.undone ? 'Undone' : 'Done'}</strong>
									<span>{action.summary}</span>
								</div>
								{#if action.undoable && !action.undone}
									<button
										type="button"
										disabled={workingCards.includes(action.id)}
										onclick={() => undo(action)}>Undo</button>
								{/if}
							</div>
						{/each}
					{/if}
				{:else}
					<div class="bubble">{m.text}</div>
				{/if}
				{#if m.status === 'interrupted' || m.status === 'error'}
					<button class="retry" onclick={() => retryFrom(m)}>Retry</button>
				{/if}
			</div>
		{/each}
		{#each queue as item (item.id)}
			<!-- in: not transition:. An outro on a keyed {#each} item left the node
			     orphaned in the DOM after the queue drained — a bubble for a
			     message that had already been sent, whose × did nothing. The
			     messages above use intro-only for the same reason. -->
			<div class="msg user queued" in:fly={{ y: 5, duration: 160, easing: cubicOut }}>
				<div class="bubble">{item.text}</div>
				<button
					type="button"
					class="unqueue"
					title="Remove from the queue and put it back in the box"
					aria-label="Remove from the queue"
					onclick={() => unqueue(item.id)}>Queued ×</button
				>
			</div>
		{/each}
	</div>

	<!-- Toggled by class rather than {#if}: this appears and disappears
	     constantly while a reply streams, and mounting/unmounting a
	     transitioning element that deep in the tree proved fragile. -->
	<button
		class="jump-down"
		class:visible={!stuckToBottom}
		type="button"
		tabindex={stuckToBottom ? -1 : 0}
		aria-hidden={stuckToBottom}
		title="Jump to the latest message"
		aria-label="Jump to the latest message"
		onclick={() => scrollDown(true)}
	>
		↓
	</button>

	{#if pending.length > 0 || uploading > 0}
		<div class="attached">
			{#each pending as image (image.path)}
				<span class="thumb" transition:fade={{ duration: 120 }}>
					{#if image.pdf || image.doc}
						<!-- A document has no thumbnail; show its name so you can tell two apart. -->
						<span class="doc" title={image.name}>
							<strong>{image.doc ? 'DOC' : 'PDF'}</strong>
							<small>{image.name.replace(/\.(pdf|docx)$/i, '')}</small>
						</span>
					{:else}
						<img src={image.url} alt={image.name} />
					{/if}
					<button
						type="button"
						title="Remove {image.name}"
						aria-label="Remove {image.name}"
						onclick={() => (pending = pending.filter((p) => p.path !== image.path))}
					>
						✕
					</button>
				</span>
			{/each}
			{#if uploading > 0}<span class="uploading">Uploading…</span>{/if}
		</div>
	{/if}

	<form
		class:dragging
		onsubmit={send}
		ondragover={(event) => {
			if (event.dataTransfer?.types.includes('Files')) {
				event.preventDefault();
				dragging = true;
			}
		}}
		ondragleave={() => (dragging = false)}
		ondrop={onDrop}
	>
		<input
			type="file"
			accept="image/png,image/jpeg,image/gif,image/webp,image/avif,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,.docx"
			multiple
			hidden
			bind:this={fileInput}
			onchange={(event) => {
				const target = event.currentTarget;
				void attachFiles(target.files ?? []);
				target.value = ''; // let the same file be picked again
			}}
		/>
		<button
			type="button"
			class="attach"
			title="Attach an image, PDF or Word doc"
			aria-label="Attach an image, PDF or Word doc"
			disabled={!overseerStatus.available}
			onclick={() => fileInput?.click()}
		>
			+
		</button>
		<textarea
			rows="1"
			placeholder={!overseerStatus.available
				? 'OMNIA is offline — add an API key in Settings'
				: busy || detachedRun
					? 'Queue a follow-up…'
					: placeholder}
			bind:value={input}
			disabled={!overseerStatus.available}
			onpaste={onPaste}
			onkeydown={(event) => {
				if (event.key === 'Enter' && !event.shiftKey) {
					event.preventDefault();
					void send();
				}
			}}
		></textarea>
		{#if busy || detachedRun}
			{#if input.trim() || pending.length > 0}
				<button class="primary" type="submit" disabled={uploading > 0}>Queue</button>
			{/if}
			<button
				type="button"
				class="stop"
				disabled={!busy && stoppingRun}
				onclick={busy ? stop : stopDetached}>Stop</button
			>
		{:else}
			<button
				class="primary"
				type="submit"
				disabled={sending ||
					!overseerStatus.available ||
					(!input.trim() && pending.length === 0) ||
					uploading > 0}
			>
				Send
			</button>
		{/if}
	</form>
</div>

<style>
	.chat-panel {
		position: relative; /* anchors the jump-to-bottom button */
		flex: 1;
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.messages {
		flex: 1;
		overflow-y: auto;
		padding: 20px 24px;
	}
	.compact .messages {
		padding: 10px 12px;
	}
	.msg {
		margin: 10px 0;
		display: flex;
		flex-direction: column;
	}
	.msg.user {
		align-items: flex-end;
	}
	.msg.assistant {
		align-items: flex-start;
	}
	.bubble {
		max-width: 85%;
		padding: 8px 14px;
		border-radius: 12px;
		font-size: 14px;
		line-height: 1.55;
		transition:
			background-color var(--motion-slow) var(--ease-out),
			border-color var(--motion-base) var(--ease-out),
			box-shadow var(--motion-base) var(--ease-out);
	}
	.compact .bubble {
		font-size: 13px;
		padding: 6px 10px;
		max-width: 95%;
	}
	.user .bubble {
		background: var(--accent);
		color: var(--accent-contrast);
	}
	.assistant .bubble {
		background: var(--bg-panel);
		border: 1px solid var(--border);
	}
	.bubble :global(p) {
		margin: 0 0 6px;
	}
	.bubble :global(p:last-child) {
		margin-bottom: 0;
	}
	.bubble :global(ul),
	.bubble :global(ol) {
		margin: 4px 0;
		padding-left: 20px;
	}
	.bubble :global(code) {
		font-family: var(--font-mono);
		font-size: 0.85em;
		background: var(--bg-hover);
		padding: 1px 4px;
		border-radius: 4px;
	}
	/* Fenced code, tables and quotes now render — the briefing tells OMNIA it
	   may use them, so they must not spill out of the bubble. */
	.bubble :global(pre) {
		max-width: 100%;
		overflow-x: auto;
		margin: 6px 0;
		padding: 9px 11px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
	}
	.bubble :global(pre code) {
		padding: 0;
		background: none;
		font-size: 0.82em;
		line-height: 1.5;
	}
	.bubble :global(table) {
		display: block;
		max-width: 100%;
		overflow-x: auto;
		margin: 6px 0;
		border-collapse: collapse;
		font-size: 0.88em;
	}
	.bubble :global(th),
	.bubble :global(td) {
		padding: 4px 8px;
		border: 1px solid var(--border);
		text-align: left;
		white-space: nowrap;
	}
	.bubble :global(th) {
		background: var(--bg-hover);
		font-weight: 600;
	}
	.bubble :global(blockquote) {
		margin: 6px 0;
		padding-left: 10px;
		border-left: 2px solid var(--border);
		color: var(--text-muted);
	}
	.bubble :global(h4),
	.bubble :global(h5),
	.bubble :global(h6) {
		margin: 8px 0 4px;
		font-size: 1em;
		font-weight: 700;
	}
	.bubble :global(hr) {
		margin: 8px 0;
		border: none;
		border-top: 1px solid var(--border);
	}
	.bubble :global(img) {
		max-width: 100%;
		border-radius: 6px;
	}
	.bubble :global(.katex-display) {
		overflow-x: auto;
		overflow-y: hidden;
	}
	/* User bubbles are accent-filled; keep inline surfaces legible on it. */
	.user .bubble :global(code),
	.user .bubble :global(th) {
		background: rgb(255 255 255 / 22%);
	}
	.user .bubble :global(a) {
		color: var(--accent-contrast);
		text-decoration: underline;
	}
	.bubble :global(a) {
		color: var(--accent);
	}
	.bubble :global(.dead-link) {
		color: var(--text-muted);
		text-decoration: underline dotted;
	}
	.attached {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		padding: 6px 12px 0;
	}
	.thumb {
		position: relative;
		display: inline-flex;
	}
	.thumb img {
		width: 46px;
		height: 46px;
		object-fit: cover;
		border: 1px solid var(--border);
		border-radius: 7px;
	}
	.thumb button {
		position: absolute;
		top: -5px;
		right: -5px;
		width: 17px;
		height: 17px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 50%;
		background: var(--bg-panel);
		color: var(--text-muted);
		font-size: 9px;
		line-height: 1;
	}
	.thumb button:hover {
		color: var(--danger);
		border-color: var(--danger);
	}
	.doc {
		display: flex;
		width: 46px;
		height: 46px;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 1px;
		overflow: hidden;
		padding: 2px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: var(--bg-panel);
	}
	.doc strong {
		color: var(--accent);
		font-size: 9.5px;
		letter-spacing: 0.4px;
	}
	.doc small {
		max-width: 100%;
		overflow: hidden;
		color: var(--text-muted);
		font-size: 8.5px;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sent-doc {
		display: inline-block;
		padding: 5px 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg-panel);
		color: var(--text);
		font-size: 12px;
	}
	.uploading {
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.attach {
		flex-shrink: 0;
		align-self: flex-end;
		width: 30px;
		height: 30px;
		padding: 0;
		border: 1px solid var(--border);
		color: var(--text-muted);
		font-size: 15px;
		line-height: 1;
	}
	.attach:hover:not(:disabled) {
		color: var(--accent);
		border-color: var(--accent);
	}
	form.dragging {
		outline: 2px dashed var(--accent);
		outline-offset: -3px;
		border-radius: var(--radius);
	}
	.sent-images {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin-bottom: 4px;
	}
	.sent-images img {
		max-width: 150px;
		max-height: 150px;
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.starters {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin: auto 0 6px;
	}
	.starters button {
		padding: 5px 11px;
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--text-muted);
		font-size: 12.5px;
		text-align: left;
	}
	.starters button:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.reply-tools {
		display: flex;
		gap: 4px;
		margin-top: 3px;
		transition: opacity var(--motion-base) var(--ease-out);
	}
	/* Hidden until hover only where there is hover. On touch the row sat
	   invisible but live, so a stray tap under the last reply fired Retry;
	   there it simply shows. A hidden row takes no pointer events, so a tap
	   on it lands on the message and reveals it; focus still reveals it. */
	@media (hover: hover) {
		.reply-tools {
			opacity: 0;
			pointer-events: none;
		}
		.msg:hover .reply-tools,
		.reply-tools:focus-within {
			opacity: 1;
			pointer-events: auto;
		}
	}
	.reply-tools button {
		padding: 1px 7px;
		border: none;
		color: var(--text-muted);
		font-size: 11px;
	}
	.reply-tools button:hover {
		color: var(--accent);
		background: var(--bg-hover);
	}
	.msg.queued .bubble {
		opacity: 0.55;
		border: 1px dashed var(--border);
	}
	.unqueue {
		align-self: flex-end;
		margin-top: 3px;
		font: inherit;
		font-size: 10px;
		color: var(--text-muted);
		background: none;
		border: none;
		padding: 0 2px;
		cursor: pointer;
	}
	.unqueue:hover {
		color: var(--text);
	}
	.jump-down {
		position: absolute;
		right: 14px;
		bottom: 62px;
		z-index: 5;
		opacity: 0;
		pointer-events: none;
		transition: opacity var(--motion-base) var(--ease-out);
		width: 28px;
		height: 28px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 50%;
		background: var(--bg-panel);
		color: var(--text-muted);
		font-size: 13px;
		box-shadow: var(--lift-shadow);
	}
	.jump-down.visible {
		opacity: 1;
		pointer-events: auto;
	}
	.jump-down:hover {
		color: var(--accent);
		border-color: var(--accent);
	}
	.trace li.failed code {
		color: var(--danger);
	}
	.tool-error {
		margin-left: 4px;
		color: var(--danger);
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
	}
	.thinking {
		color: var(--text-muted);
		font-style: italic;
		animation: thinking-pulse 1.15s ease-in-out infinite;
	}
	/* The tool running right now wears the trace's tool-id look inside the
	   placeholder's muted pulse — the bubble's inline-code chip (padding,
	   background) is for markup, not for a status line. */
	.thinking code {
		padding: 0;
		background: none;
		font-family: var(--font-mono);
		font-size: 0.92em;
	}
	/* Same line, under an answer that has already started streaming. */
	.running {
		margin: 4px 3px 0;
		font-size: 12px;
	}
	.context-note {
		margin: 0 3px 3px;
		color: var(--text-muted);
		font-size: 10px;
	}
	.retry {
		align-self: flex-start;
		margin-top: 4px;
		padding: 3px 7px;
		color: var(--accent);
		font-size: 11px;
	}
	.action-card {
		width: min(420px, 92%);
		margin-top: 7px;
		padding: 9px 10px;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg-panel);
		font-size: 12px;
	}
	button.action-tray {
		cursor: pointer;
		text-align: left;
	}
	button.action-tray:hover {
		background: var(--bg-hover);
	}
	.tray-chev {
		color: var(--text-muted);
		flex-shrink: 0;
	}
	/* Cards under an open tray read as its contents. */
	.action-card.in-tray {
		width: min(400px, 88%);
		margin-top: 5px;
	}
	.action-card.destructive {
		border-color: color-mix(in srgb, var(--danger) 55%, var(--border));
	}
	.action-card > div:first-child {
		display: grid;
		gap: 2px;
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.card-details {
		margin: 2px 0 0;
		padding: 0;
		list-style: none;
		display: grid;
		gap: 1px;
		color: var(--text-muted);
		font-size: 11px;
	}
	.card-reason {
		color: var(--text-muted);
		font-size: 11px;
	}
	.action-card strong {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
	}
	.card-buttons {
		display: flex !important;
		flex-direction: row !important;
		gap: 5px !important;
	}
	.action-card button {
		padding: 4px 8px;
		white-space: nowrap;
	}
	.destructive .card-buttons button:first-child {
		color: var(--danger);
	}
	@keyframes thinking-pulse {
		0%,
		100% {
			opacity: 0.55;
		}
		50% {
			opacity: 1;
		}
	}
	.trace {
		font-size: 12px;
		color: var(--text-muted);
		margin-bottom: 4px;
	}
	.trace summary {
		cursor: pointer;
		list-style: none;
		display: inline-flex;
		align-items: center;
		gap: 4px;
		border-radius: 5px;
		padding: 1px 5px 1px 2px;
	}
	.trace summary::-webkit-details-marker {
		display: none;
	}
	.trace summary::before {
		content: '›';
		font-size: 15px;
		line-height: 1;
		transition: transform var(--motion-base) var(--ease-snap);
	}
	.trace[open] summary::before {
		transform: rotate(90deg);
	}
	.trace summary:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.trace ul {
		margin: 4px 0;
		padding-left: 18px;
		transform-origin: top;
		animation: trace-open var(--motion-base) var(--ease-out);
	}
	@keyframes trace-open {
		from {
			opacity: 0;
			transform: translateY(-3px) scaleY(0.96);
		}
		to {
			opacity: 1;
			transform: translateY(0) scaleY(1);
		}
	}
	.trace code {
		color: var(--accent);
	}
	form {
		display: flex;
		gap: 8px;
		padding: 14px 24px;
		border-top: 1px solid var(--border);
	}
	.compact form {
		padding: 8px 10px;
		gap: 6px;
	}
	.compact form button {
		padding: 4px 8px;
	}
	form textarea {
		flex: 1;
		min-width: 0;
		max-height: 140px;
		resize: none;
		font: inherit;
	}
	.stop {
		color: var(--danger);
	}

	@media (max-width: 760px) {
		/* `compact` is for a 320px side panel. On a phone the transcript is the
		   whole screen wide, so both variants get the same reading size here
		   rather than one of them shrinking for a column it no longer sits
		   in. */
		.messages,
		.compact .messages {
			padding: 14px var(--m-gutter);
			overflow-x: hidden;
		}
		.msg {
			margin: 7px 0;
		}
		.bubble,
		.compact .bubble {
			max-width: 90%;
			padding: 10px 12px;
			border-radius: 14px;
			font-size: 15px;
			overflow-wrap: anywhere;
		}
		.starters {
			gap: 8px;
		}
		.starters button {
			padding: 8px 14px;
			font-size: var(--m-meta);
		}
		form,
		.compact form {
			gap: 8px;
			/* No safe-area inset here any more: every mobile chat lives inside
			   <main>, and the tab bar is the last thing in it, so the bar owns
			   the home-indicator strip. Adding it again would float the
			   composer a bar's width of empty space above its own panel. */
			padding: 10px var(--m-gutter);
			background: var(--bg);
		}
		/* `font: inherit` picks up the 15px body text, and anything under 16px
		   makes iOS zoom the page the moment the composer takes focus. */
		form textarea {
			font-size: var(--m-row-text);
		}
		form button,
		.compact form button {
			min-width: 64px;
			padding-inline: 12px;
		}
		/* Square, not the 64px pill the send button wears — and beating that
		   rule's specificity is why this one is written through `form`. */
		form .attach {
			width: 44px;
			min-width: 44px;
			height: 44px;
			font-size: 18px;
		}
		.jump-down {
			right: var(--m-gutter);
			/* Clears the composer: 10px of padding either side of a 44px
			   field, its hairline, and a little air. */
			bottom: 74px;
			width: 44px;
			height: 44px;
			font-size: 16px;
		}
		/* The remove dot stays a 17px circle on the thumbnail's corner; the
		   ::after is the thumb target — 45px, measured from the 15px padding
		   box inside the border. */
		.thumb button {
			min-height: 0;
		}
		.thumb button::after {
			content: '';
			position: absolute;
			inset: -15px;
		}
	}
</style>
