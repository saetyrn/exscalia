<script lang="ts">
	import ChatPanel from '$lib/ChatPanel.svelte';
	import { cubicOut } from 'svelte/easing';
	import { fly } from 'svelte/transition';
	import { api } from '$lib/api';
	import {
		createOmniaThread,
		ensureOmniaThread,
		omniaThreads,
		openOmniaThread,
		overseerChat,
		overseerStatus,
		refreshOverseerStatus,
		refreshThreads,
		vault
	} from '$lib/stores.svelte';
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';

	let search = $state('');
	let threadMenuOpen = $state(false);
	// DAILY is a permanent row: the seam between the day-by-day record and the
	// conversations you named. `dailyView` says which side is showing beneath
	// it; `navDir` points the transition the way the user is moving.
	let dailyView = $state(false);
	let navDir = $state(1);

	$effect(() => {
		void initialize();
	});

	async function initialize() {
		await Promise.all([refreshOverseerStatus(), refreshThreads()]);
		try {
			await ensureOmniaThread();
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

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
			threadMenuOpen = false;
			// Come back up a level, or the new conversation is created into a
			// list that cannot show it.
			dailyView = false;
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	async function selectThread(id: string) {
		await openOmniaThread(id);
		threadMenuOpen = false;
	}

	async function renameActive() {
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
			await ensureOmniaThread();
			notify('Conversation deleted.');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}
</script>

<div class="omnia-shell">
	<aside class:mobile-open={threadMenuOpen}>
		<header>
			<strong>Conversations</strong>
			<button class="primary" onclick={newThread}>＋</button>
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
			<button class="mobile-threads" onclick={() => (threadMenuOpen = !threadMenuOpen)} aria-label="Choose conversation">☰</button>
			<div>
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

		<ChatPanel
			chat={overseerChat}
			placeholder="Message OMNIA…"
			starters={[
				'Tag my untagged notes',
				'Find duplicate tags and merge them',
				'What did I write this week?',
				'Which notes should link to each other?'
			]}
		/>
	</div>
</div>

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
	.mobile-threads {
		display: none;
	}
	@media (max-width: 760px) {
		.omnia-shell {
			display: block;
			position: relative;
		}
		.omnia-shell > aside {
			position: absolute;
			inset: 0 auto 0 0;
			z-index: 25;
			width: min(82vw, 290px);
			transform: translateX(-105%);
			transition: transform var(--motion-base) var(--ease-out);
			box-shadow: 14px 0 40px color-mix(in srgb, #000 28%, transparent);
		}
		.omnia-shell > aside.mobile-open {
			transform: translateX(0);
		}
		.mobile-threads {
			display: block;
			width: 32px;
			height: 32px;
			padding: 0;
		}
		.chat {
			height: 100%;
		}
		.chat-head {
			padding: 8px 10px;
		}
		.sub {
			display: block;
			margin: 1px 0 0;
		}
		.head-actions button {
			font-size: 0;
		}
		.head-actions button::first-letter {
			font-size: 12px;
		}
		.pending-link {
			display: none;
		}
	}
</style>
