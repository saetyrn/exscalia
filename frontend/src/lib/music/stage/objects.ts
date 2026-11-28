// The stage's objects, built from code to real proportions (scene.ts shows
// them). Units: a CD's radius is 1 (60 mm), so 1 unit is 60 mm.
//
// CD (120 mm, 1.2 mm thick): a 15 mm hole; a clear hub out to 33 mm with a
// moulded stacking ring; a thin mirror band where the data starts; the
// printed label out to 59 mm; a clear rim; the data side a grating.
// Vinyl (12 inch, shown at the same size): grooves, smooth bands between
// tracks, dead wax, a paper label. Jewel case: 142 x 125 x 10 mm.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import { mergeGeometries, mergeVertices } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { FOLDER_GLYPH_PATHS } from './glyphs';
import {
	dataSide,
	debossMaps,
	labelPress,
	matrixRing,
	paperGrain,
	prismGlass,
	vinylMaps,
	vinylMaterial,
	withDiffraction
} from './materials';

export type StageObject =
	| { kind: 'blank' }
	| { kind: 'cd'; cover: string | null }
	| { kind: 'vinyl'; cover: string | null }
	| { kind: 'case'; cover: string | null }
	| { kind: 'folder'; colour: string; glyph: string | null; pinned: boolean }
	| { kind: 'sleeve'; cover: string | null }
	| { kind: 'glass'; shape: 'heart' | 'stack' | 'clock' };

export function objectKey(o: StageObject): string {
	switch (o.kind) {
		case 'folder':
			return `folder:${o.colour}:${o.glyph ?? ''}:${o.pinned}`;
		case 'glass':
			return `glass:${o.shape}`;
		case 'blank':
			return 'blank';
		default:
			return `${o.kind}:${o.cover ?? ''}`;
	}
}

export type TextureLoader = (url: string) => Promise<THREE.Texture | null>;

export interface Built {
	group: THREE.Group;
	/** Spins about the object's own axis while playing (discs only). */
	spin: THREE.Group | null;
	/** Turns all the way round when idle; everything else sways. */
	turns: boolean;
	/** How dark its floor shadow is (glass casts a lighter one). */
	shadow: number;
	/** Glass: throws a rainbow onto the floor. */
	prism: boolean;
	dispose: () => void;
}

const HOLE = 0.125;
const HUB = 0.33;
const MIRROR = 0.375;
const PRINT_EDGE = 0.985;
const THICK = 0.02;

// -- shared bits ---------------------------------------------------------------------

function clearPolycarbonate(): THREE.MeshPhysicalMaterial {
	return new THREE.MeshPhysicalMaterial({
		color: 0xffffff,
		metalness: 0,
		roughness: 0.06,
		transmission: 1,
		thickness: 0.02,
		ior: 1.58,
		specularIntensity: 1,
		clearcoat: 1,
		clearcoatRoughness: 0.03
	});
}

function ink(texture: THREE.Texture | null, fallback: number, roughness = 0.38, coat = 0.7): THREE.MeshPhysicalMaterial {
	return new THREE.MeshPhysicalMaterial({
		color: texture ? 0xffffff : fallback,
		map: texture,
		roughness,
		metalness: 0,
		clearcoat: coat,
		clearcoatRoughness: coat >= 1 ? 0.05 : 0.14,
		normalMap: paperGrain(),
		normalScale: new THREE.Vector2(0.15, 0.15)
	});
}

function paper(texture: THREE.Texture | null, fallback: number): THREE.MeshPhysicalMaterial {
	return new THREE.MeshPhysicalMaterial({
		color: texture ? 0xffffff : fallback,
		map: texture,
		roughness: 0.72,
		metalness: 0,
		normalMap: paperGrain(),
		normalScale: new THREE.Vector2(0.35, 0.35),
		sheen: 0.25,
		sheenRoughness: 0.8
	});
}

/** A rounded edge for a flat disc: a half-pipe lathed around the rim. */
function rimGeometry(radius: number, thickness: number): THREE.LatheGeometry {
	const points: THREE.Vector2[] = [];
	const r = thickness / 2;
	for (let i = 0; i <= 12; i++) {
		const a = -Math.PI / 2 + (i / 12) * Math.PI;
		points.push(new THREE.Vector2(radius - r + Math.cos(a) * r, Math.sin(a) * r));
	}
	const lathe = new THREE.LatheGeometry(points, 192);
	lathe.rotateX(Math.PI / 2);
	return lathe;
}

function ring(inner: number, outer: number, material: THREE.Material, z: number, back = false): THREE.Mesh {
	const mesh = new THREE.Mesh(new THREE.RingGeometry(inner, outer, 192, 1), material);
	mesh.position.z = z;
	if (back) mesh.rotation.y = Math.PI;
	return mesh;
}

// -- turned solids ------------------------------------------------------------------

type Corner = [r: number, z: number, round?: number];

