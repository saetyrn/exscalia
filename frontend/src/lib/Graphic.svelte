<script lang="ts">
	import { onMount } from 'svelte';
	import type { GraphicSpec } from './api';
	import { pieArcPath } from './piegeom';

	let { spec }: { spec: GraphicSpec } = $props();

	// Validated categorical palette (dataviz reference set) — fixed slot order.
	const LIGHT = ['#2a78d6', '#008300', '#e87ba4', '#eda100', '#1baf7a', '#eb6834', '#4a3aa7', '#e34948'];
	const DARK = ['#3987e5', '#008300', '#d55181', '#c98500', '#199e70', '#d95926', '#9085e9', '#e66767'];
	// Sequential blue ramp (light→dark) for heatmap magnitude.
	const SEQ = ['#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281', '#0d366b'];

	// Follow the APP's theme, not the OS. A light palette on a dark OS (or any
	// of beige/forest/black/custom) otherwise gave mismatched chart colors.
	let dark = $state(false);
	onMount(() => {
		const root = document.documentElement;
		const sync = () => {
			dark = getComputedStyle(root).colorScheme.includes('dark');
		};
		sync();
		// data-theme flips presets; the inline style carries custom themes.
		const observer = new MutationObserver(sync);
		observer.observe(root, { attributes: true, attributeFilter: ['data-theme', 'style'] });
		// 'system' still tracks the OS, so keep listening to that too.
		const mq = matchMedia('(prefers-color-scheme: dark)');
		mq.addEventListener('change', sync);
		return () => {
			observer.disconnect();
			mq.removeEventListener('change', sync);
		};
	});

	let palette = $derived(dark ? DARK : LIGHT);
	// On a dark surface low values recede into darkness; on light they stay pale.
	let ramp = $derived(dark ? [...SEQ].reverse() : SEQ);

	const W = 560;
	const PAD = { l: 44, r: 14, t: 10, b: 26 };

	let series = $derived(spec.series ?? []);
	let xLabels = $derived(spec.x_labels ?? []);

	function niceTicks(lo: number, hi: number, count = 4): number[] {
		if (lo === hi) hi = lo + 1;
		const span = hi - lo;
		const step0 = span / count;
		const mag = Math.pow(10, Math.floor(Math.log10(step0)));
		const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count + 0.5) ?? mag * 10;
		const start = Math.ceil(lo / step) * step;
		const out = [];
		for (let v = start; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
		return out;
	}

	function fmt(v: number): string {
		if (Math.abs(v) >= 1000) return `${+(v / 1000).toFixed(1)}k`;
		return `${+v.toFixed(2)}`;
	}

	// ---- bar / line scaffolding ----
	let allValues = $derived(series.flatMap((s) => s.values));
	let yMin = $derived(Math.min(0, ...allValues));
	let yMax = $derived(Math.max(0, ...allValues));
	let ticks = $derived(niceTicks(yMin, yMax));
	let plotH = $derived(spec.type === 'pie' ? 0 : 230);
	let H = $derived(spec.type === 'pie' ? 240 : plotH + PAD.t + PAD.b);
	let plotW = W - PAD.l - PAD.r;

	function sy(v: number): number {
		const lo = Math.min(yMin, ticks[0] ?? yMin);
		const hi = Math.max(yMax, ticks[ticks.length - 1] ?? yMax);
		return PAD.t + plotH - ((v - lo) / (hi - lo || 1)) * plotH;
	}

	let nCats = $derived(Math.max(...series.map((s) => s.values.length), xLabels.length, 1));
	let groupW = $derived(plotW / nCats);
	let barW = $derived(Math.max(4, Math.min(26, (groupW - 6) / Math.max(series.length, 1) - 2)));
	let showBarValues = $derived(spec.type === 'bar' && nCats * series.length <= 12);
	let xTickEvery = $derived(Math.ceil(nCats / 12));

	// Rounded 3px corners on the data end only, anchored square at the baseline.
	function barPath(x: number, v: number): string {
		const y0 = sy(0);
		const y1 = sy(v);
		const top = Math.min(y0, y1);
		const h = Math.abs(y0 - y1);
		const r = Math.min(3, h / 2, barW / 2);
		const up = v >= 0;
		if (h < 0.5) return '';
		if (up) {
			return `M${x},${y0} L${x},${top + r} Q${x},${top} ${x + r},${top} L${x + barW - r},${top} Q${x + barW},${top} ${x + barW},${top + r} L${x + barW},${y0} Z`;
		}
		const bot = top + h;
		return `M${x},${y0} L${x},${bot - r} Q${x},${bot} ${x + r},${bot} L${x + barW - r},${bot} Q${x + barW},${bot} ${x + barW},${bot - r} L${x + barW},${y0} Z`;
	}

	function lineX(i: number): number {
		return PAD.l + (nCats === 1 ? plotW / 2 : (i / (nCats - 1)) * plotW);
	}

	function linePoints(vals: number[]): string {
		return vals.map((v, i) => `${lineX(i)},${sy(v)}`).join(' ');
	}

	// ---- pie ----
	let pieTotal = $derived(series[0]?.values.reduce((a, b) => a + b, 0) ?? 0);
	let pieSlices = $derived.by(() => {
		const vals = series[0]?.values ?? [];
		const cx = 120,
			cy = 120,
			R = 92,
			r0 = 52;
		let angle = -Math.PI / 2;
		return vals.map((v, i) => {
			const frac = pieTotal > 0 ? v / pieTotal : 0;
			const a0 = angle;
			angle += frac * Math.PI * 2;
			return {
				d: pieArcPath(frac, a0, cx, cy, R, r0),
				value: v,
				pct: frac * 100,
				label: xLabels[i] ?? `${i + 1}`,
				color: palette[i % palette.length]
			};
		});
	});

	// ---- heatmap ----
	let cells = $derived(spec.cells ?? []);
	let hmMin = $derived(cells.length ? Math.min(...cells.flat()) : 0);
	let hmMax = $derived(cells.length ? Math.max(...cells.flat()) : 1);
	function cellColor(v: number): string {
		const t = hmMax === hmMin ? 0.5 : (v - hmMin) / (hmMax - hmMin);
		return ramp[Math.round(t * (ramp.length - 1))];
	}
	function cellInk(bg: string): string {
		const n = parseInt(bg.slice(1), 16);
		const lum = (0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
		return lum > 0.6 ? '#0b0b0b' : '#ffffff';
	}
	let hmLeft = $derived(spec.y_labels?.length ? 86 : 12);
	let hmCell = $derived.by(() => {
		const cols = cells[0]?.length ?? 1;
		return Math.min(34, Math.max(14, (W - hmLeft - 14) / cols));
	});
	let hmH = $derived(cells.length * (hmCell + 2) + 46);
	let showCellValues = $derived(cells.length <= 12 && (cells[0]?.length ?? 0) <= 12 && hmCell >= 22);
</script>

<figure class="viz" class:dark>
	{#if spec.title}<figcaption>{spec.title}</figcaption>{/if}

	{#if spec.type === 'bar' || spec.type === 'line'}
		<svg viewBox="0 0 {W} {H}" role="img" aria-label={spec.title || `${spec.type} chart`}>
			{#each ticks as t (t)}
				<line class="grid" x1={PAD.l} x2={W - PAD.r} y1={sy(t)} y2={sy(t)} />
				<text class="tick" x={PAD.l - 6} y={sy(t) + 3} text-anchor="end">{fmt(t)}</text>
			{/each}
			<line class="axis" x1={PAD.l} x2={W - PAD.r} y1={sy(0)} y2={sy(0)} />

			{#if spec.type === 'bar'}
				{#each series as s, si (si)}
					{#each s.values as v, i (i)}
						{@const x = PAD.l + i * groupW + (groupW - (barW + 2) * series.length + 2) / 2 + si * (barW + 2)}
						<path d={barPath(x, v)} style="fill: var(--c{(si % 8) + 1})">
							<title>{s.name || 'value'} — {xLabels[i] ?? i + 1}: {fmt(v)}</title>
						</path>
						{#if showBarValues}
							<text class="val" x={x + barW / 2} y={sy(Math.max(v, 0)) - 4} text-anchor="middle">{fmt(v)}</text>
						{/if}
					{/each}
				{/each}
			{:else}
				{#each series as s, si (si)}
					<polyline class="line" points={linePoints(s.values)} style="stroke: var(--c{(si % 8) + 1})" />
					{#each s.values as v, i (i)}
						<circle cx={lineX(i)} cy={sy(v)} r="3" style="fill: var(--c{(si % 8) + 1})">
							<title>{s.name || 'value'} — {xLabels[i] ?? i + 1}: {fmt(v)}</title>
						</circle>
					{/each}
					{#if series.length > 1 && series.length <= 4 && s.name}
						<text class="series-label" x={W - PAD.r} y={Math.max(sy(s.values[s.values.length - 1]) - 6, PAD.t + 8)} text-anchor="end">{s.name}</text>
					{/if}
				{/each}
			{/if}

			{#each xLabels.slice(0, nCats) as lbl, i (i)}
				{#if i % xTickEvery === 0}
					<text class="tick" x={spec.type === 'bar' ? PAD.l + i * groupW + groupW / 2 : lineX(i)} y={H - 8} text-anchor="middle">{lbl}</text>
				{/if}
			{/each}
		</svg>
	{:else if spec.type === 'pie'}
		<div class="pie-row">
			<svg viewBox="0 0 240 240" role="img" aria-label={spec.title || 'pie chart'}>
				{#each pieSlices as sl, i (i)}
					<path d={sl.d} fill={sl.color} class="slice">
						<title>{sl.label}: {fmt(sl.value)} ({sl.pct.toFixed(1)}%)</title>
					</path>
				{/each}
				<text class="pie-total" x="120" y="126" text-anchor="middle">{fmt(pieTotal)}</text>
			</svg>
			<ul class="legend pie-legend">
				{#each pieSlices as sl, i (i)}
					<li>
						<span class="chip" style="background: {sl.color}"></span>
						{sl.label}
						<span class="legend-val">{fmt(sl.value)} · {sl.pct.toFixed(0)}%</span>
					</li>
				{/each}
			</ul>
		</div>
	{:else if spec.type === 'heatmap'}
		<svg viewBox="0 0 {W} {hmH}" role="img" aria-label={spec.title || 'heatmap'}>
			{#each cells as row, ri (ri)}
				{#if spec.y_labels?.[ri]}
					<text class="tick" x={hmLeft - 6} y={ri * (hmCell + 2) + hmCell / 2 + 4} text-anchor="end">{spec.y_labels[ri]}</text>
				{/if}
				{#each row as v, ci (ci)}
					{@const bg = cellColor(v)}
					<rect x={hmLeft + ci * (hmCell + 2)} y={ri * (hmCell + 2)} width={hmCell} height={hmCell} rx="3" fill={bg}>
						<title>{spec.y_labels?.[ri] ?? ri + 1} / {spec.x_labels?.[ci] ?? ci + 1}: {fmt(v)}</title>
					</rect>
					{#if showCellValues}
						<text class="cell-val" x={hmLeft + ci * (hmCell + 2) + hmCell / 2} y={ri * (hmCell + 2) + hmCell / 2 + 3.5} text-anchor="middle" fill={cellInk(bg)}>{fmt(v)}</text>
					{/if}
				{/each}
			{/each}
			{#each spec.x_labels ?? [] as lbl, ci (ci)}
				{#if ci < (cells[0]?.length ?? 0) && ci % Math.ceil((cells[0]?.length ?? 1) / 14) === 0}
					<text class="tick" x={hmLeft + ci * (hmCell + 2) + hmCell / 2} y={cells.length * (hmCell + 2) + 14} text-anchor="middle">{lbl}</text>
				{/if}
			{/each}
			<text class="tick" x={hmLeft} y={cells.length * (hmCell + 2) + 34}>{fmt(hmMin)}</text>
			{#each ramp as c, i (i)}
				<rect x={hmLeft + 34 + i * 10} y={cells.length * (hmCell + 2) + 26} width="10" height="10" fill={c} />
			{/each}
			<text class="tick" x={hmLeft + 34 + ramp.length * 10 + 6} y={cells.length * (hmCell + 2) + 34}>{fmt(hmMax)}</text>
		</svg>
	{/if}

	{#if (spec.type === 'bar' || spec.type === 'line') && series.length > 1}
		<ul class="legend">
			{#each series as s, si (si)}
				<li>
					<span class="chip" style="background: var(--c{(si % 8) + 1})"></span>
					{s.name || `Series ${si + 1}`}
				</li>
			{/each}
		</ul>
	{/if}
</figure>

<style>
	.viz {
		/* Structural colors come from the active theme so charts sit correctly
		   on beige, forest, black, and custom palettes. Series colors stay the
		   designed categorical ramp, swapped light/dark by the `dark` class. */
		--viz-surface: var(--bg-panel);
		--viz-ink: var(--text);
		--viz-ink-2: var(--text-muted);
		--viz-muted: var(--text-muted);
		--viz-grid: var(--border);
		--viz-axis: var(--border);
		--c1: #2a78d6;
		--c2: #008300;
		--c3: #e87ba4;
		--c4: #eda100;
		--c5: #1baf7a;
		--c6: #eb6834;
		--c7: #4a3aa7;
		--c8: #e34948;
		margin: 8px 0 4px;
		padding: 12px 14px 10px;
		background: var(--viz-surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		max-width: 560px;
	}
	.viz.dark {
		--c1: #3987e5;
		--c2: #008300;
		--c3: #d55181;
		--c4: #c98500;
		--c5: #199e70;
		--c6: #d95926;
		--c7: #9085e9;
		--c8: #e66767;
	}
	figcaption {
		font-size: 13px;
		font-weight: 600;
		color: var(--viz-ink);
		margin-bottom: 8px;
	}
	svg {
		display: block;
		width: 100%;
		height: auto;
	}
	.grid {
		stroke: var(--viz-grid);
		stroke-width: 1;
	}
	.axis {
		stroke: var(--viz-axis);
		stroke-width: 1;
	}
	.tick {
		font-size: 10px;
		fill: var(--viz-muted);
		font-family: var(--font-ui);
	}
	.val,
	.cell-val {
		font-size: 10px;
		fill: var(--viz-ink-2);
		font-family: var(--font-ui);
	}
	.series-label {
		font-size: 11px;
		font-weight: 600;
		fill: var(--viz-ink-2);
	}
	.line {
		fill: none;
		stroke-width: 2;
		stroke-linejoin: round;
		stroke-linecap: round;
	}
	.slice {
		stroke: var(--viz-surface);
		stroke-width: 2;
	}
	.pie-total {
		font-size: 18px;
		font-weight: 700;
		fill: var(--viz-ink);
	}
	.pie-row {
		display: flex;
		align-items: center;
		gap: 16px;
	}
	.pie-row svg {
		width: 190px;
		flex-shrink: 0;
	}
	.legend {
		list-style: none;
		display: flex;
		flex-wrap: wrap;
		gap: 4px 14px;
		margin: 8px 0 0;
		padding: 0;
		font-size: 12px;
		color: var(--viz-ink-2);
	}
	.pie-legend {
		flex-direction: column;
		margin: 0;
	}
	.legend li {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.chip {
		width: 10px;
		height: 10px;
		border-radius: 3px;
		flex-shrink: 0;
	}
	.legend-val {
		color: var(--viz-muted);
	}

	@media (max-width: 480px) {
		.viz {
			width: 100%;
			padding: 10px 8px 8px;
		}
		.pie-row {
			align-items: stretch;
			flex-direction: column;
			gap: 10px;
		}
		.pie-row svg {
			width: min(220px, 100%);
			margin: 0 auto;
		}
		.pie-legend {
			flex-direction: row;
			flex-wrap: wrap;
		}
	}
</style>
