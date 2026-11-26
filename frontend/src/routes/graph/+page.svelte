<script lang="ts">
	import { api } from '$lib/api';
	import GraphView from '$lib/GraphView.svelte';
	import { notify } from '$lib/ui.svelte';

	let data = $state<Awaited<ReturnType<typeof api.graph>> | null>(null);
	let viewH = $state(480);

	$effect(() => {
		void api
			.graph()
			.then((g) => (data = g))
			.catch((e) => notify((e as Error).message, { tone: 'error' }));
		const size = () => (viewH = Math.max(320, window.innerHeight - 150));
		size();
		window.addEventListener('resize', size);
		return () => window.removeEventListener('resize', size);
	});
</script>

<svelte:head><title>Graph — EXSCALIA</title></svelte:head>

<div class="graph-page">
	<header>
		<h1>Graph</h1>
		<p>Every note, joined by its wikilinks. Click a dot to open it.</p>
	</header>
	{#if data}
		<GraphView nodes={data.nodes} edges={data.edges} height={viewH} />
	{:else}
		<p class="state">Reading the vault…</p>
	{/if}
</div>

<style>
	.graph-page {
		max-width: 1100px;
		margin: 0 auto;
		padding: 20px 24px 40px;
	}
	header {
		margin-bottom: 14px;
	}
	h1 {
		margin: 0;
		font-size: 20px;
	}
	p {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 13px;
	}
	.state {
		text-align: center;
		padding: 40px;
	}
	@media (max-width: 760px) {
		.graph-page {
			padding: 14px 10px 40px;
		}
	}
</style>