/** A solid of revolution about the z axis from its half outline: corners
 * (radius, z) running from the back, out round the rim and in across the
 * front, which is the order that turns the faces outward. A corner given a
 * `round` is filleted by that much, as polished crystal is: a hard edge would
 * shade as a crease and lose the bright line a rounded one catches. */
function turned(corners: Corner[], segments: number): THREE.BufferGeometry {
	const points: THREE.Vector2[] = [];
	const a = new THREE.Vector2();
	const b = new THREE.Vector2();
	for (let i = 0; i < corners.length; i++) {
		const [r, z, round = 0] = corners[i];
		const here = new THREE.Vector2(r, z);
		if (!round || i === 0 || i === corners.length - 1) {
			points.push(here);
			continue;
		}
		a.set(corners[i - 1][0], corners[i - 1][1]).sub(here);
		b.set(corners[i + 1][0], corners[i + 1][1]).sub(here);
		const reach = Math.min(round, a.length() / 2, b.length() / 2);
		a.setLength(reach).add(here);
		b.setLength(reach).add(here);
		for (let k = 0; k <= 8; k++) {
			const t = k / 8;
			const u = 1 - t;
			points.push(new THREE.Vector2(u * u * a.x + 2 * u * t * r + t * t * b.x, u * u * a.y + 2 * u * t * z + t * t * b.y));
		}
	}
	const lathe = new THREE.LatheGeometry(points, segments);
	lathe.rotateX(Math.PI / 2);
	return lathe;
}

// -- discs ---------------------------------------------------------------------------

let cdBodyCache: THREE.BufferGeometry | null = null;

/** The disc itself: 1.2 mm of clear polycarbonate, moulded in one piece, with
 * a rounded rim, the stacking ring standing out of the data side round the
 * hub, and a fine moulded groove in the label side. Everything printed or
 * metallised lies on or just under its label face. Shared by every disc. */
function cdBodyGeometry(): THREE.BufferGeometry {
	if (cdBodyCache) return cdBodyCache;
	const f = THICK / 2;
	const corners: Corner[] = [
		[HOLE, f],
		[HOLE, -f, 0.004],
		[0.262, -f, 0.003],
		[0.268, -f - 0.008, 0.004],
		[0.288, -f - 0.008, 0.004],
		[0.294, -f, 0.003],
		[1 - f, -f]
	];
	for (let i = 1; i < 10; i++) {
		const a = -Math.PI / 2 + (i / 10) * Math.PI;
		corners.push([1 - f + Math.cos(a) * f, Math.sin(a) * f]);
	}
	corners.push([1 - f, f], [0.312, f, 0.002], [0.308, f - 0.004, 0.002], [0.298, f - 0.004, 0.002], [0.294, f, 0.002], [HOLE + 0.004, f, 0.004], [HOLE, f - 0.004]);
	cdBodyCache = turned(corners, 160);
	return cdBodyCache;
}

function cd(cover: THREE.Texture | null): THREE.Group {
	const disc = new THREE.Group();
	const front = THICK / 2;
	const body = new THREE.Mesh(cdBodyGeometry(), clearPolycarbonate());
	// Label side: the print under a hard gloss lacquer. A blank disc shows
	// its silver lacquer instead.
	const label = cover
		? ink(cover, 0xffffff, 0.32, 1)
		: withDiffraction(
				new THREE.MeshPhysicalMaterial({ color: 0xc9ccd4, metalness: 0.9, roughness: 0.35, clearcoat: 0.8 }),
				0.18
			);
	// Map the cover across the whole disc, as a printed CD is.
	const labelRing = ring(MIRROR, PRINT_EDGE, label, front + 0.0006);
	const mirror = ring(HUB, MIRROR, dataSide(), front + 0.0003);
	// The clamping area is lightly frosted by the mould.
	const hubFrost = ring(0.16, 0.26, new THREE.MeshPhysicalMaterial({ color: 0xffffff, roughness: 0.55, transparent: true, opacity: 0.2 }), front + 0.0005);
	// The metal layer lies under the label, so the data side is read (and
	// seen) through the whole thickness of the disc: the rainbow sits a
	// little way down inside clear plastic that has a shine of its own.
	const data = ring(HUB, 0.99, dataSide(), front - 0.0012, true);
	// The plant's etching in the mirror band, on both faces. Cut out, not
	// blended: inside the clear body a blended layer would be drawn after it
	// and lost.
	const etch = () => new THREE.MeshBasicMaterial({ map: matrixRing(), alphaTest: 0.5, color: 0x8d93a6 });
	const etchFront = ring(HUB + 0.004, MIRROR - 0.004, etch(), front + 0.0005);
	const etchBack = ring(HUB + 0.004, MIRROR - 0.004, etch(), front - 0.0018, true);
	disc.add(body, labelRing, mirror, hubFrost, data, etchFront, etchBack);
	return disc;
}

let recordRimCache: THREE.BufferGeometry | null = null;

