<script lang="ts">
	// There is no home page: the home route is today's daily note. Everything
	// that "goes home" (a new tab, EXSCALIA from Settings, a deleted note, the
	// phone's Daily tab) lands there, made from the template if the day has
	// none yet.
	//
	// onMount, not afterNavigate: the layout mounts pages only once the
	// session is known, after the first navigation has already finished.
	import { onMount } from 'svelte';
	import { encodePath } from '$lib/api';
	import { lastLeft, openToday, todayPath } from '$lib/daily';
	import { vault } from '$lib/stores.svelte';
	import { notify } from '$lib/ui.svelte';

	// Only shown when the redirect does not happen.
	let stay = $state(false);

	onMount(() => {
		const today = todayPath();
		// Deleting today's note navigates here; making it again at once would
		// undo the delete. The wordmark it is, until the user asks for Daily.
		if (
			lastLeft.path === `/note/${encodePath(today)}` &&
			!vault.notes.some((note) => note.path === today)
		) {
			stay = true;
			return;
		}
		openToday({ replaceState: true }).catch((cause) => {
			stay = true;
			notify((cause as Error).message, { tone: 'error' });
		});
	});
</script>

{#if stay}
	<div class="landing">
		<h1 class="wordmark">EXSCALIA</h1>
	</div>
{/if}

<style>
	.landing {
		flex: 1;
		min-height: 0;
		display: flex;
		align-items: flex-end;
		justify-content: flex-end;
		padding: 20px 24px;
	}
	.wordmark {
		margin: 0;
		color: var(--text-muted);
		font-size: 20px;
	}
</style>
