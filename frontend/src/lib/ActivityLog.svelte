<script lang="ts">
	import { fade, fly } from '$lib/motion';
	import { api, type ActivityPeriod, type ActivitySummary } from '$lib/api';
	import { localToday } from '$lib/dates';
	import { confirmDialog, notify } from '$lib/ui.svelte';

	let { date } = $props<{ date: string }>();

	let summary: ActivitySummary | null = $state(null);
	let loading = $state(true);
	let saving = $state(false);
	let activity = $state('');
	let hours = $state('1');
	let loadGeneration = 0;

	$effect(() => {
		const day = date;
		activity = '';
		hours = '1';
		void load(day);
	});

	async function load(day = date) {
		const generation = ++loadGeneration;
		loading = true;
		try {
			const loaded = await api.activitySummary(day);
			if (generation === loadGeneration) summary = loaded;
		} catch (cause) {
			if (generation === loadGeneration) {
				notify((cause as Error).message, { tone: 'error' });
			}
		} finally {
			if (generation === loadGeneration) loading = false;
		}
	}

	async function addActivity(event: SubmitEvent) {
		event.preventDefault();
		const name = activity.trim();
		const amount = Number(hours);
		if (!name) {
			notify('Enter an activity name.', { tone: 'error' });
			return;
		}
		if (!Number.isFinite(amount) || amount <= 0 || amount > 24) {
			notify('Hours must be greater than 0 and no more than 24.', { tone: 'error' });
			return;
		}

		saving = true;
		try {
			await api.createActivity({ date, activity: name, hours: amount });
			activity = '';
			hours = '1';
			await load();
			notify('Activity logged.', { tone: 'success' });
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			saving = false;
		}
	}

	async function removeActivity(id: string, name: string) {
		const confirmed = await confirmDialog(`Remove this ${name} entry from the activity log?`, {
			title: 'Remove activity',
			confirmLabel: 'Remove',
			danger: true
		});
		if (!confirmed) return;
		try {
			await api.deleteActivity(id);
			await load();
			notify('Activity removed.');
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		}
	}

	function displayHours(value: number) {
		return value.toFixed(2).replace(/\.?0+$/, '');
	}

	function readableDate(value: string) {
		const [year, month, day] = value.split('-').map(Number);
		return new Date(year, month - 1, day).toLocaleDateString('en', {
			month: 'short',
			day: 'numeric'
		});
	}

	function periodRange(period: ActivityPeriod) {
		return period.start === period.end
			? readableDate(period.start)
			: `${readableDate(period.start)} – ${readableDate(period.end)}`;
	}

	const currentDate = localToday();

	function summaryPeriods(value: ActivitySummary | null) {
		if (!value) return [];
		return [
			{
				key: 'day',
				label: date === currentDate ? 'Today' : 'This day',
				value: value.day
			},
			{ key: 'week', label: 'This week', value: value.week },
			{ key: 'month', label: 'This month', value: value.month }
		];
	}

	let periods = $derived(summaryPeriods(summary));
</script>