/** A record's edge: the raised bead that keeps the grooves off the next
 * record in a stack, rounding over to the rim. */
function recordRimGeometry(half: number): THREE.BufferGeometry {
	recordRimCache ??= turned(
		[
			[0.972, -half],
			[0.984, -half - 0.007, 0.006],
			[0.996, -half - 0.004, 0.006],
			[1, 0, 0.012],
			[0.996, half + 0.004, 0.006],
			[0.984, half + 0.007, 0.006],
			[0.972, half]
		],
		192
	);
	return recordRimCache;
}

const SPINDLE = 0.024;

function record(cover: THREE.Texture | null): THREE.Group {
	const disc = new THREE.Group();
	// The maps are shared and cached (materials.ts); not this object's to dispose.
	const face = vinylMaterial(vinylMaps());
	const half = 0.018;
	disc.add(ring(SPINDLE, 0.974, face, half), ring(SPINDLE, 0.974, face, -half, true));
	const edge = new THREE.Mesh(
		recordRimGeometry(half),
		new THREE.MeshPhysicalMaterial({ color: 0x0b0b0d, roughness: 0.22, clearcoat: 1, clearcoatRoughness: 0.1 })
	);
	disc.add(edge);
	// The paper label, slightly raised, pressed into the vinyl with it; and
	// its back.
	const pressed = (art: THREE.Texture | null) => {
		const m = paper(art, 0xe9e1d2);
		m.normalMap = labelPress();
		m.normalScale = new THREE.Vector2(0.6, 0.6);
		// Label stock is coated: a soft sheen, not bare paper.
		m.roughness = 0.5;
		m.clearcoat = 0.35;
		m.clearcoatRoughness = 0.35;
		return m;
	};
	const labelFront = ring(SPINDLE, 0.33, pressed(cover), half + 0.002);
	const labelBack = ring(SPINDLE, 0.33, pressed(null), -half - 0.002, true);
	const spindle = new THREE.Mesh(
		new THREE.CylinderGeometry(SPINDLE, SPINDLE, half * 2 + 0.004, 48, 1, true),
		new THREE.MeshPhysicalMaterial({ color: 0x0a0a0c, roughness: 0.35, side: THREE.BackSide })
	);
	spindle.rotation.x = Math.PI / 2;
	disc.add(labelFront, labelBack, spindle);
	return disc;
}

// -- shapes ----------------------------------------------------------------------------

function roundedRect(w: number, h: number, r: number, x = -w / 2, y = -h / 2): THREE.Shape {
	const s = new THREE.Shape();
	s.moveTo(x + r, y);
	s.lineTo(x + w - r, y);
	s.quadraticCurveTo(x + w, y, x + w, y + r);
	s.lineTo(x + w, y + h - r);
	s.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
	s.lineTo(x + r, y + h);
	s.quadraticCurveTo(x, y + h, x, y + h - r);
	s.lineTo(x, y + r);
	s.quadraticCurveTo(x, y, x + r, y);
	return s;
}

function folderBackShape(w: number, h: number): THREE.Shape {
	const r = 0.16;
	const x = -w / 2;
	const y = -h / 2;
	const tabW = w * 0.38;
	const tabH = 0.2;
	const s = new THREE.Shape();
	s.moveTo(x + r, y);
	s.lineTo(x + w - r, y);
	s.quadraticCurveTo(x + w, y, x + w, y + r);
	s.lineTo(x + w, y + h - r);
	s.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
	s.lineTo(x + tabW + 0.16, y + h);
	s.quadraticCurveTo(x + tabW + 0.04, y + h, x + tabW - 0.02, y + h + tabH * 0.5);
	s.quadraticCurveTo(x + tabW - 0.08, y + h + tabH, x + tabW - 0.2, y + h + tabH);
	s.lineTo(x + r, y + h + tabH);
	s.quadraticCurveTo(x, y + h + tabH, x, y + h + tabH - r);
	s.lineTo(x, y + r);
	s.quadraticCurveTo(x, y, x + r, y);
	return s;
}

function starShape(outer: number, inner: number): THREE.Shape {
	const s = new THREE.Shape();
	for (let i = 0; i < 10; i++) {
		const r = i % 2 ? inner : outer;
		const a = Math.PI / 2 + (i * Math.PI) / 5;
		if (i === 0) s.moveTo(Math.cos(a) * r, Math.sin(a) * r);
		else s.lineTo(Math.cos(a) * r, Math.sin(a) * r);
	}
	s.closePath();
	return s;
}

/** A puffed glass heart: a fine sphere whose rim is pulled out to a heart
 * outline, so the surface is smooth and rounded everywhere, like blown
 * glass, not a flat outline with bevelled edges.
 *
 * The outline is the classic heart curve (x = 16 sin^3 t, y = 13 cos t -
 * 5 cos 2t - 2 cos 3t - cos 4t) for the lobes and the notch, the owner's
 * favourite part, and below the widest points two straight lines to a sharp
 * point, the way a drawn heart ends. The point is sharp at the rim and
 * rounds into the body, so it does not crease the front.
 *
 * Shaping it takes about a tenth of a second, so it is made once and kept;
 * disposing it only frees the GPU's copy, which the next heart uploads again. */
