<script lang="ts">
	// The vault as dots and lines. A small hand-rolled force layout — the
	// vault is personal-scale, so an O(n²) simulation run to rest at mount
	// beats shipping a graph library. Monochrome per the design language:
	// muted dots, border-colored lines, accent only for the centered note
	// and the node under the cursor.
	import { goto } from '$app/navigation';
	import { encodePath } from './api';

	interface GraphNode {
		path: string;
		title: string;
	}
	interface GraphEdge {
		source: string;
		target: string;
	}

	let {
		nodes,
		edges,
		center = null,
		height = 480
	}: {
		nodes: GraphNode[];
		edges: GraphEdge[];
		center?: string | null;
		height?: number;
	} = $props();

	let canvas: HTMLCanvasElement | undefined = $state();
	let wrap: HTMLDivElement | undefined = $state();

	interface Body {
		path: string;
		label: string;
		x: number;
		y: number;
		vx: number;
		vy: number;
		degree: number;
	}

	// Plain arrays on purpose: the O(n²) simulation would crawl through a
	// deep $state proxy. The template only needs to know emptiness.
	let bodies: Body[] = [];
	let springs: [number, number][] = [];
	let hasBodies = $state(false);
	let view = { x: 0, y: 0, scale: 1 };
	let hovered: Body | null = null;

	/** center set → keep only notes within two hops of it. */
	function visibleSet(): { nodes: GraphNode[]; edges: GraphEdge[] } {
		if (!center) return { nodes, edges };
		const keep = new Set([center]);
		for (let hop = 0; hop < 2; hop++) {
			for (const e of edges) {
				if (keep.has(e.source)) keep.add(e.target);
				else if (keep.has(e.target)) keep.add(e.source);
			}
		}
		return {
			nodes: nodes.filter((n) => keep.has(n.path)),
			edges: edges.filter((e) => keep.has(e.source) && keep.has(e.target))
		};
	}

	function build() {
		const { nodes: vn, edges: ve } = visibleSet();
		const index = new Map(vn.map((n, i) => [n.path, i]));
		const degree = new Map<string, number>();
		for (const e of ve) {
			degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
			degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
		}
		bodies = vn.map((n, i) => {
			const angle = (i / Math.max(1, vn.length)) * Math.PI * 2;
			const r = n.path === center ? 0 : 120 + (i % 5) * 30;
			return {
				path: n.path,
				label: n.title || n.path.split('/').pop()!.replace(/\.md$/, ''),
				x: Math.cos(angle) * r,
				y: Math.sin(angle) * r,
				vx: 0,
				vy: 0,
				degree: degree.get(n.path) ?? 0
			};
		});
		springs = ve
			.map(
				(e) => [index.get(e.source), index.get(e.target)] as [number, number]
			)
			.filter(([a, b]) => a !== undefined && b !== undefined);
		hasBodies = bodies.length > 0;
		settle();
	}

	/** Run the simulation to rest, synchronously — a calm static layout. */
	function settle() {
		const REPULSE = 2600;
		const SPRING = 0.015;
		const REST = 90;
		const CENTERING = 0.005;
		for (let tick = 0; tick < 320; tick++) {
			for (let i = 0; i < bodies.length; i++) {
				const a = bodies[i];
				for (let j = i + 1; j < bodies.length; j++) {
					const b = bodies[j];
					let dx = a.x - b.x;
					let dy = a.y - b.y;
					let d2 = dx * dx + dy * dy;
					if (d2 < 1) {
						dx = (Math.random() - 0.5) * 2;
						dy = (Math.random() - 0.5) * 2;
						d2 = dx * dx + dy * dy;
					}
					const f = REPULSE / d2;
					const d = Math.sqrt(d2);
					a.vx += (dx / d) * f;
					a.vy += (dy / d) * f;
					b.vx -= (dx / d) * f;
					b.vy -= (dy / d) * f;
				}
			}
			for (const [ia, ib] of springs) {
				const a = bodies[ia];
				const b = bodies[ib];
				const dx = b.x - a.x;
				const dy = b.y - a.y;
				const d = Math.max(1, Math.hypot(dx, dy));
				const f = (d - REST) * SPRING;
				a.vx += (dx / d) * f;
				a.vy += (dy / d) * f;
				b.vx -= (dx / d) * f;
				b.vy -= (dy / d) * f;
			}
			for (const b of bodies) {
				// A centered note stays pinned in the middle.
				if (b.path === center) {
					b.x = 0;
					b.y = 0;
					b.vx = 0;
					b.vy = 0;
					continue;
				}
				b.vx -= b.x * CENTERING;
				b.vy -= b.y * CENTERING;
				b.x += Math.max(-14, Math.min(14, b.vx * 0.08));
				b.y += Math.max(-14, Math.min(14, b.vy * 0.08));
				b.vx *= 0.55;
				b.vy *= 0.55;
			}
		}
	}

	function colors() {
		const style = getComputedStyle(document.documentElement);
		return {
			line: style.getPropertyValue('--border').trim() || '#444',
			dot: style.getPropertyValue('--text-muted').trim() || '#888',
			text: style.getPropertyValue('--text').trim() || '#ddd',
			accent: style.getPropertyValue('--accent').trim() || '#6c5ce7'
		};
	}

	function draw() {
		if (!canvas) return;
		const ctx = canvas.getContext('2d');
		if (!ctx) return;
		const dpr = window.devicePixelRatio || 1;
		const w = canvas.clientWidth;
		const h = canvas.clientHeight;
		if (canvas.width !== w * dpr) canvas.width = w * dpr;
		if (canvas.height !== h * dpr) canvas.height = h * dpr;
		const c = colors();
		ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
		ctx.clearRect(0, 0, w, h);
		ctx.translate(w / 2 + view.x, h / 2 + view.y);
		ctx.scale(view.scale, view.scale);

		ctx.lineWidth = 1 / view.scale;
		for (const [ia, ib] of springs) {
			const active = hovered && (bodies[ia] === hovered || bodies[ib] === hovered);
			ctx.strokeStyle = active ? c.dot : c.line;
			ctx.beginPath();
			ctx.moveTo(bodies[ia].x, bodies[ia].y);
			ctx.lineTo(bodies[ib].x, bodies[ib].y);
			ctx.stroke();
		}
		for (const b of bodies) {
			const r = Math.min(9, 3.5 + b.degree * 0.8);
			const special = b.path === center || b === hovered;
			ctx.fillStyle = special ? c.accent : c.dot;
			ctx.beginPath();
			ctx.arc(b.x, b.y, r, 0, Math.PI * 2);
			ctx.fill();
			if (special || b.degree >= 3 || bodies.length <= 14) {
				ctx.fillStyle = special ? c.text : c.dot;
				ctx.font = `${11 / view.scale}px ${getComputedStyle(document.body).fontFamily}`;
				ctx.textAlign = 'center';
				ctx.fillText(b.label, b.x, b.y + r + 12 / view.scale);
			}
		}
	}

	function toWorld(event: PointerEvent | WheelEvent): [number, number] {
		const box = canvas!.getBoundingClientRect();
		const x = (event.clientX - box.left - box.width / 2 - view.x) / view.scale;
		const y = (event.clientY - box.top - box.height / 2 - view.y) / view.scale;
		return [x, y];
	}

	function nodeAt(x: number, y: number): Body | null {
		let best: Body | null = null;
		let bestD = 14 / view.scale;
		for (const b of bodies) {
			const d = Math.hypot(b.x - x, b.y - y);
			if (d < bestD) {
				bestD = d;
				best = b;
			}
		}
		return best;
	}

	let panning = false;
	let moved = false;
	let last = [0, 0];

	function onPointerDown(event: PointerEvent) {
		panning = true;
		moved = false;
		last = [event.clientX, event.clientY];
		canvas?.setPointerCapture(event.pointerId);
	}

	function onPointerMove(event: PointerEvent) {
		if (panning) {
			const dx = event.clientX - last[0];
			const dy = event.clientY - last[1];
			if (Math.hypot(dx, dy) > 3) moved = true;
			view.x += dx;
			view.y += dy;
			last = [event.clientX, event.clientY];
			draw();
			return;
		}
		const [x, y] = toWorld(event);
		const hit = nodeAt(x, y);
		if (hit !== hovered) {
			hovered = hit;
			if (canvas) canvas.style.cursor = hit ? 'pointer' : 'grab';
			draw();
		}
	}

	function onPointerUp(event: PointerEvent) {
		panning = false;
		if (moved) return;
		const [x, y] = toWorld(event);
		const hit = nodeAt(x, y);
		if (hit) void goto(`/note/${encodePath(hit.path)}`);
	}

	function onWheel(event: WheelEvent) {
		event.preventDefault();
		const factor = event.deltaY < 0 ? 1.12 : 0.9;
		view.scale = Math.max(0.25, Math.min(4, view.scale * factor));
		draw();
	}

	$effect(() => {
		void nodes;
		void edges;
		void center;
		build();
		view = { x: 0, y: 0, scale: 1 };
		draw();
	});

	// The data effect above can run before {#if} has mounted the canvas —
	// its draw() hits undefined and the backing store stays the 300×150
	// default, stretched blurry over the panel. Redraw when the canvas
	// actually exists, and again whenever the panel resizes.
	$effect(() => {
		if (!canvas || !wrap) return;
		draw();
		const observer = new ResizeObserver(() => draw());
		observer.observe(wrap);
		return () => observer.disconnect();
	});
</script>

<div class="graph" bind:this={wrap} style:height="{height}px">
	{#if !hasBodies}
		<p class="empty">No linked notes yet — wikilinks draw this map.</p>
	{:else}
		<canvas
			bind:this={canvas}
			onpointerdown={onPointerDown}
			onpointermove={onPointerMove}
			onpointerup={onPointerUp}
			onwheel={onWheel}
		></canvas>
	{/if}
</div>

<style>
	.graph {
		position: relative;
		width: 100%;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--bg);
		overflow: hidden;
	}
	canvas {
		width: 100%;
		height: 100%;
		display: block;
		cursor: grab;
		touch-action: none;
	}
	.empty {
		position: absolute;
		inset: 0;
		display: grid;
		place-items: center;
		margin: 0;
		color: var(--text-muted);
		font-size: 13px;
	}
</style>
