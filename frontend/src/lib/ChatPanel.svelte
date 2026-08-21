<script lang="ts">
	import { goto } from '$app/navigation';
	import { tick, type Snippet } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from 'svelte/transition';
	import {
		api,
		streamOmniaMessage,
		uploadAttachment,
		type OmniaAction,
		type OmniaConfirmation
	} from './api';
	import Graphic from './Graphic.svelte';
	import ChatGallery from './media/ChatGallery.svelte';
	import { renderChatMarkdown } from './markdown';
	import { applyAppearanceSettings } from './theme';
	import { notify } from './ui.svelte';
	import {
		findNoteByName,
		ensureOmniaThread,
		loadThreadInto,
		MEDIA_TOOLS,
		omniaActivity,
		omniaContext,
		overseerStatus,
		refreshNotes,
		refreshThreads,
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
		beforeSend
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
	} = $props();

	let input = $state('');
	let busy = $state(false);
	// Messages typed while OMNIA is still answering. They are held here rather
	// than fired at the server, so they stay visible and cancellable, and this
	// panel only ever has one generation in flight.
	let queue: { id: string; text: string; attachments: string[] }[] = $state([]);
	let scroller: HTMLDivElement | undefined = $state();
	let controller: AbortController | null = $state(null);
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
	let pending = $state<{ path: string; url: string; name: string; pdf?: boolean }[]>([]);
	let uploading = $state(0);
	let dragging = $state(false);
	let fileInput: HTMLInputElement | undefined = $state();

	const IMAGE_TYPES = /^image\/(png|jpeg|gif|webp|avif)$/;
	const PDF_TYPE = 'application/pdf';
	const MAX_IMAGES = 4;

	const attachable = (file: File) => IMAGE_TYPES.test(file.type) || file.type === PDF_TYPE;

	async function attachFiles(files: Iterable<File>) {
		const images = [...files].filter(attachable);
		if (!images.length) return;
		const room = MAX_IMAGES - pending.length;
		if (room <= 0) {
			notify(`OMNIA can look at ${MAX_IMAGES} files at a time.`, { tone: 'error' });
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
						{ path, url, name: file.name || 'file', pdf: file.type === PDF_TYPE }
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
		const text = retryText ?? input.trim();
		// A retry re-sends the original attachments; without this, asking again
		// silently dropped every image from the question.
		const attached = retryText ? (retryAttachments ?? []) : pending.map((p) => p.path);
		// An image on its own is a valid message.
		if (!text && !attached.length) return;
		if (uploading > 0) {
			notify('Still uploading…', { tone: 'error' });
			return;
		}
		if (busy) {
			// Queue it. Sending now would either be dropped (the old behaviour)
			// or race the reply already streaming into this thread.
			queue = [
				...queue,
				{ id: `q${Date.now()}${queue.length}`, text, attachments: attached }
			];
			if (!retryText) {
				input = '';
				pending = [];
			}
			void scrollDown();
			return;
		}
		if (beforeSend) {
			try {
				await beforeSend();
			} catch (cause) {
				notify((cause as Error).message, { tone: 'error' });
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
			return;
		}
		input = '';
		const attachedUrls = pending.map((p) => p.url);
		pending = [];
		const userMessage: ChatMsg = {
			id: `local-user-${Date.now()}`,
			role: 'user' as const,
			text,
			status: 'complete' as const,
			contextNote: contextPath,
			images: attachedUrls
		};
		const assistantMessage: ChatMsg = {
			id: `local-assistant-${Date.now()}`,
			role: 'assistant' as const,
			text: '',
			status: 'streaming' as const,
			trace: [] as { tool: string; input: string; ok?: boolean; error?: string }[],
			graphics: [],
			actions: [],
			confirmations: []
		};
		chat.messages.push(userMessage, assistantMessage);
		// Pushing into a $state array stores a *proxy* of the object, not the
		// object itself. Mutating `assistantMessage` from here on would write to
		// the raw copy and never reach the template — the reply would sit at
		// "Working…" until loadThreadInto() swapped the array wholesale, and
		// Stop/error (which never reach that line) would freeze it forever.
		// Read the element back out and mutate that.
		const live = chat.messages[chat.messages.length - 1];
		let actionsFired = false;
		busy = true;
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

		await scrollDown();
		try {
			await streamOmniaMessage(
				threadId,
				text,
				contextPath,
				(event, value) => {
					if (event === 'status') queued = value.status === 'queued';
					if (event === 'delta') {
						queued = false;
						live.text += String(value.text ?? '');
					}
					if (event === 'tool') {
						const toolName = String(value.tool ?? 'tool');
						if (MEDIA_TOOLS.has(toolName)) markMedia();
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
					if (event === 'done') {
						live.status = 'complete';
						vault.pendingCount = Number(value.pending_count ?? 0);
						const saved = value.message as
							| { actions?: OmniaAction[]; confirmations?: OmniaConfirmation[] }
							| undefined;
						if (saved?.actions) live.actions = saved.actions;
						if (saved?.confirmations) live.confirmations = saved.confirmations;
					}
					if (event === 'error') {
						live.status = 'error';
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
			// These were awaited one after another, so the user watched four
			// serial round-trips of dead time after the answer was already on
			// screen. They are independent, and refreshPendingCount is dropped
			// entirely — the `done` event already carried the count.
			// Reload into THIS panel's chat, not the global one.
			await Promise.all([
				loadThreadInto(chat, threadId),
				refreshThreads(),
				// refreshFor() already refreshed notes if any action fired.
				actionsFired ? Promise.resolve() : refreshNotes()
			]);
		} catch (err) {
			if ((err as Error).name === 'AbortError') {
				live.status = 'interrupted';
				live.text ||= 'Response stopped.';
			} else {
				live.status = 'error';
				live.text = `Error: ${(err as Error).message}`;
			}
		} finally {
			busy = false;
			queued = false;
			controller = null;
			omniaActivity.turns = Math.max(0, omniaActivity.turns - 1);
			if (turnIsMedia) omniaActivity.mediaTurns = Math.max(0, omniaActivity.mediaTurns - 1);
			await scrollDown();
			drainQueue();
		}
	}

	function stop() {
		// Abort locally for instant feedback, and tell the server too — the
		// generation runs there and used to keep going until the socket died.
		controller?.abort();
		if (chat.sessionId) void api.stopOmniaThread(chat.sessionId).catch(() => {});
		// Anything queued goes back to the composer. Letting it fire straight
		// after would look like Stop had failed; dropping it would lose typing.
		if (queue.length > 0) {
			const pendingText = queue.map((item) => item.text).filter(Boolean);
			input = [input.trim(), ...pendingText].filter(Boolean).join('\n\n');
			queue = [];
		}
	}

	/** Send the next queued message, once the current one is done. */
	function drainQueue() {
		if (busy || queue.length === 0) return;
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
		if (busy) return;
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

	async function approve(confirmation: OmniaConfirmation, message: ChatMsg) {
		if (!chat.sessionId || workingCards.includes(confirmation.token)) return;
		workingCards = [...workingCards, confirmation.token];
		try {
			const result = await api.approveOmniaConfirmation(confirmation.token, chat.sessionId);
			confirmation.status = 'approved';
			if (result.actions.length) {
				message.actions ??= [];
				message.actions.push(...result.actions);
				refreshFor(
					[...new Set(result.actions.flatMap((action) => action.refresh))],
					[...new Set(result.actions.flatMap((action) => action.paths))]
				);
			}
		} catch {
			confirmation.status = 'failed';
		} finally {
			workingCards = workingCards.filter((token) => token !== confirmation.token);
		}
	}

	async function cancel(confirmation: OmniaConfirmation) {
		if (!chat.sessionId || workingCards.includes(confirmation.token)) return;
		workingCards = [...workingCards, confirmation.token];
		try {
			await api.cancelOmniaConfirmation(confirmation.token, chat.sessionId);
			confirmation.status = 'cancelled';
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
</script>

<div class="chat-panel" class:compact>
	<div class="messages" bind:this={scroller} onscroll={onScroll}>
		{#if chat.messages.length === 0 && empty}
			{@render empty()}
		{/if}
		{#if chat.messages.length === 0 && starters.length > 0 && overseerStatus.available}
			<div class="starters">
				{#each starters as prompt (prompt)}
					<button type="button" disabled={busy} onclick={() => send(undefined, prompt)}>
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
			<div class="msg {m.role}" in:fly={{ y: 5, duration: 170, easing: cubicOut }}>
				{#if m.trace && m.trace.length > 0}
					<details class="trace">
						<summary>{m.trace.length} tool call{m.trace.length === 1 ? '' : 's'}</summary>
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
						<div class="bubble">{@html renderChatMarkdown(m.text, resolveNote)}</div>
						{#if m.status !== 'streaming'}
							<div class="reply-tools">
								<button type="button" title="Copy reply" onclick={() => copyReply(m.text)}>
									Copy
								</button>
								{#if i === chat.messages.length - 1 && !busy}
									<button type="button" title="Ask again" onclick={regenerate}>Retry</button>
								{/if}
							</div>
						{/if}
					{:else if m.status === 'streaming'}
						<div class="bubble thinking">
							{queued ? 'Waiting for the current reply…' : 'Working…'}
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
						<!-- A settled confirmation vanishes: approval is answered by the
						     action cards that follow, cancellation by their absence.
						     Only a failure stays visible — that needs to be seen. -->
						{#if !confirmation.status || confirmation.status === 'pending' || confirmation.status === 'failed'}
							<!-- in: only — this file has already been bitten by outros on
							     keyed-each content leaving orphaned nodes. -->
							<div class="action-card destructive" in:fade={{ duration: 140 }}>
								<div>
									<strong>Confirm action</strong>
									<span>{confirmation.summary}</span>
								</div>
								{#if !confirmation.status || confirmation.status === 'pending'}
									<div class="card-buttons">
										<button
											type="button"
											disabled={workingCards.includes(confirmation.token)}
											onclick={() => approve(confirmation, m)}>Confirm</button>
										<button
											type="button"
											disabled={workingCards.includes(confirmation.token)}
											onclick={() => cancel(confirmation)}>Cancel</button>
									</div>
								{:else}
									<small>{confirmation.status}</small>
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
					{#if image.pdf}
						<!-- A PDF has no thumbnail; show its name so you can tell two apart. -->
						<span class="doc" title={image.name}>
							<strong>PDF</strong>
							<small>{image.name.replace(/\.pdf$/i, '')}</small>
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
			accept="image/png,image/jpeg,image/gif,image/webp,image/avif,application/pdf"
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
			title="Attach an image or PDF"
			aria-label="Attach an image or PDF"
			disabled={!overseerStatus.available}
			onclick={() => fileInput?.click()}
		>
			+
		</button>
		<textarea
			rows="1"
			placeholder={!overseerStatus.available
				? 'OMNIA is offline — add an API key in Settings'
				: busy
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
		{#if busy}
			{#if input.trim() || pending.length > 0}
				<button class="primary" type="submit" disabled={uploading > 0}>Queue</button>
			{/if}
			<button type="button" class="stop" onclick={stop}>Stop</button>
		{:else}
			<button
				class="primary"
				type="submit"
				disabled={!overseerStatus.available ||
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
		color: #fff;
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
		color: #fff;
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
		opacity: 0;
		transition: opacity var(--motion-base) var(--ease-out);
	}
	.msg:hover .reply-tools,
	.reply-tools:focus-within {
		opacity: 1;
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
		.messages,
		.compact .messages {
			padding: 12px 10px;
			overflow-x: hidden;
		}
		.msg {
			margin: 7px 0;
		}
		.bubble,
		.compact .bubble {
			max-width: 94%;
			padding: 8px 10px;
			font-size: 14px;
			overflow-wrap: anywhere;
		}
		form,
		.compact form {
			gap: 7px;
			/* No safe-area inset here any more: every mobile chat lives inside
			   <main>, and the tab bar is the last thing in it, so the bar owns
			   the home-indicator strip. Adding it again would float the
			   composer a bar's width of empty space above its own panel. */
			padding: 9px 10px;
			background: var(--bg);
		}
		form button,
		.compact form button {
			min-width: 64px;
			padding-inline: 12px;
		}
	}
</style>