let heartCache: THREE.BufferGeometry | null = null;

function heartGeometry(): THREE.BufferGeometry {
	heartCache ??= shapeHeart();
	return heartCache;
}

function shapeHeart(): THREE.BufferGeometry {
	const curve = (t: number): [number, number] => [
		16 * Math.sin(t) ** 3,
		13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t)
	];
	const N = 4096;
	const points: [number, number][] = [];
	for (let i = 0; i < N; i++) points.push(curve((i / N) * Math.PI * 2));
	// The point, and where straight sides from it touch each lobe: the lobe
	// point that leans furthest out as seen from the tip.
	const tip: [number, number] = [0, -17.6];
	let right = 1;
	let left = N - 1;
	for (let i = 1; i < N / 2; i++) {
		const [x, y] = points[i];
		const [rx, ry] = points[right];
		if (y > tip[1] + 1 && x / (y - tip[1]) > rx / (ry - tip[1])) right = i;
	}
	for (let i = N / 2 + 1; i < N; i++) {
		const [x, y] = points[i];
		const [lx, ly] = points[left];
		if (y > tip[1] + 1 && x / (y - tip[1]) < lx / (ly - tip[1])) left = i;
	}
	const line = (from: [number, number], to: [number, number]) => {
		const out: [number, number][] = [];
		for (let k = 1; k < 400; k++) {
			const f = k / 400;
			out.push([from[0] + (to[0] - from[0]) * f, from[1] + (to[1] - from[1]) * f]);
		}
		return out;
	};
	const outlinePoints = [
		...points.slice(0, right + 1),
		...line(points[right], tip),
		tip,
		...line(tip, points[left]),
		...points.slice(left)
	];

	// The outline's distance from a centre point, by angle.
	const cx = 0;
	const cy = -2.5;
	const samples = outlinePoints
		.map(([x, y]) => ({ a: Math.atan2(y - cy, x - cx), r: Math.hypot(x - cx, y - cy) }))
		.sort((p, q) => p.a - q.a);
	const radiusAt = (angle: number) => {
		// Wrap into [-pi, pi): the smoothing window reaches past the seam.
		const a = ((((angle + Math.PI) % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2)) - Math.PI;
		let lo = 0;
		let hi = samples.length - 1;
		if (a <= samples[0].a || a >= samples[hi].a) return (samples[0].r + samples[hi].r) / 2;
		while (hi - lo > 1) {
			const mid = (lo + hi) >> 1;
			if (samples[mid].a < a) lo = mid;
			else hi = mid;
		}
		const k = (a - samples[lo].a) / (samples[hi].a - samples[lo].a || 1);
		return samples[lo].r + (samples[hi].r - samples[lo].r) * k;
	};
	// Two tables: the rim's outline (softened a few degrees at the notch, as
	// glass is, but sharp at the point) and a softer one for the body, so the
	// point's kink does not run a crease up the face.
	const STEPS = 1440;
	const rim = new Float32Array(STEPS);
	const body = new Float32Array(STEPS);
	const smooth = (a: number, width: number) => {
		if (width <= 0) return radiusAt(a);
		let sum = 0;
		for (let k = -12; k <= 12; k++) sum += radiusAt(a + (k / 12) * width);
		return sum / 25;
	};
	for (let i = 0; i < STEPS; i++) {
		const a = -Math.PI + (i / STEPS) * Math.PI * 2;
		const upper = THREE.MathUtils.clamp((Math.sin(a) + 0.2) / 0.4, 0, 1);
		rim[i] = smooth(a, 0.07 * upper);
		body[i] = smooth(a, 0.07 * upper + 0.3 * (1 - upper));
	}
	let mean = 0;
	for (const r of rim) mean += r;
	mean /= STEPS;
	const at = (table: Float32Array, a: number) =>
		table[Math.min(STEPS - 1, Math.floor(((a + Math.PI) / (Math.PI * 2)) * STEPS))];

	let geometry: THREE.BufferGeometry = new THREE.SphereGeometry(1, 288, 180);
	geometry.rotateX(Math.PI / 2); // poles to the front and back
	const position = geometry.attributes.position as THREE.BufferAttribute;
	const v = new THREE.Vector3();
	const scale = 1 / 17;
	const tipY = (tip[1] - cy) * scale;
	for (let i = 0; i < position.count; i++) {
		v.fromBufferAttribute(position, i);
		const rho = Math.min(1, Math.hypot(v.x, v.y));
		// The sphere's meridians are spread evenly, but the outline is not:
		// the notch and the point turn in a few degrees, the sides hardly at
		// all. Crowd the meridians toward the top and bottom (two and a half
		// times as dense there), or the notch is a handful of facets and
		// every highlight that crosses it combs.
		const even = Math.atan2(v.y, v.x);
		const a = even - 0.3 * Math.sin(2 * (even - Math.PI / 2));
		const down = Math.max(0, -Math.sin(a)) ** 3;
		// Round in the middle, the outline toward the rim. Toward the point
		// the outline reaches further in, so the point is a point, not a fin.
		const target = THREE.MathUtils.lerp(at(body, a), at(rim, a), rho ** 6);
		const r = (mean + (target - mean) * Math.pow(rho, 1.8 - 0.7 * down)) * scale;
		const x = Math.cos(a) * rho * r;
		const y = Math.sin(a) * rho * r;
		// Deeper through the lobes, tapering to the point.
		const depth =
			(0.4 + 0.14 * THREE.MathUtils.smoothstep(y, -0.6, 0.5)) *
			(0.3 + 0.7 * THREE.MathUtils.smoothstep(y, tipY, tipY + 0.6));
		position.setXYZ(i, x + cx * scale, y + cy * scale, v.z * depth);
	}
	// Weld the sphere's seam so the shading runs across it.
	geometry.deleteAttribute('normal');
	geometry.deleteAttribute('uv');
	geometry = mergeVertices(geometry, 1e-5);
	geometry.computeVertexNormals();
	geometry.center();
	return geometry;
}

