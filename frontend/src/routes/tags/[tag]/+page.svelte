<script lang="ts">
	import { page } from '$app/state';
	import { api } from '$lib/api';
	import { noteHref } from '$lib/recipes/links';

	let tag = $derived(page.params.tag ?? '');
	let notes: { path: string; title: string; modified: string }[] = $state([]);
	let loading = $state(true);
	let error: string | null = $state(null);
	// Only the newest request may write, so a slow response for the previous
	// tag never lands under the current heading.
	let tagGeneration = 0;

	$effect(() => {
		const generation = ++tagGeneration;
		notes = [];
		loading = true;
		error = null;
		api
			.notesForTag(tag)
			.then((n) => {
				if (generation === tagGeneration) notes = n;
			})
			.catch((cause) => {
				if (generation === tagGeneration) error = (cause as Error).message;
			})
			.finally(() => {
				if (generation === tagGeneration) loading = false;
			});
	});
</script>

<div class="tag-page">
	<h1>#{tag}</h1>
	{#if loading}
		<p class="muted">Loading…</p>
	{:else if error}
		<p class="error">{error}</p>
	{:else if notes.length === 0}
		<p class="muted">No notes with this tag.</p>
	{:else}
		<ul>
			{#each notes as n (n.path)}
				<li>
					<a href={noteHref(n.path)}>{n.title || n.path}</a>
					<span class="mod">{n.modified?.slice(0, 10)}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.tag-page {
		max-width: 720px;
		width: 100%;
		margin: 0 auto;
		padding: 28px 32px;
		overflow-y: auto;
	}
	h1 {
		font-size: 20px;
		color: var(--accent);
	}
	ul {
		list-style: none;
		padding: 0;
	}
	li {
		padding: 8px 0;
		border-bottom: 1px solid var(--border);
	}
	a {
		color: var(--text);
		font-weight: 600;
		text-decoration: none;
	}
	a:hover {
		color: var(--accent);
	}
	.mod {
		color: var(--text-muted);
		font-size: 12px;
		margin-left: 8px;
	}
	.muted {
		color: var(--text-muted);
	}
	.error {
		color: var(--danger);
	}

	@media (max-width: 760px) {
		.tag-page {
			padding: 16px var(--m-gutter) 24px;
		}
		/* Keeps the accent — the tag itself is the page's identity here. */
		h1 {
			margin-top: 0;
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		/* Column flex so the two-line row still centres against the 52px floor. */
		li {
			display: flex;
			flex-direction: column;
			justify-content: center;
			min-height: var(--m-row-h);
			padding: 10px 2px;
		}
		a {
			font-size: var(--m-row-text);
		}
		.mod {
			display: block;
			margin: 2px 0 0;
			font-size: var(--m-meta);
		}
	}
</style>
