<script lang="ts">
	import ChatPanel from '$lib/ChatPanel.svelte';
	import Glyph from '$lib/Glyph.svelte';
	import OmniaThreadList from '$lib/OmniaThreadList.svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { untrack } from 'svelte';
	import { cubicOut, quintOut } from 'svelte/easing';
	import { fly } from '$lib/motion';
	import { api } from '$lib/api';
	import {
		createOmniaThread,
		ensureOmniaThread,
		isChatDirty,
		omniaHandoff,
		omniaThreads,
		openOmniaThread,
		overseerChat,
		overseerStatus,
		refreshOverseerStatus,
		refreshThreads,
		takeOmniaHandoff,
		vault
	} from '$lib/stores.svelte';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';

	let search = $state('');
	// Mobile only: Rename/Delete live behind the header's dots.
	let actionsOpen = $state(false);

	// The phone gets a different view tree, not a restyled one — /omnia is a
	// conversation list there, and a chat is a screen pushed on top of it. Read
	// synchronously (the app runs with ssr off, so `window` is here) because
	// the setup effect below branches on it: starting at `false` would run the
	// desktop path once on every cold load of ?t=, wiping the very thread the
	// URL asked for.
	let isMobile = $state(
		typeof window !== 'undefined' && window.matchMedia('(max-width: 760px)').matches
	);
	$effect(() => {
		const mq = window.matchMedia('(max-width: 760px)');
		isMobile = mq.matches;
		const onChange = (event: MediaQueryListEvent) => (isMobile = event.matches);
		mq.addEventListener('change', onChange);
		return () => mq.removeEventListener('change', onChange);
	});
	// DAILY is a permanent row: the seam between the day-by-day record and the
	// conversations you named. `dailyView` says which side is showing beneath
	// it; `navDir` points the transition the way the user is moving.
	let dailyView = $state(false);
	let navDir = $state(1);

	// A question typed in the command palette's free-text row, asked here where
	// there is a thread to ask it in. It is handed to the panel only at the end
	// of initialize(): the reset there would otherwise wipe it straight out
	// again.
	let handoff = $state({ text: '' });

	/** What the query string is asking for. On the phone these also decide
	 * which of the two screens is showing, so they are read as one shape. */
	type Route = { resume: boolean; fresh: boolean; thread: string | null; mobile: boolean };

	let threadParam = $derived(page.url.searchParams.get('t'));
	// The chat screen is up whenever the URL names a conversation, asks for a
	// fresh one, or is still resolving which one to resume — the list must not
	// flash into that last gap before the redirect lands.
	let mobileChat = $derived(
		threadParam !== null ||
			page.url.searchParams.has('new') ||
			page.url.searchParams.has('resume')
	);

	// Every trigger is read HERE rather than inside the async body, so the
	// effect stays subscribed to them — this page is keyed on the pathname, and
	// none of them reach it by remounting: Shift-Mod-J from an already-open
	// /omnia only changes the query string, the phone's list-to-chat moves are
	// query changes too, and a hand-off changes nothing in the URL at all.
	// `seq` and not `.text`, because taking the text clears it and a reactive
	// read of that would re-run all this with nothing left to ask.
	$effect(() => {
		const params = page.url.searchParams;
		const resume = params.has('resume');
		const fresh = params.has('new');
		const thread = params.get('t');
		omniaHandoff.seq;
		// Deliberately not a trigger: crossing the breakpoint mid-session swaps
		// the view tree, and re-running setup there would throw away the
		// conversation already on screen.
		const mobile = untrack(() => isMobile);
		void initialize({ resume, fresh, thread, mobile });
	});

	async function initialize(route: Route) {
		// Before the first await, because the panel mounts on this same tick:
		// otherwise the conversation being left behind paints its transcript
		// under the new conversation's title until the fetch lands.
		if (route.mobile && route.thread && overseerChat.sessionId !== route.thread) {
			overseerChat.messages = [];
		}
		await Promise.all([refreshOverseerStatus(), refreshThreads()]);
		if (route.mobile) {
			await initializeMobile(route);
			return;
		}
		if (route.resume && omniaThreads.items.length > 0) {
			// Shift-Mod-J asks for the conversation you were last in; without a
			// thread to resume there is nothing to do but open the blank one.
			try {
				await ensureOmniaThread();
			} catch (cause) {
				notify((cause as Error).message, { tone: 'error' });
			}
		} else {
			// Opening OMNIA starts a fresh chat by default — earlier conversations
			// stay in the rail to pick back up. The thread is created lazily on the
			// first message (ChatPanel's ensureThread below), so opening and leaving
			// OMNIA never litters the rail with empty threads.
			overseerChat.sessionId = null;
			overseerChat.messages = [];
		}
		handoff.text = takeOmniaHandoff();
	}

	// The iOS navigation pair, as two fixed roles rather than a tracked
	// direction: the chat is always the pushed card (in from the right edge,
	// back out the same way, carrying the leading-edge shadow), the list is
	// always the one beneath (recedes 30% and dims under a push, returns and
	// clears on the pop). Playing both halves off the same {#if} toggle gives
	// push AND pop with no bookkeeping. Reduced motion collapses to a fade.
	const NAV_MS = 360;
	function reducedMotion(): boolean {
		return (
			typeof window !== 'undefined' &&
			window.matchMedia('(prefers-reduced-motion: reduce)').matches
		);
	}
	function pushedScreen(_node: Element) {
		if (reducedMotion()) return { duration: 90, css: (t: number) => `opacity: ${t}` };
		return {
			duration: NAV_MS,
			easing: quintOut,
			css: (t: number, u: number) =>
				`transform: translateX(${u * 100}%); box-shadow: -12px 0 32px rgb(0 0 0 / ${0.22 * t});`
		};
	}
	function underScreen(_node: Element) {
		if (reducedMotion()) return { duration: 90, css: (t: number) => `opacity: ${t}` };
		return {
			duration: NAV_MS,
			easing: quintOut,
			css: (_t: number, u: number) =>
				`transform: translateX(${u * -30}%); --nav-dim: ${0.14 * u};`
		};
	}

	/** The phone's four states, all of them addressable. */
	async function initializeMobile(route: Route) {
		if (route.resume) {
			// ?resume names no conversation, so it picks one and hands the URL
			// over to ?t=. Replacing rather than pushing: Back out of the chat
			// must reach the list, not bounce through this redirect again.
			if (omniaThreads.items.length === 0) {
				await goto('/omnia?new=1', { replaceState: true });
				return;
			}
			try {
				const id = await ensureOmniaThread();
				await goto(`/omnia?t=${encodeURIComponent(id)}`, { replaceState: true });
			} catch (cause) {
				notify((cause as Error).message, { tone: 'error' });
			}
			return;
		}
		if (route.thread) {
			// Re-entering the conversation that is already loaded keeps it, unless
			// it is behind the server's copy (isChatDirty): Back mid-reply closes
			// the panel and its reader, and the reply finishes on the server. A
			// turn streaming into these messages is never dirty, so it is never
			// reloaded under. The id alone is not proof of "loaded" — the store
			// restores the last-active id from localStorage at module load with
			// no transcript behind it — so an empty message list still fetches.
			const loaded =
				overseerChat.sessionId === route.thread && overseerChat.messages.length > 0;
			if (!loaded || isChatDirty(overseerChat)) {
				try {
					await openOmniaThread(route.thread);
				} catch (cause) {
					// A transcript already on screen outlives a failed re-read (the
					// next open tries again); only a missing one leaves the route.
					if (!loaded || (cause as { status?: number }).status === 404) {
						notify((cause as Error).message, { tone: 'error' });
						await goto('/omnia', { replaceState: true });
						return;
					}
				}
			}
			handoff.text = takeOmniaHandoff();
			return;
		}
		if (route.fresh) {
			overseerChat.sessionId = null;
			overseerChat.messages = [];
			handoff.text = takeOmniaHandoff();
			return;
		}
		// The list itself, which touches no chat state — a turn started in a
		// conversation goes on streaming while its row sits on screen. The one
		// exception is a question handed over from the command palette: there is
		// no thread here to ask it in, so it opens the blank one and asks there.
		if (omniaHandoff.text) await goto('/omnia?new=1');
	}

	function openThreadRoute(id: string) {
		void goto(`/omnia?t=${encodeURIComponent(id)}`);
	}

	// The desktop panel stays mounted across conversations, and its busy flag,
	// queue and stream belong to the one that started them: switching threads
	// under a running reply carried all three into the next one. A switch
	// remounts it instead; the reply finishes server-side and reads whole when
	// its thread is reopened. Not null → id, though: that is the panel naming
	// the thread it has just created for its first message, mid-send.
	let panelKey = $state(0);
	let panelThread = untrack(() => overseerChat.sessionId);
	let switches = 0;
	$effect.pre(() => {
		const id = overseerChat.sessionId;
		if (id !== panelThread && panelThread !== null) panelKey = ++switches;
		panelThread = id;
	});

	let active = $derived(
		omniaThreads.items.find((thread) => thread.id === overseerChat.sessionId) ?? null
	);
	let filtered = $derived(
		omniaThreads.items.filter((thread) =>
			`${thread.title} ${thread.preview}`.toLowerCase().includes(search.toLowerCase())
		)
	);

	const now = new Date();
	const todayIso = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(
		now.getDate()
	).padStart(2, '0')}`;

	// Newest first, so the submenu reads as a diary and today is at the top.
	let dailyThreads = $derived(
		omniaThreads.items
			.filter((thread) => thread.kind === 'daily')
			.sort((a, b) => (b.date ?? '').localeCompare(a.date ?? ''))
	);
	let namedThreads = $derived(omniaThreads.items.filter((thread) => thread.kind !== 'daily'));
	// The morning check makes today's thread on first open, so it is normally
	// there; fall back to the most recent day when it is not.
	let todayThread = $derived(
		dailyThreads.find((thread) => thread.date === todayIso) ?? dailyThreads[0] ?? null
	);
	// A note's conversation is named after the note, so a note you have talked
	// to more than once yields several identically-named rows. Those get the
	// day they began, in the same shape the daily threads use.
	const dayKey = (iso: string) => new Date(iso).toDateString();

	let repeated = $derived.by(() => {
		const byTitle = new Map<string, number>();
		const byTitleAndDay = new Map<string, number>();
		for (const thread of omniaThreads.items) {
			if (thread.kind === 'daily') continue;
			byTitle.set(thread.title, (byTitle.get(thread.title) ?? 0) + 1);
			const key = `${thread.title}|${dayKey(thread.created_at)}`;
			byTitleAndDay.set(key, (byTitleAndDay.get(key) ?? 0) + 1);
		}
		return { byTitle, byTitleAndDay };
	});

	function threadLabel(thread: { title: string; kind: string; created_at: string }): string {
		if (thread.kind === 'daily' || (repeated.byTitle.get(thread.title) ?? 0) < 2) {
			return thread.title;
		}
		const started = new Date(thread.created_at);
		if (Number.isNaN(started.getTime())) return thread.title;
		const day = started.toLocaleDateString(undefined, {
			weekday: 'long',
			month: 'long',
			day: 'numeric'
		});
		// Two conversations with the same note on the same day would still read
		// alike, so those carry the time as well.
		const sameDay = repeated.byTitleAndDay.get(`${thread.title}|${dayKey(thread.created_at)}`) ?? 0;
		if (sameDay > 1) {
			const time = started.toLocaleTimeString(undefined, {
				hour: 'numeric',
				minute: '2-digit'
			});
			return `${thread.title} · ${day}, ${time}`;
		}
		return `${thread.title} · ${day}`;
	}

	let activeIsDaily = $derived(
		omniaThreads.items.some(
			(thread) => thread.id === overseerChat.sessionId && thread.kind === 'daily'
		)
	);

	async function toggleDaily() {
		if (dailyView) {
			// Back to the conversations you named. Closing is only ever a change
			// of view — it must not drag the chat along with it.
			navDir = -1;
			dailyView = false;
			return;
		}
		// Opening goes straight to today, with the rest of the days beneath it.
		navDir = 1;
		dailyView = true;
		if (todayThread && todayThread.id !== overseerChat.sessionId) {
			await openOmniaThread(todayThread.id);
		}
	}

	async function newThread() {
		try {
			await createOmniaThread();
			// Come back up a level, or the new conversation is created into a
			// list that cannot show it.
			dailyView = false;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function selectThread(id: string) {
		await openOmniaThread(id);
	}

	async function renameActive() {
		actionsOpen = false;
		if (!active) return;
		const title = await promptDialog('Choose a name for this conversation:', {
			title: 'Rename OMNIA thread',
			value: active.title,
			confirmLabel: 'Rename'
		});
		if (!title) return;
		try {
			await api.renameThread(active.id, title);
			await refreshThreads();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function removeActive() {
		actionsOpen = false;
		if (!active) return;
		if (
			!(await confirmDialog(`Delete the OMNIA thread “${active.title}”?`, {
				title: 'Delete conversation',
				confirmLabel: 'Delete',
				danger: true
			}))
		)
			return;
		try {
			await api.deleteThread(active.id);
			overseerChat.sessionId = null;
			overseerChat.messages = [];
			await refreshThreads();
			// The phone goes back to the list it came from; the rail has nowhere
			// to go, so it lands in whatever conversation is next.
			if (isMobile) await goto('/omnia');
			else await ensureOmniaThread();
			notify('Conversation deleted.');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}
</script>

{#if isMobile}
	<div class="m-nav">
	{#if mobileChat}
		<div class="omnia-mobile m-screen m-over" transition:pushedScreen>
			<header class="m-head">
				<button
					type="button"
					class="m-icon"
					aria-label="Back to conversations"
					onclick={() => goto('/omnia')}
				>
					<Glyph name="back" />
				</button>
				<h1>{active ? threadLabel(active) : 'OMNIA'}</h1>
				<button
					type="button"
					class="m-icon"
					aria-label="Conversation actions"
					aria-expanded={actionsOpen}
					disabled={!active}
					onclick={() => (actionsOpen = !actionsOpen)}
				>
					<Glyph name="dots" />
				</button>
			</header>
			{#if actionsOpen && active}
				<div class="m-actions">
					<button type="button" onclick={renameActive}>Rename</button>
					<button type="button" onclick={removeActive}>Delete</button>
				</div>
			{/if}

			<ChatPanel
				chat={overseerChat}
				ensureThread={createOmniaThread}
				{handoff}
				placeholder="Message OMNIA…"
				starters={[
					'Tag my untagged notes',
					'Find duplicate tags and merge them',
					'What did I write this week?',
					'Which notes should link to each other?'
				]}
			/>
		</div>
	{:else}
		<div class="m-screen" transition:underScreen>
			<OmniaThreadList
				threads={omniaThreads.items}
				oncompose={() => goto('/omnia?new=1')}
				onopen={openThreadRoute}
			/>
		</div>
	{/if}
	</div>
{:else}
<div class="omnia-shell">
	<aside>
		<header>
			<strong>Conversations</strong>
			<button class="primary" aria-label="New conversation" title="New conversation" onclick={newThread}>＋</button>
		</header>
		<input type="search" bind:value={search} placeholder="Search threads…" />
		<div class="thread-list">
			{#if search.trim()}
				<!-- Searching looks past the grouping: a specific day is exactly
				     what you are usually hunting for. -->
				{#each filtered as thread (thread.id)}
					<button
						class:active={thread.id === overseerChat.sessionId}
						onclick={() => selectThread(thread.id)}
					>
						<span class="thread-title">
							{#if thread.date === todayIso}<em>Today</em>{/if}
							{threadLabel(thread)}
						</span>
						<small>{thread.preview || 'No messages yet'}</small>
					</button>
				{/each}
				{:else}
					{#if dailyThreads.length > 0}
						<!-- Permanent: the seam between the day-by-day record and the
						     conversations you named yourself. It never scrolls away, so
						     there is always one click back to either side. -->
						<button
							class="daily-row"
							class:active={activeIsDaily}
							aria-expanded={dailyView}
							onclick={toggleDaily}
						>
							<span class="thread-title">Daily</span>
							{#if !dailyView}
								<small>
									{todayThread?.preview ||
										`${dailyThreads.length} day${dailyThreads.length === 1 ? '' : 's'}`}
								</small>
							{/if}
							<span class="chev" class:open={dailyView} aria-hidden="true">›</span>
						</button>
					{/if}
					<!-- Keyed on the side being shown, not on `search`, so switching sides
					     animates but typing does not. In-only, like the route switch in the
					     app shell: an out transition would hold the old side in flow beside
					     the new one and shove the list around mid-slide. -->
					{#key dailyView}
						<div class="level" in:fly={{ x: navDir * 14, duration: 170, easing: cubicOut }}>
							{#if dailyView}
								{#each dailyThreads as thread (thread.id)}
									<button
										class:active={thread.id === overseerChat.sessionId}
										onclick={() => selectThread(thread.id)}
									>
										<span class="thread-title">
											{#if thread.date === todayIso}<em>Today</em>{/if}
											{thread.title}
										</span>
										<small>{thread.preview || 'No messages yet'}</small>
									</button>
								{/each}
							{:else}
								{#each namedThreads as thread (thread.id)}
									<button
										class:active={thread.id === overseerChat.sessionId}
										onclick={() => selectThread(thread.id)}
									>
										<span class="thread-title">{threadLabel(thread)}</span>
										<small>{thread.preview || 'No messages yet'}</small>
									</button>
								{/each}
							{/if}
						</div>
					{/key}
			{/if}
		</div>
	</aside>

	<div class="chat">
		<header class="chat-head">
			<div class="head-title">
				<h1>{active ? threadLabel(active) : 'OMNIA'}</h1>
				{#if overseerStatus.loaded}
					<span class="sub">
						{#if overseerStatus.available}
							{overseerStatus.model}
						{:else}
							offline — <a href="/settings">add an API key in Settings</a>
						{/if}
					</span>
				{/if}
			</div>
			<div class="head-actions">
				<button onclick={renameActive} disabled={!active} title="Rename conversation">Rename</button>
				<button onclick={removeActive} disabled={!active} title="Delete conversation">Delete</button>
			</div>
			{#if vault.pendingCount > 0}
				<a class="pending-link" href="/pending">
					{vault.pendingCount} pending change{vault.pendingCount === 1 ? '' : 's'} →
				</a>
			{/if}
		</header>

		{#key panelKey}
			<ChatPanel
				chat={overseerChat}
				ensureThread={createOmniaThread}
				{handoff}
				placeholder="Message OMNIA…"
				starters={[
					'Tag my untagged notes',
					'Find duplicate tags and merge them',
					'What did I write this week?',
					'Which notes should link to each other?'
				]}
			/>
		{/key}
	</div>
</div>
{/if}

<style>
	.omnia-shell {
		flex: 1;
		min-height: 0;
		display: grid;
		grid-template-columns: 230px minmax(0, 1fr);
	}
	.omnia-shell > aside {
		min-height: 0;
		display: flex;
		flex-direction: column;
		padding: 10px;
		border-right: 1px solid var(--border);
		background: var(--bg-panel);
	}
	aside > header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 3px 2px 9px;
	}
	aside > header strong {
		font-size: 12px;
	}
	aside > header button {
		width: 28px;
		height: 28px;
		padding: 0;
	}
	aside input {
		width: 100%;
		margin-bottom: 7px;
		font-size: 12px;
	}
	.thread-list {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		/* The level slides a few pixels sideways on the way in; without this it
		   would briefly offer a horizontal scrollbar. */
		overflow-x: hidden;
	}
	.thread-list button {
		width: 100%;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 3px;
		padding: 8px;
		border: 0;
		text-align: left;
	}
	.thread-list button.active {
		background: var(--bg-active);
		color: var(--accent);
	}
	.level {
		display: flex;
		flex-direction: column;
	}
	.daily-row {
		position: relative;
		padding-right: 22px !important;
		/* It divides the two sides, so it carries the line between them. */
		border-bottom: 1px solid var(--border);
	}
	.daily-row .thread-title {
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}
	.daily-row .chev {
		position: absolute;
		top: 50%;
		right: 8px;
		color: var(--text-muted);
		font-size: 13px;
		/* Points right at the named conversations, down at the days. */
		transform: translateY(-50%) rotate(0deg);
		transform-origin: center;
		transition: transform var(--motion-base) var(--ease-out);
	}
	.daily-row .chev.open {
		transform: translateY(-50%) rotate(90deg);
	}
	.thread-title {
		max-width: 100%;
		overflow: hidden;
		font-size: 12px;
		font-weight: 650;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.thread-title em {
		margin-right: 4px;
		padding: 1px 4px;
		border-radius: 3px;
		background: var(--accent-soft);
		font-size: 8px;
		font-style: normal;
		text-transform: uppercase;
	}
	.thread-list small {
		width: 100%;
		overflow: hidden;
		color: var(--text-muted);
		font-size: 10px;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.chat {
		min-width: 0;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.chat-head {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 12px 18px;
		border-bottom: 1px solid var(--border);
	}
	h1 {
		display: inline;
		margin: 0;
		font-size: 15px;
	}
	.sub {
		margin-left: 9px;
		color: var(--text-muted);
		font-size: 11px;
	}
	.sub a {
		color: var(--accent);
	}
	.head-actions {
		display: flex;
		gap: 3px;
		margin-left: auto;
	}
	.head-actions button {
		padding: 4px 7px;
		color: var(--text-muted);
		font-size: 11px;
	}
	.pending-link {
		color: var(--accent);
		font-size: 11px;
		font-weight: 700;
		text-decoration: none;
	}
	/* --- Mobile: the conversation screen pushed on top of the list. --- */
	/* The navigation stack. Screens are absolutely stacked so an outgoing one
	   animates without holding layout space, each on an opaque --bg so the
	   card sliding over/under reads as a surface, not a ghost. The ::after
	   layer is the iOS under-view dim, driven by --nav-dim from the
	   transitions; the pushed card carries its own leading-edge shadow. */
	.m-nav {
		position: relative;
		flex: 1;
		min-width: 0;
		min-height: 0;
		overflow: hidden;
	}
	.m-screen {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		background: var(--bg);
	}
	.m-screen::after {
		content: '';
		position: absolute;
		inset: 0;
		background: #000;
		opacity: var(--nav-dim, 0);
		pointer-events: none;
		z-index: 5;
	}
	.m-over {
		z-index: 2;
	}
	.omnia-mobile {
		flex: 1;
		min-width: 0;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.m-head {
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 2px 6px;
		border-bottom: 1px solid var(--border);
	}
	/* Centred between the two 44px controls, and the only thing in the row
	   that can run long — so it is the only thing that shrinks. */
	.m-head h1 {
		flex: 1;
		min-width: 0;
		display: block;
		overflow: hidden;
		margin: 0;
		font-size: var(--m-row-text);
		font-weight: 600;
		text-align: center;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.m-icon {
		flex: 0 0 auto;
		display: grid;
		place-items: center;
		width: 44px;
		height: 44px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: none;
		color: var(--text);
	}
	.m-icon:active {
		background: var(--bg-hover);
	}
	.m-icon :global(svg) {
		display: block;
		width: var(--m-icon-bar);
		height: var(--m-icon-bar);
	}
	/* Rename and Delete, revealed by the dots rather than sitting in the row:
	   a phone header has space for the title or for them, not both. */
	.m-actions {
		flex: 0 0 auto;
		display: flex;
		gap: 6px;
		justify-content: flex-end;
		padding: 4px var(--m-gutter) 6px;
		border-bottom: 1px solid var(--border);
	}
	.m-actions button {
		min-height: 40px;
		padding-inline: 12px;
		color: var(--text-muted);
		font-size: var(--m-meta);
	}
</style>