// -- crystal --------------------------------------------------------------------------

/** A record cut from crystal: a thick polished disc with a rounded rim, a
 * raised centre where the label would be, a spindle hole, and grooves cut
 * into both faces. The grooves are real, turned with the disc: far fewer and
 * rounder than a pressed record's (which would be finer than a pixel), so
 * each is a little lens with a light of its own, in three bands with smooth
 * crystal between, as tracks are. */
function crystalRecordGeometry(): THREE.BufferGeometry {
	const half = 0.04;
	const boss = half + 0.016;
	const hole = 0.045;
	const pitch = 0.035;
	const smooth = THREE.MathUtils.smoothstep;
	const cut = (r: number) => {
		let amount = smooth(r, 0.4, 0.43) * (1 - smooth(r, 0.925, 0.95));
		for (const gap of [0.58, 0.77]) amount *= smooth(Math.abs(r - gap), 0.012, 0.032);
		return 0.009 * amount * (0.5 - 0.5 * Math.cos((r / pitch) * Math.PI * 2));
	};
	// A face from the centre's shoulder out to where the rim starts.
	const face: number[] = [];
	const steps = Math.round(((1 - half - 0.36) / pitch) * 7);
	for (let i = 0; i <= steps; i++) face.push(0.36 + ((1 - half - 0.36) * i) / steps);

	const corners: Corner[] = [
		[hole, boss],
		[hole, -boss, 0.012],
		[0.3, -boss, 0.016],
		[0.34, -half, 0.016]
	];
	for (const r of face) corners.push([r, -half + cut(r)]);
	// The rim: a full half-round.
	for (let i = 1; i < 16; i++) {
		const a = -Math.PI / 2 + (i / 16) * Math.PI;
		corners.push([1 - half + Math.cos(a) * half, Math.sin(a) * half]);
	}
	for (const r of face.reverse()) corners.push([r, half - cut(r)]);
	corners.push([0.34, half, 0.016], [0.3, boss, 0.016], [hole, boss, 0.012], [hole, boss - 0.012]);
	return turned(corners, 144);
}

/** The clock's body, one piece of crystal: a flat back, a rounded bezel
 * and a domed front, like a watch glass grown thick. The marks and the
 * hands sit inside it and are seen through the dome. */
function clockCrystalGeometry(): THREE.BufferGeometry {
	const corners: Corner[] = [
		[0, -0.13],
		[0.9, -0.13, 0.06],
		[1, -0.03, 0.06],
		[1, 0.08, 0.07],
		[0.93, 0.165, 0.06],
		[0.86, 0.165, 0.05],
		[0.8, 0.08, 0.03]
	];
	// The dome, rising from inside the bezel to the centre.
	for (let i = 1; i <= 28; i++) {
		const r = 0.8 * (1 - i / 28);
		corners.push([r, 0.08 + 0.1 * (1 - (r / 0.8) ** 2)]);
	}
	return turned(corners, 256);
}

/** Sixty minute marks, the twelve hours longer and heavier, as one mesh of
 * small polished bars. */
