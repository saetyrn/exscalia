<script lang="ts">
	import { page } from '$app/state';
</script>

<div class="error-page">
	<p class="status">{page.status}</p>
	<p class="message">{page.error?.message}</p>
	<button type="button" class="primary" onclick={() => location.reload()}>Reload</button>
</div>

<style>
	.error-page {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: 24px 28px 60px;
		background: var(--bg);
		color: var(--text);
		text-align: center;
	}
	.status {
		margin: 0;
		font-size: 13px;
		font-weight: 600;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.message {
		margin: 0;
		max-width: 560px;
		font-size: 15px;
		overflow-wrap: anywhere;
	}
	@media (max-width: 760px) {
		.error-page {
			padding: 18px var(--m-gutter) 32px;
		}
		.message {
			font-size: var(--m-row-text);
		}
	}
</style>
