<script lang="ts">
	// Renders an image cropped to fill its container (like object-fit: cover)
	// but with an explicit focal point (x/y, 0–100 = fraction of the image) and
	// zoom. Geometry is computed in pixels from the natural + container sizes,
	// so `editable` dragging moves the image 1:1 with the pointer.
	let {
		src,
		zoom = 1,
		x = 50,
		y = 50,
		editable = false,
		onreposition
	}: {
		src: string;
		zoom?: number;
		x?: number;
		y?: number;
		editable?: boolean;
		onreposition?: (x: number, y: number) => void;
	} = $props();

	let W = $state(0);
	let H = $state(0);
	let iw = $state(0);
	let ih = $state(0);
	let dragging = $state(false);

	// Displayed size (cover × zoom) and the translate that puts focal point
	// (x%, y%) of the image at the container's centre, clamped to keep it filled.
	let geo = $derived.by(() => {
		if (!W || !H || !iw || !ih) return null;
		const cover = Math.max(W / iw, H / ih);
		const fw = iw * cover * zoom;
		const fh = ih * cover * zoom;
		const tx = Math.min(0, Math.max(W - fw, W / 2 - (x / 100) * fw));
		const ty = Math.min(0, Math.max(H - fh, H / 2 - (y / 100) * fh));
		return { fw, fh, tx, ty };
	});

	function onLoad(event: Event) {
		const img = event.currentTarget as HTMLImageElement;
		iw = img.naturalWidth;
		ih = img.naturalHeight;
	}

	function startDrag(event: PointerEvent) {
		if (!editable || !geo) return;
		const start = { px: event.clientX, py: event.clientY, tx: geo.tx, ty: geo.ty };
		dragging = true;
		const move = (e: PointerEvent) => {
			if (!geo) return;
			const { fw, fh } = geo;
			// 1:1 — apply the raw pixel delta to the start translate, then clamp.
			const tx = Math.min(0, Math.max(W - fw, start.tx + (e.clientX - start.px)));
			const ty = Math.min(0, Math.max(H - fh, start.ty + (e.clientY - start.py)));
			const nx = fw ? ((W / 2 - tx) / fw) * 100 : 50;
			const ny = fh ? ((H / 2 - ty) / fh) * 100 : 50;
			onreposition?.(
				Math.max(0, Math.min(100, nx)),
				Math.max(0, Math.min(100, ny))
			);
		};
		const up = () => {
			dragging = false;
			window.removeEventListener('pointermove', move);
			window.removeEventListener('pointerup', up);
		};
		window.addEventListener('pointermove', move);
		window.addEventListener('pointerup', up);
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="framed"
	class:editable
	class:dragging
	bind:clientWidth={W}
	bind:clientHeight={H}
	onpointerdown={startDrag}
>
	{#if geo}
		<img
			{src}
			alt=""
			draggable="false"
			onload={onLoad}
			style="width:{geo.fw}px;height:{geo.fh}px;transform:translate({geo.tx}px,{geo.ty}px);"
		/>
	{:else}
		<img
			{src}
			alt=""
			draggable="false"
			onload={onLoad}
			style="width:100%;height:100%;object-fit:cover;object-position:{x}% {y}%;"
		/>
	{/if}
</div>

<style>
	.framed {
		position: relative;
		width: 100%;
		height: 100%;
		overflow: hidden;
	}
	.framed.editable {
		cursor: grab;
		touch-action: none;
		user-select: none;
	}
	.framed.dragging {
		cursor: grabbing;
	}
	.framed img {
		position: absolute;
		left: 0;
		top: 0;
		transform-origin: top left;
		pointer-events: none;
		display: block;
	}
</style>