function clockMarksGeometry(): THREE.BufferGeometry {
	const hour = new RoundedBoxGeometry(0.036, 0.15, 0.022, 2, 0.009);
	const quarter = new RoundedBoxGeometry(0.05, 0.19, 0.026, 2, 0.011);
	const minute = new RoundedBoxGeometry(0.012, 0.06, 0.012, 1, 0.005);
	const bars: THREE.BufferGeometry[] = [];
	for (let i = 0; i < 60; i++) {
		const source = i % 15 === 0 ? quarter : i % 5 === 0 ? hour : minute;
		const length = i % 15 === 0 ? 0.19 : i % 5 === 0 ? 0.15 : 0.06;
		const bar = source.clone();
		// Outer ends on one circle.
		bar.translate(0, 0.73 - length / 2, 0);
		bar.rotateZ(-(i / 60) * Math.PI * 2);
		bars.push(bar);
	}
	const merged = mergeGeometries(bars);
	for (const g of [hour, quarter, minute, ...bars]) g.dispose();
	return merged;
}

/** A dauphine hand: a long faceted kite, widest near the pivot. */
function handGeometry(length: number, width: number): THREE.BufferGeometry {
	const s = new THREE.Shape();
	s.moveTo(0, -length * 0.16);
	s.lineTo(width / 2, 0);
	s.lineTo(0, length);
	s.lineTo(-width / 2, 0);
	s.closePath();
	return new THREE.ExtrudeGeometry(s, { depth: 0.004, bevelEnabled: true, bevelSize: 0.012, bevelThickness: 0.012, bevelSegments: 1 });
}

// -- the builder -----------------------------------------------------------------------