<div class="activity-log">
	<section class="entry-card">
		<div class="section-heading">
			<div>
				<h2>Activity log</h2>
				<p>{date === currentDate ? 'What did you spend time on today?' : `Log time for ${readableDate(date)}.`}</p>
			</div>
		</div>

		<form onsubmit={addActivity}>
			<label>
				<span>Activity</span>
				<input
					bind:value={activity}
					list="activity-suggestions"
					placeholder="Reading, Blender, admin…"
					maxlength="80"
					autocomplete="off"
					disabled={saving}
				/>
			</label>
			<label class="hours-field">
				<span>Hours</span>
				<!-- step="any": the arrows walk whole hours (browsers' default
				     increment) while typed values like 1.3 stay valid — a fixed
				     step would flag anything off its grid as a step mismatch.
				     The real bounds live in addActivity and the API (0 < h ≤ 24). -->
				<input
					bind:value={hours}
					type="number"
					min="0"
					max="24"
					step="any"
					inputmode="decimal"
					disabled={saving}
				/>
			</label>
			<button class="primary add-button" type="submit" disabled={saving}>
				{saving ? 'Adding…' : 'Add'}
			</button>
			<datalist id="activity-suggestions">
				{#each summary?.suggestions ?? [] as suggestion (suggestion)}
					<option value={suggestion}></option>
				{/each}
			</datalist>
		</form>

		{#if summary?.entries.length}
			<ul class="day-entries">
				{#each summary.entries as entry (entry.id)}
					<li in:fly={{ y: 3, duration: 150 }}>
						<span class="entry-name">{entry.activity}</span>
						<strong>{displayHours(entry.hours)}h</strong>
						<button
							type="button"
							class="remove-entry"
							aria-label={`Remove ${entry.activity}, ${displayHours(entry.hours)} hours`}
							title="Remove entry"
							onclick={() => removeActivity(entry.id, entry.activity)}
						>
							×
						</button>
					</li>
				{/each}
			</ul>
		{:else if !loading}
			<p class="empty-day" in:fade={{ duration: 120 }}>No time logged for this day yet.</p>
		{/if}
	</section>

	{#if loading && !summary}
		<p class="loading">Loading activity…</p>
	{:else if summary}
		<div class="summary-grid">
			{#each periods as period (period.key)}
				<section class="summary-card" in:fly={{ y: 4, duration: 170 }}>
					<header>
						<div>
							<h3>{period.label}</h3>
							<span>{periodRange(period.value)}</span>
						</div>
						<strong>{displayHours(period.value.total_hours)}h</strong>
					</header>
					{#if period.value.activities.length}
						<table>
							<thead>
								<tr><th>Activity</th><th>Hours</th></tr>
							</thead>
							<tbody>
								{#each period.value.activities as item (item.activity)}
									<tr>
										<td>{item.activity}</td>
										<td>{displayHours(item.hours)}</td>
									</tr>
								{/each}
							</tbody>
						</table>
					{:else}
						<p class="no-results">Nothing logged yet.</p>
					{/if}
				</section>
			{/each}
		</div>
	{/if}
</div>

<style>
	.activity-log {
		display: flex;
		flex-direction: column;
		gap: 12px;
		min-height: 0;
		overflow-y: auto;
		padding: 14px;
	}
	.entry-card,
	.summary-card {
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg-panel);
	}
	.entry-card {
		padding: 12px;
	}
	.section-heading h2 {
		margin: 0;
		font-size: 15px;
	}
	.section-heading p {
		margin: 3px 0 0;
		color: var(--text-muted);
		font-size: 11px;
	}
	form {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 72px auto;
		align-items: end;
		gap: 6px;
		margin-top: 12px;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-width: 0;
	}
	label > span {
		color: var(--text-muted);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.4px;
		text-transform: uppercase;
	}
	input {
		width: 100%;
		min-width: 0;
		height: 34px;
	}
	.add-button {
		height: 34px;
		padding-inline: 11px;
	}
	.day-entries {
		display: flex;
		flex-direction: column;
		gap: 3px;
		margin: 10px 0 0;
		padding: 9px 0 0;
		border-top: 1px solid var(--border);
		list-style: none;
	}
	.day-entries li {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto 24px;
		align-items: center;
		gap: 8px;
		min-height: 28px;
		padding: 2px 3px 2px 7px;
		border-radius: 6px;
	}
	.day-entries li:hover {
		background: var(--bg-hover);
	}
	.entry-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.day-entries strong {
		color: var(--accent);
		font-variant-numeric: tabular-nums;
	}
	.remove-entry {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		padding: 0;
		border: 0;
		color: var(--text-muted);
		opacity: 0.55;
	}
	.remove-entry:hover {
		background: var(--bg-active);
		color: var(--danger);
		opacity: 1;
	}
	.empty-day,
	.loading {
		margin: 12px 0 0;
		color: var(--text-muted);
		font-size: 11px;
	}
	.loading {
		text-align: center;
	}
	.summary-grid {
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding-bottom: 12px;
	}
	.summary-card {
		overflow: hidden;
	}
	.summary-card > header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		padding: 10px 12px;
		border-bottom: 1px solid var(--border);
		background: color-mix(in srgb, var(--bg-panel) 82%, var(--accent-soft));
	}
	.summary-card h3 {
		margin: 0;
		font-size: 12px;
	}
	.summary-card header span {
		display: block;
		margin-top: 2px;
		color: var(--text-muted);
		font-size: 9px;
	}
	.summary-card header strong {
		color: var(--accent);
		font-size: 17px;
		font-variant-numeric: tabular-nums;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 11px;
	}
	th,
	td {
		padding: 6px 12px;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
		text-align: left;
	}
	th:last-child,
	td:last-child {
		width: 62px;
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	th {
		color: var(--text-muted);
		font-size: 9px;
		letter-spacing: 0.35px;
		text-transform: uppercase;
	}
	tbody tr:last-child td {
		border-bottom: 0;
	}
	.no-results {
		margin: 0;
		padding: 14px 12px;
		color: var(--text-muted);
		font-size: 11px;
	}
	@media (max-width: 420px) {
		.activity-log {
			padding: 12px 10px 20px;
		}
		form {
			grid-template-columns: minmax(0, 1fr) 76px;
		}
		.add-button {
			grid-column: 1 / -1;
		}
	}
</style>