export async function build(o: StageObject, load: TextureLoader): Promise<Built> {
	const textures: THREE.Texture[] = [];
	const group = new THREE.Group();
	let spin: THREE.Group | null = null;
	let turns = false;
	let shadow = 1;
	let prism = false;
	const cover = async (url: string | null) => (url ? load(url) : null);

	switch (o.kind) {
		case 'blank':
		case 'cd': {
			spin = cd(o.kind === 'cd' ? await cover(o.cover) : null);
			group.add(spin);
			turns = true;
			break;
		}
		case 'vinyl': {
			spin = record(await cover(o.cover));
			group.add(spin);
			turns = true;
			break;
		}
		case 'case': {
			// A jewel case, 142 x 125 x 10 mm: a black tray with a ribbed
			// strip down the hinge side, and a clear lid over it that
			// carries the booklet behind four half-moon tabs.
			const art = await cover(o.cover);
			const w = 2.37;
			const h = 2.08;
			const black = () => new THREE.MeshPhysicalMaterial({ color: 0x0e0e12, roughness: 0.2, clearcoat: 1, clearcoatRoughness: 0.06 });
			const tray = new THREE.Mesh(new RoundedBoxGeometry(w - 0.02, h - 0.02, 0.105, 4, 0.016), black());
			tray.position.z = -0.0525;
			// The strip the lid hinges on, standing up beside the booklet,
			// with its column of moulded ribs.
			const stripW = 0.2;
			const strip = new THREE.Mesh(new RoundedBoxGeometry(stripW, h - 0.05, 0.05, 3, 0.012), black());
			strip.position.set(-w / 2 + stripW / 2 + 0.012, 0, 0.018);
			const rib = new RoundedBoxGeometry(0.105, 0.016, 0.012, 1, 0.005);
			const ribList: THREE.BufferGeometry[] = [];
			for (let i = 0; i < 44; i++) {
				const one = rib.clone();
				one.translate(0, -0.86 + i * 0.04, 0);
				ribList.push(one);
			}
			const ribs = new THREE.Mesh(mergeGeometries(ribList), black());
			for (const g of [rib, ...ribList]) g.dispose();
			ribs.position.set(strip.position.x, 0, 0.044);
			// The disc on its tray. With a booklet in the lid it is hidden,
			// as it is in a shop; without one it shows through the lid.
			const disc = cd(null);
			disc.scale.setScalar(0.94);
			disc.position.set(0.1, 0, 0.014);
			const hubClip = new THREE.Mesh(new THREE.CylinderGeometry(0.115, 0.125, 0.03, 48), black());
			hubClip.rotation.x = Math.PI / 2;
			hubClip.position.set(0.1, 0, 0.014);
			group.add(tray, strip, ribs, disc, hubClip);
			if (art) {
				const booklet = new THREE.Mesh(new RoundedBoxGeometry(2.0, 2.0, 0.01, 2, 0.003), paper(null, 0xf3f1ec));
				booklet.position.set(0.1, 0, 0.036);
				// Print the cover on the booklet's face only.
				const face = new THREE.Mesh(new THREE.PlaneGeometry(1.99, 1.99), ink(art, 0xd6d9e1, 0.42));
				face.position.set(0.1, 0, 0.0424);
				group.add(booklet, face);
			}
			// The lid: a plate with three walls, clear polystyrene.
			const clear = () => {
				const m = clearPolycarbonate();
				m.thickness = 0.03;
				m.roughness = 0;
				// One polished surface, not two: a second coat doubles the
				// room's reflection and greys the cover under it.
				m.clearcoat = 0;
				m.ior = 1.5;
				return m;
			};
			// The plate over the cover is drawn as its reflections alone, added
			// to what lies under it. Three's refraction would show the cover
			// through a resampled copy of the picture, a little soft; this
			// way the print stays as sharp as the booklet itself.
			const plate = new THREE.Mesh(
				new RoundedBoxGeometry(w, h, 0.022, 4, 0.009),
				new THREE.MeshPhysicalMaterial({
					color: 0x000000,
					roughness: 0,
					metalness: 0,
					ior: 1.5,
					envMapIntensity: 1.6,
					transparent: true,
					blending: THREE.AdditiveBlending,
					depthWrite: false
				})
			);
			plate.position.z = 0.058;
			group.add(plate);
			const wall = (bw: number, bh: number, x: number, y: number) => {
				const m = new THREE.Mesh(new RoundedBoxGeometry(bw, bh, 0.062, 2, 0.008), clear());
				m.position.set(x, y, 0.027);
				group.add(m);
			};
			wall(w, 0.022, 0, h / 2 - 0.011);
			wall(w, 0.022, 0, -h / 2 + 0.011);
			wall(0.022, h, w / 2 - 0.011, 0);
			// The tabs that hold the booklet, and the hinge arms.
			const tab = (x: number, y: number, turn: number) => {
				const m = new THREE.Mesh(new THREE.CylinderGeometry(0.1, 0.1, 0.008, 32, 1, false, 0, Math.PI), clear());
				m.rotation.set(Math.PI / 2, turn, 0);
				m.position.set(x, y, 0.044);
				group.add(m);
			};
			tab(-0.3, h / 2 - 0.022, -Math.PI / 2);
			tab(0.55, h / 2 - 0.022, -Math.PI / 2);
			tab(-0.3, -h / 2 + 0.022, Math.PI / 2);
			tab(0.55, -h / 2 + 0.022, Math.PI / 2);
			for (const y of [-h / 2 + 0.13, h / 2 - 0.13]) {
				const arm = new THREE.Mesh(new RoundedBoxGeometry(0.2, 0.2, 0.06, 2, 0.01), clear());
				arm.position.set(-w / 2 + 0.1, y, 0.026);
				group.add(arm);
			}
			break;
		}
		case 'folder': {
			const base = new THREE.Color(`#${o.colour.replace(/^#/, '')}`);
			const rich = base.clone().offsetHSL(0, 0.06, -0.05);
			const backColour = rich.clone().multiplyScalar(0.84);
			const soft = { depth: 0.05, bevelEnabled: true, bevelSize: 0.04, bevelThickness: 0.04, bevelSegments: 10, curveSegments: 48 };
			const card = (color: THREE.Color, maps?: { color: THREE.Texture; normal: THREE.Texture }) =>
				new THREE.MeshPhysicalMaterial({
					color: maps ? 0xffffff : color,
					map: maps?.color ?? null,
					normalMap: maps?.normal ?? paperGrain(),
					normalScale: new THREE.Vector2(maps ? 1 : 0.25, maps ? 1 : 0.25),
					roughness: 0.66,
					clearcoat: 0.25,
					clearcoatRoughness: 0.5,
					sheen: 0.35,
					sheenRoughness: 0.6,
					sheenColor: color
				});
			const panel = new THREE.Mesh(new THREE.ExtrudeGeometry(folderBackShape(2.3, 1.62), soft), card(backColour));
			panel.position.y = -0.05;
			// Loose sheets, none quite square to the next.
			const sheet = (dx: number, dy: number, z: number, tint: number, turn = 0) => {
				const m = new THREE.Mesh(new RoundedBoxGeometry(2.08, 1.52, 0.012, 2, 0.004), paper(null, tint));
				m.position.set(dx, dy, z);
				m.rotation.z = turn;
				return m;
			};
			const flapW = 2.3;
			const flapH = 1.42;
			// Cached in materials.ts, so not disposed with the folder.
			const maps = debossMaps(o.glyph ? (FOLDER_GLYPH_PATHS[o.glyph] ?? null) : null, rich, [flapW, flapH]);
			const flap = new THREE.Mesh(new THREE.ExtrudeGeometry(roundedRect(flapW, flapH, 0.16), soft), card(rich, maps));
			flap.position.set(0, -0.16, 0.18);
			flap.rotation.x = -0.05;
			group.add(panel, sheet(0.03, 0.05, 0.096, 0xf6f3ec, 0.012), sheet(0.01, 0.03, 0.112, 0xfbfaf6, -0.006), sheet(-0.03, 0.0, 0.128, 0xf4f1ea, 0.004), flap);
			if (o.pinned) {
				const star = new THREE.Mesh(
					new THREE.ExtrudeGeometry(starShape(0.13, 0.055), { depth: 0.02, bevelEnabled: true, bevelSize: 0.018, bevelThickness: 0.018, bevelSegments: 6 }),
					new THREE.MeshPhysicalMaterial({ color: 0xffffff, roughness: 0.25, clearcoat: 1, sheen: 0.4 })
				);
				star.position.set(0.88, 0.32, 0.29);
				star.rotation.x = -0.05;
				group.add(star);
			}
			break;
		}
		case 'sleeve': {
			const art = await cover(o.cover);
			const sticker = art ? null : await load('/music/sleeve-sticker.png');
			// A real jacket: two boards with the record between them, closed
			// along the spine, top and bottom, open at the right. The record
			// is drawn out far enough for its label to show at the mouth.
			const disc = record(art);
			disc.scale.setScalar(0.93);
			disc.position.set(0.74, 0, 0);
			const card = () => paper(null, 0xe7e1d5);
			const gap = 0.07;
			const board = (z: number) => {
				const m = new THREE.Mesh(new RoundedBoxGeometry(2, 2, 0.012, 2, 0.004), card());
				m.position.z = z;
				return m;
			};
			const edge = (bw: number, bh: number, x: number, y: number) => {
				const m = new THREE.Mesh(new RoundedBoxGeometry(bw, bh, gap + 0.012, 2, 0.006), card());
				m.position.set(x, y, 0);
				return m;
			};
			const faceZ = gap / 2 + 0.0066;
			const face = new THREE.Mesh(new THREE.PlaneGeometry(1.994, 1.994), art ? ink(art, 0xffffff, 0.5) : paper(null, 0xebe6dc));
			face.position.z = faceZ;
			group.add(disc, board(gap / 2), board(-gap / 2), edge(0.016, 2, -0.992, 0), edge(2, 0.016, 0, 0.992), edge(2, 0.016, 0, -0.992), face);
			if (sticker) {
				const decal = new THREE.Mesh(
					new THREE.PlaneGeometry(1.5, 0.84),
					new THREE.MeshPhysicalMaterial({ map: sticker, transparent: true, roughness: 0.4, clearcoat: 0.6, depthWrite: false })
				);
				decal.position.z = faceZ + 0.001;
				group.add(decal);
			}
			break;
		}
		case 'glass': {
			shadow = 0.45;
			prism = true;
			if (o.shape === 'heart') {
				group.add(new THREE.Mesh(heartGeometry(), prismGlass(0xfdf6fb, 1.6)));
			} else if (o.shape === 'stack') {
				const geometry = crystalRecordGeometry();
				for (let i = 0; i < 3; i++) {
					const disc = new THREE.Mesh(geometry, prismGlass(0xf4f6ff, 0.22, { strength: 0.85, depth: 0.12, pane: true }));
					disc.scale.setScalar(0.92);
					disc.rotation.x = -0.55;
					disc.position.set((i - 1) * 0.18, (i - 1) * -0.26, (i - 1) * 0.2);
					group.add(disc);
				}
			} else {
				const crystal = prismGlass(0xf8f9ff, 0.4, { depth: 0.5, face: 0.2 });
				// Three splits what shows through into colours too, which
				// smears the marks; the prism has that covered.
				crystal.dispersion = 1;
				const body = new THREE.Mesh(clockCrystalGeometry(), crystal);
				// Blued steel, as a watch's hands are: dark against a light
				// page, and it still catches the lamps on a dark one.
				const steel = new THREE.MeshPhysicalMaterial({ color: 0x2b3f9a, metalness: 1, roughness: 0.22, envMapIntensity: 1.6 });
				const marks = new THREE.Mesh(clockMarksGeometry(), steel);
				marks.position.z = -0.03;
				const hand = (length: number, width: number, angle: number, z: number) => {
					const m = new THREE.Mesh(handGeometry(length, width), steel);
					m.rotation.z = angle;
					m.position.z = z;
					return m;
				};
				const pin = new THREE.Mesh(new THREE.SphereGeometry(0.05, 32, 16), steel);
				pin.scale.z = 0.6;
				pin.position.z = 0.05;
				group.add(body, marks, hand(0.42, 0.085, -0.9, -0.01), hand(0.66, 0.066, 0.55, 0.022), pin);
			}
			break;
		}
	}

	const owned: { dispose(): void }[] = [...textures];
	group.traverse((child) => {
		const mesh = child as THREE.Mesh;
		if (mesh.geometry) owned.push(mesh.geometry);
		const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
		for (const m of Array.isArray(mat) ? mat : mat ? [mat] : []) owned.push(m);
		// Solid parts shadow each other under the key light (scene.ts).
		// Clear and see-through ones do neither: a shadow map knows nothing
		// of glass, and would black out whatever lies under a lid.
		const one = (Array.isArray(mat) ? mat[0] : mat) as THREE.MeshPhysicalMaterial | undefined;
		if (mesh.isMesh && one && !prism) {
			const clear = (one.transmission ?? 0) > 0 || one.transparent;
			mesh.castShadow = !clear;
			mesh.receiveShadow = !clear;
		}
	});
	return {
		group,
		spin,
		turns,
		shadow,
		prism,
		dispose: () => {
			for (const item of owned) item.dispose();
		}
	};
}
