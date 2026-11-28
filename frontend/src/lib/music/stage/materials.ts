// Materials and generated textures for the stage (scene.ts). Nothing here
// loads a file: every surface detail is drawn into a canvas at start.
//
// - studioEnvironment: a photo studio (softboxes, strip lights, a grey room)
//   baked into an environment map, so glossy things catch long highlights.
// - diffraction: a CD's data side is a grating (1.6 micrometre track pitch);
//   the rainbow it throws depends on the angle to the tracks, not on a thin
//   film, so it is computed per light from the track tangent (after GPU
//   Gems 1, chapter 8).
// - vinyl: concentric grooves as a normal map, and anisotropy along them, so
//   a light becomes the radial streak a record shows.
// - grain / deboss: cardstock and paper surfaces, a glyph pressed into one.
import * as THREE from 'three';

const TAU = Math.PI * 2;

// -- environment ---------------------------------------------------------------------

export function studioEnvironment(renderer: THREE.WebGLRenderer): THREE.Texture {
	const room = new THREE.Scene();
	const walls = new THREE.Mesh(
		new THREE.SphereGeometry(30, 32, 16),
		new THREE.ShaderMaterial({
			side: THREE.BackSide,
			uniforms: {},
			vertexShader: 'varying vec3 vP; void main(){ vP = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
			fragmentShader:
				'varying vec3 vP; void main(){ float h = normalize(vP).y; vec3 top = vec3(0.62,0.64,0.70); vec3 floor = vec3(0.16,0.16,0.19); gl_FragColor = vec4(mix(floor, top, smoothstep(-0.6, 0.7, h)), 1.0); }'
		})
	);
	room.add(walls);
	const panel = (w: number, h: number, power: number, at: [number, number, number], tint = 0xffffff) => {
		const mesh = new THREE.Mesh(
			new THREE.PlaneGeometry(w, h),
			new THREE.MeshBasicMaterial({ color: new THREE.Color(tint).multiplyScalar(power), side: THREE.DoubleSide })
		);
		mesh.position.set(...at);
		mesh.lookAt(0, 0, 0);
		room.add(mesh);
	};
	panel(14, 9, 7, [-7, 9, 9]); // the key softbox, high front left
	panel(3, 18, 5, [12, 1, 3]); // a tall strip, right
	panel(3, 18, 3.2, [-12, 1, -4], 0xdfe6ff); // a cooler strip, back left
	panel(10, 4, 1.6, [0, -6, 10], 0xfff4ea); // warm bounce from below front
	panel(6, 6, 2.5, [3, 12, -6]); // top back
	const pmrem = new THREE.PMREMGenerator(renderer);
	const texture = pmrem.fromScene(room, 0.03).texture;
	pmrem.dispose();
	room.traverse((o) => {
		const mesh = o as THREE.Mesh;
		mesh.geometry?.dispose();
		(mesh.material as THREE.Material | undefined)?.dispose();
	});
	return texture;
}

/** The studio's light directions, world space: the diffraction shader's. */
export const LIGHT_DIRECTIONS = [
	new THREE.Vector3(-7, 9, 9).normalize(),
	new THREE.Vector3(12, 1, 3).normalize(),
	new THREE.Vector3(-12, 1, -4).normalize(),
	new THREE.Vector3(3, 12, -6).normalize()
];
const LIGHT_WEIGHTS = [1, 0.75, 0.5, 0.45];

// -- the CD's data side --------------------------------------------------------------

/** A material whose surface is a circular grating about the mesh's local
 * origin (the disc's centre): it adds the spectral orders a CD throws. */
export function withDiffraction<T extends THREE.MeshPhysicalMaterial>(material: T, strength = 0.55): T {
	material.onBeforeCompile = (shader) => {
		shader.uniforms.uLightDirs = { value: LIGHT_DIRECTIONS };
		shader.uniforms.uLightWeights = { value: LIGHT_WEIGHTS };
		shader.uniforms.uDiffraction = { value: strength };
		shader.vertexShader = shader.vertexShader
			.replace(
				'#include <common>',
				'#include <common>\nvarying vec3 vDiffPos;\nvarying vec3 vDiffTangent;\nvarying vec3 vDiffNormal;'
			)
			.replace(
				'#include <project_vertex>',
				`#include <project_vertex>
				vDiffPos = (modelMatrix * vec4(transformed, 1.0)).xyz;
				vDiffTangent = normalize(mat3(modelMatrix) * vec3(-transformed.y, transformed.x, 0.0));
				vDiffNormal = normalize(mat3(modelMatrix) * objectNormal);`
			);
		shader.fragmentShader = shader.fragmentShader
			.replace(
				'#include <common>',
				`#include <common>
				varying vec3 vDiffPos;
				varying vec3 vDiffTangent;
				varying vec3 vDiffNormal;
				uniform vec3 uLightDirs[4];
				uniform float uLightWeights[4];
				uniform float uDiffraction;
				vec3 diffBump(vec3 x) { vec3 y = 1.0 - x * x; return max(y, vec3(0.0)); }
				// Wavelength (0..1 across 400..700 nm) to a soft RGB.
				vec3 diffSpectrum(float w) {
					if (w < 0.0 || w > 1.0) return vec3(0.0);
					return diffBump(vec3(3.0 * (w - 0.75), 3.0 * (w - 0.5), 3.0 * (w - 0.25)));
				}
				vec3 diffGrating(vec3 L, vec3 V, vec3 T, vec3 N) {
					float u = abs(dot(T, L + V));
					vec3 c = vec3(0.0);
					// Track pitch 1.6 um; orders 1 to 4.
					for (int n = 1; n <= 4; n++) {
						float lambda = 1.6 * u / float(n);
						c += diffSpectrum((lambda - 0.4) / 0.3) / float(n);
					}
					vec3 H = normalize(L + V);
					float facing = max(dot(N, H), 0.0);
					return c * (0.25 + 0.75 * pow(facing, 3.0)) * max(dot(N, L), 0.0);
				}`
			)
			.replace(
				'#include <opaque_fragment>',
				`{
					vec3 Vd = normalize(cameraPosition - vDiffPos);
					vec3 Td = normalize(vDiffTangent);
					vec3 Nd = normalize(vDiffNormal);
					if (!gl_FrontFacing) Nd = -Nd;
					vec3 rainbow = vec3(0.0);
					for (int i = 0; i < 4; i++) rainbow += diffGrating(uLightDirs[i], Vd, Td, Nd) * uLightWeights[i];
					outgoingLight += rainbow * uDiffraction;
				}
				#include <opaque_fragment>`
			);
	};
	material.customProgramCacheKey = () => `diffraction:${strength}`;
	return material;
}

export function dataSide(): THREE.MeshPhysicalMaterial {
	return withDiffraction(
		new THREE.MeshPhysicalMaterial({
			color: 0xd9dde6,
			metalness: 1,
			roughness: 0.14,
			envMapIntensity: 1.1,
			// The tracks stretch every reflection into a spoke.
			anisotropy: 0.7,
			anisotropyMap: tangentMap(),
			side: THREE.DoubleSide
		})
	);
}

// -- canvas helpers ------------------------------------------------------------------

function canvas(size: number) {
	const el = document.createElement('canvas');
	el.width = el.height = size;
	return { el, ctx: el.getContext('2d', { willReadFrequently: true })! };
}

function finish(el: HTMLCanvasElement, color: boolean): THREE.Texture {
	const texture = new THREE.CanvasTexture(el);
	texture.colorSpace = color ? THREE.SRGBColorSpace : THREE.NoColorSpace;
	texture.anisotropy = 8;
	texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
	return texture;
}

/** A height field (0..1 per pixel) to a tangent-space normal map. */
function normalsFrom(height: Float32Array, size: number, strength: number): THREE.Texture {
	const { el, ctx } = canvas(size);
	const image = ctx.createImageData(size, size);
	const at = (x: number, y: number) => height[((y + size) % size) * size + ((x + size) % size)];
	for (let y = 0; y < size; y++) {
		for (let x = 0; x < size; x++) {
			const dx = (at(x + 1, y) - at(x - 1, y)) * strength;
			const dy = (at(x, y + 1) - at(x, y - 1)) * strength;
			const len = Math.hypot(dx, dy, 1);
			const i = (y * size + x) * 4;
			image.data[i] = ((-dx / len) * 0.5 + 0.5) * 255;
			// Canvas rows run down, texture v runs up.
			image.data[i + 1] = ((dy / len) * 0.5 + 0.5) * 255;
			image.data[i + 2] = (1 / len) * 255;
			image.data[i + 3] = 255;
		}
	}
	ctx.putImageData(image, 0, 0);
	return finish(el, false);
}

function noise(size: number, blur: number, seed = 1): Float32Array {
	const { el, ctx } = canvas(size);
	let s = seed * 9301 + 49297;
	const rand = () => ((s = (s * 9301 + 49297) % 233280) / 233280);
	const image = ctx.createImageData(size, size);
	for (let i = 0; i < size * size; i++) {
		const v = rand() * 255;
		image.data[i * 4] = image.data[i * 4 + 1] = image.data[i * 4 + 2] = v;
		image.data[i * 4 + 3] = 255;
	}
	ctx.putImageData(image, 0, 0);
	if (blur > 0) {
		const copy = canvas(size);
		copy.ctx.filter = `blur(${blur}px)`;
		copy.ctx.drawImage(el, 0, 0);
		return readHeight(copy.ctx, size);
	}
	return readHeight(ctx, size);
}

function readHeight(ctx: CanvasRenderingContext2D, size: number): Float32Array {
	const data = ctx.getImageData(0, 0, size, size).data;
	const out = new Float32Array(size * size);
	for (let i = 0; i < out.length; i++) out[i] = data[i * 4] / 255;
	return out;
}

// -- vinyl ---------------------------------------------------------------------------

export interface VinylMaps {
	normal: THREE.Texture;
	anisotropy: THREE.Texture;
	color: THREE.Texture;
}

const vinylCache = new Map<number, VinylMaps>();

/** Maps for a record face drawn on a RingGeometry of outer radius 1 (its UVs
 * are planar: u = (x + 1) / 2, v = (y + 1) / 2). Drawn once per size and
 * shared: they take a moment to draw, and hovering shows records often. */
export function vinylMaps(size = 1024): VinylMaps {
	let maps = vinylCache.get(size);
	if (!maps) {
		maps = drawVinylMaps(size);
		vinylCache.set(size, maps);
	}
	return maps;
}

function drawVinylMaps(size: number): VinylMaps {
	const height = new Float32Array(size * size);
	const aniso = canvas(size);
	const color = canvas(size);
	const aImage = aniso.ctx.createImageData(size, size);
	const cImage = color.ctx.createImageData(size, size);
	// Smooth bands between tracks, the dead wax by the label, the lead-in rim.
	const gaps = [0.52, 0.64, 0.75, 0.86];
	// Loud passages cut wider grooves than quiet ones, which is why a real
	// side shows soft bands of darker and brighter sheen inside each track.
	const loud = (r: number) =>
		0.5 + 0.22 * Math.sin(r * 41 + 1.3) + 0.16 * Math.sin(r * 97 + 0.4) + 0.12 * Math.sin(r * 211 + 2.1);
	for (let py = 0; py < size; py++) {
		for (let px = 0; px < size; px++) {
			const u = (px + 0.5) / size * 2 - 1;
			const v = 1 - (py + 0.5) / size * 2;
			const r = Math.hypot(u, v);
			const i = py * size + px;
			let grooved = r > 0.42 && r < 0.975;
			for (const g of gaps) if (Math.abs(r - g) < 0.008) grooved = false;
			height[i] = grooved ? 0.5 + 0.5 * Math.sin(r * 900) : 0.5;
			// The direction a light is smeared in: across the grooves, toward
			// the centre, which is what draws a record's two bright spokes.
			const t = r > 0 ? [u / r, v / r] : [1, 0];
			aImage.data[i * 4] = (t[0] * 0.5 + 0.5) * 255;
			aImage.data[i * 4 + 1] = (t[1] * 0.5 + 0.5) * 255;
			const sheen = grooved ? loud(r) : 0;
			aImage.data[i * 4 + 2] = grooved ? 175 + 80 * sheen : 60;
			aImage.data[i * 4 + 3] = 255;
			// Pressed black, a touch lighter where the grooves scatter light.
			const shade = grooved ? 13 + 9 * sheen : r > 0.975 ? 20 : 9;
			cImage.data[i * 4] = shade;
			cImage.data[i * 4 + 1] = shade;
			cImage.data[i * 4 + 2] = shade + 2;
			cImage.data[i * 4 + 3] = 255;
		}
	}
	aniso.ctx.putImageData(aImage, 0, 0);
	color.ctx.putImageData(cImage, 0, 0);
	return {
		normal: normalsFrom(height, size, 0.35),
		anisotropy: finish(aniso.el, false),
		color: finish(color.el, true)
	};
}

export function vinylMaterial(maps: VinylMaps): THREE.MeshPhysicalMaterial {
	return new THREE.MeshPhysicalMaterial({
		color: 0xffffff,
		map: maps.color,
		normalMap: maps.normal,
		normalScale: new THREE.Vector2(0.6, 0.6),
		roughness: 0.3,
		metalness: 0,
		// Groove walls throw far more light back than flat vinyl would: a
		// high index stands in for them, so the key light makes the streak.
		ior: 2.3,
		anisotropy: 0.9,
		anisotropyMap: maps.anisotropy,
		clearcoat: 0.6,
		clearcoatRoughness: 0.2,
		specularIntensity: 1,
		side: THREE.DoubleSide
	});
}

// -- disc details --------------------------------------------------------------------

let tangentCache: THREE.Texture | null = null;

/** An anisotropy map for anything turned about its centre (planar UVs over
 * a disc): the direction points out from the centre everywhere, at full
 * strength. A CD's tracks run round the disc and smear a light across
 * themselves, into a spoke toward the hub. */
export function tangentMap(): THREE.Texture {
	if (tangentCache) return tangentCache;
	const size = 256;
	const { el, ctx } = canvas(size);
	const image = ctx.createImageData(size, size);
	for (let py = 0; py < size; py++) {
		for (let px = 0; px < size; px++) {
			const u = ((px + 0.5) / size) * 2 - 1;
			const v = 1 - ((py + 0.5) / size) * 2;
			const r = Math.hypot(u, v) || 1;
			const i = (py * size + px) * 4;
			image.data[i] = ((u / r) * 0.5 + 0.5) * 255;
			image.data[i + 1] = ((v / r) * 0.5 + 0.5) * 255;
			image.data[i + 2] = 255;
			image.data[i + 3] = 255;
		}
	}
	ctx.putImageData(image, 0, 0);
	tangentCache = finish(el, false);
	return tangentCache;
}

let matrixCache: THREE.Texture | null = null;

/** What a pressing plant etches into a disc's mirror band: a matrix number
 * and a barcode, running round the hub. Drawn for a ring whose outer radius
 * is the texture's edge; the marks sit in its outer eighth. Too small to
 * read at stage size, which is how the real ones look too. */
export function matrixRing(): THREE.Texture {
	if (matrixCache) return matrixCache;
	const size = 1024;
	const { el, ctx } = canvas(size);
	ctx.translate(size / 2, size / 2);
	ctx.fillStyle = 'rgba(52,58,78,1)';
	const at = size / 2 - 34;
	// The barcode: a third of the way round.
	let s = 7;
	const rand = () => ((s = (s * 9301 + 49297) % 233280) / 233280);
	for (let a = 0.2; a < 2.2; ) {
		const w = 0.004 + rand() * 0.012;
		ctx.save();
		ctx.rotate(a);
		ctx.fillRect(-(w * at) / 2, -at - 14, w * at, 28);
		ctx.restore();
		a += w + 0.006 + rand() * 0.012;
	}
	// The matrix number and mould codes, letter by letter round the rest.
	ctx.font = '600 22px ui-monospace, Menlo, monospace';
	ctx.textAlign = 'center';
	ctx.textBaseline = 'middle';
	const text = 'EXS 0417 2A  ·  01 · A3  ·  L 0926  ·  94V1 · 06 · 77';
	for (let i = 0; i < text.length; i++) {
		ctx.save();
		ctx.rotate(2.75 + i * 0.066);
		ctx.fillText(text[i], 0, -at);
		ctx.restore();
	}
	matrixCache = finish(el, true);
	matrixCache.wrapS = matrixCache.wrapT = THREE.ClampToEdgeWrapping;
	return matrixCache;
}

let labelCache: THREE.Texture | null = null;

/** A record label's surface: paper grain, and the rings the press leaves in
 * it, one round the spindle and one near the edge where the label's paper
 * steps down into the vinyl. For a disc with planar UVs. */
export function labelPress(): THREE.Texture {
	if (labelCache) return labelCache;
	const size = 512;
	const grain = noise(size, 0.8, 5);
	const height = new Float32Array(size * size);
	const dip = (r: number, at: number, width: number) => Math.exp(-(((r - at) / width) ** 2));
	for (let py = 0; py < size; py++) {
		for (let px = 0; px < size; px++) {
			const u = ((px + 0.5) / size) * 2 - 1;
			const v = 1 - ((py + 0.5) / size) * 2;
			const r = Math.hypot(u, v);
			height[py * size + px] = grain[py * size + px] * 0.12 - dip(r, 0.3, 0.018) * 0.8 - dip(r, 0.93, 0.02) * 0.5;
		}
	}
	labelCache = normalsFrom(height, size, 3);
	labelCache.wrapS = labelCache.wrapT = THREE.ClampToEdgeWrapping;
	return labelCache;
}

// -- paper, cardstock, glyphs ----------------------------------------------------------

let grainCache: THREE.Texture | null = null;

/** Fine paper grain, tiling. Shared by every paper surface. */
export function paperGrain(): THREE.Texture {
	if (!grainCache) {
		const size = 512;
		const fine = noise(size, 0.6, 3);
		const coarse = noise(size, 3, 7);
		const height = new Float32Array(size * size);
		for (let i = 0; i < height.length; i++) height[i] = fine[i] * 0.6 + coarse[i] * 0.4;
		grainCache = normalsFrom(height, size, 1.2);
		grainCache.repeat.set(3, 3);
	}
	return grainCache;
}

const debossCache = new Map<string, { color: THREE.Texture; normal: THREE.Texture }>();
const DEBOSS_KEEP = 12;

/** A folder front: its colour, with the glyph pressed in. The ExtrudeGeometry
 * front face maps UVs to shape coordinates; `bounds` is the flap's
 * [width, height] so the maps span it exactly. The last dozen are kept, since
 * moving along the drum shows the same folders again and again. */
export function debossMaps(
	path: string | null,
	colour: THREE.Color,
	bounds: [number, number]
): { color: THREE.Texture; normal: THREE.Texture } {
	const key = `${path ?? ''}|${colour.getHexString()}|${bounds.join('x')}`;
	const hit = debossCache.get(key);
	if (hit) {
		debossCache.delete(key);
		debossCache.set(key, hit);
		return hit;
	}
	const made = drawDeboss(path, colour, bounds);
	debossCache.set(key, made);
	if (debossCache.size > DEBOSS_KEEP) {
		const [oldest, old] = debossCache.entries().next().value!;
		debossCache.delete(oldest);
		// Not disposed: a folder on screen may still use it. The GPU copy
		// goes with its last material.
		void old;
	}
	return made;
}

function drawDeboss(
	path: string | null,
	colour: THREE.Color,
	bounds: [number, number]
): { color: THREE.Texture; normal: THREE.Texture } {
	const size = 768;
	const grain = noise(size, 1.2, 11);
	const height = new Float32Array(size * size);
	const press = canvas(size);
	const tint = canvas(size);
	tint.ctx.fillStyle = `#${colour.getHexString()}`;
	tint.ctx.fillRect(0, 0, size, size);
	if (path) {
		// The glyph, 0.5 units square on the flap, centred. The square texture
		// is spread over a flap wider than it is tall, so the glyph is drawn
		// squeezed by the same proportion and lands at its true shape.
		const glyph = 0.5;
		const sx = ((glyph / bounds[0]) * size) / 24;
		const sy = ((glyph / bounds[1]) * size) / 24;
		const draw = (ctx: CanvasRenderingContext2D, style: string, blur: number, width: number) => {
			ctx.save();
			ctx.filter = blur ? `blur(${blur}px)` : 'none';
			ctx.translate(size / 2 - 12 * sx, size / 2 - 12 * sy);
			ctx.scale(sx, sy);
			ctx.lineCap = 'round';
			ctx.lineJoin = 'round';
			ctx.lineWidth = width;
			ctx.strokeStyle = style;
			ctx.stroke(new Path2D(path));
			ctx.restore();
		};
		press.ctx.fillStyle = '#000';
		press.ctx.fillRect(0, 0, size, size);
		draw(press.ctx, '#fff', 5, 1.9);
		const dark = colour.clone().multiplyScalar(0.8);
		draw(tint.ctx, `#${dark.getHexString()}`, 2, 1.7);
	}
	const pressed = path ? readHeight(press.ctx, size) : null;
	for (let i = 0; i < height.length; i++) {
		height[i] = grain[i] * 0.05 - (pressed ? pressed[i] : 0) * 0.9;
	}
	const normal = normalsFrom(height, size, 6);
	const color = finish(tint.el, true);
	for (const t of [normal, color]) {
		t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
		t.repeat.set(1 / bounds[0], 1 / bounds[1]);
		t.offset.set(0.5, 0.5);
	}
	return { color, normal };
}

/** A soft round shadow on the floor under an object. */
export function contactShadow(): THREE.Texture {
	const { el, ctx } = canvas(256);
	const g = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
	g.addColorStop(0, 'rgba(10,12,24,0.55)');
	g.addColorStop(0.45, 'rgba(10,12,24,0.22)');
	g.addColorStop(1, 'rgba(10,12,24,0)');
	ctx.fillStyle = g;
	ctx.fillRect(0, 0, 256, 256);
	return finish(el, true);
}

// -- glass ---------------------------------------------------------------------------

export function glass(tint = 0xf2f0ff, thickness = 1): THREE.MeshPhysicalMaterial {
	return new THREE.MeshPhysicalMaterial({
		color: 0xffffff,
		metalness: 0,
		roughness: 0.03,
		transmission: 1,
		thickness,
		ior: 1.52,
		dispersion: 5,
		attenuationColor: new THREE.Color(tint),
		attenuationDistance: 6,
		specularIntensity: 1,
		clearcoat: 0.4,
		clearcoatRoughness: 0.02,
		iridescence: 0.45,
		iridescenceIOR: 1.6,
		iridescenceThicknessRange: [200, 600],
		envMapIntensity: 1.35
	});
}

// -- prismatic crystal ---------------------------------------------------------------

/** The time the light inside every crystal runs on, in seconds. scene.ts
 * advances it each frame and holds it still under reduced motion. One object
 * shared by every prism material and the floor caustic. */
export const prismTime = { value: 0 };

const PRISM_GLSL = /* glsl */ `
	// Hue 0 red, 0.17 yellow, 0.33 green, 0.5 cyan, 0.67 blue, 0.8 violet.
	vec3 prismHue(float h) {
		return clamp(abs(fract(h + vec3(0.0, 2.0 / 3.0, 1.0 / 3.0)) * 6.0 - 3.0) - 1.0, 0.0, 1.0);
	}
	// The bright net light draws after passing through a rippled clear
	// body (water, a gem): thin filaments that open and close. Tiling in
	// 0..1; after the well-known iterated-sine water caustic.
	float prismCaustic(vec2 uv, float t) {
		vec2 p = mod(uv * 6.2831853, 6.2831853) - 250.0;
		vec2 i = p;
		float c = 1.0;
		for (int n = 0; n < 4; n++) {
			float tt = t * (1.0 - 3.5 / float(n + 1));
			i = p + vec2(cos(tt - i.x) + sin(tt + i.y), sin(tt - i.y) + cos(tt + i.x));
			c += 1.0 / length(vec2(p.x / (sin(i.x + tt) / 0.005), p.y / (cos(i.y + tt) / 0.005)));
		}
		c = 1.17 - pow(c / 4.0, 1.4);
		return pow(abs(c), 8.0);
	}
	// Small hard lamps around the studio, which only the crystals see. The
	// softboxes are too broad to sparkle; a jewel's fire is a point of light
	// drawn out into its colours.
	float prismLamps(vec3 d) {
		float g = 0.0;
		g += pow(max(dot(d, vec3(-0.42, 0.74, 0.52)), 0.0), 700.0);
		g += pow(max(dot(d, vec3(0.62, 0.55, 0.56)), 0.0), 900.0) * 0.8;
		g += pow(max(dot(d, vec3(0.1, 0.96, -0.26)), 0.0), 600.0) * 0.9;
		g += pow(max(dot(d, vec3(-0.78, 0.12, -0.61)), 0.0), 800.0) * 0.7;
		g += pow(max(dot(d, vec3(0.83, -0.2, -0.52)), 0.0), 700.0) * 0.7;
		g += pow(max(dot(d, vec3(-0.2, -0.62, 0.76)), 0.0), 900.0) * 0.6;
		return g;
	}
	// Snell's law for a ray leaving the crystal, but past the critical angle
	// it slides along the wall instead of flipping to a mirror bounce: the
	// flip is a hard line, and on a curved body it shows as a ragged one.
	vec3 prismLeave(vec3 I, vec3 N, float eta) {
		float c = dot(N, I);
		float k = max(1.0 - eta * eta * (1.0 - c * c), 0.0);
		return normalize(eta * I - (eta * c + sqrt(k)) * N);
	}
`;

export interface PrismOptions {
	/** How much of the rainbow band, fire and shimmer shows. */
	strength?: number;
	/** How much the body acts as a lens: 1 a ball, which turns the room
	 * upside down inside itself; 0 a flat pane, which only shifts it. */
	depth?: number;
	/** A flat piece turned about its local z axis (a record): its far wall
	 * is flat whatever is cut into the near one, so grooves act as lenses. */
	pane?: boolean;
	/** How much of the band lies over the parts facing the viewer, not only
	 * toward the rim. Low keeps a face clear (the clock's dial). */
	face?: number;
}

/** A polished crystal rather than window glass. Three things make the
 * difference, and none of them comes from three's transmission, which over a
 * flat page colour has nothing to bend or split:
 *
 * - The studio seen through the body. Each ray is bent in at the front and
 *   out at the far wall (or bounced off it, past the critical angle), once
 *   per colour of the spectrum with its own index, and the softboxes it
 *   lands on are drawn inside the crystal: bright shapes with coloured
 *   edges, dark where it looks at the floor. That is the depth and the fire.
 * - The rainbow a clear ball gathers on the side away from the key light:
 *   red at the band's outer edge through to violet (blue bends most), a
 *   white-hot core where it focuses, a fainter reversed bow inside it, and
 *   rainbow fringes along the rim.
 * - Light that moves. The band breathes and its colours drift, the room
 *   inside turns a little as if the lights swung, and a net of caustic
 *   filaments, split into colours, plays through the far side. All of it
 *   runs on prismTime. */
export function withPrism<T extends THREE.MeshPhysicalMaterial>(material: T, options: PrismOptions = {}): T {
	const { strength = 1, depth = 1, pane = false, face = 0.5 } = options;
	const before = material.onBeforeCompile;
	const beforeKey = material.customProgramCacheKey;
	material.onBeforeCompile = (shader, renderer) => {
		before?.call(material, shader, renderer);
		shader.uniforms.uPrismLight = { value: LIGHT_DIRECTIONS[0] };
		shader.uniforms.uPrism = { value: strength };
		shader.uniforms.uPrismDepth = { value: depth };
		shader.uniforms.uPrismPane = { value: pane ? 1 : 0 };
		shader.uniforms.uPrismFace = { value: face };
		shader.uniforms.uPrismTime = prismTime;
		shader.vertexShader = shader.vertexShader
			.replace(
				'#include <common>',
				'#include <common>\nvarying vec3 vPrismPos;\nvarying vec3 vPrismLocal;\nvarying vec3 vPrismCenter;\nvarying vec3 vPrismAxis;'
			)
			.replace(
				'#include <project_vertex>',
				`#include <project_vertex>
				vPrismPos = (modelMatrix * vec4(transformed, 1.0)).xyz;
				vPrismLocal = transformed;
				vPrismCenter = (modelMatrix * vec4(0.0, 0.0, 0.0, 1.0)).xyz;
				vPrismAxis = normalize(mat3(modelMatrix) * vec3(0.0, 0.0, 1.0));`
			);
		shader.fragmentShader = shader.fragmentShader
			.replace(
				'#include <common>',
				`#include <common>
				varying vec3 vPrismPos;
				varying vec3 vPrismLocal;
				varying vec3 vPrismCenter;
				varying vec3 vPrismAxis;
				uniform vec3 uPrismLight;
				uniform float uPrism;
				uniform float uPrismDepth;
				uniform float uPrismPane;
				uniform float uPrismFace;
				uniform float uPrismTime;
				${PRISM_GLSL}`
			)
			.replace(
				'#include <opaque_fragment>',
				`{
					float tp = uPrismTime;
					// The shaded normal (grooves and all) and the smooth one
					// under it, in world space.
					vec3 Np = inverseTransformDirection(normal, viewMatrix);
					vec3 Ng = inverseTransformDirection(nonPerturbedNormal, viewMatrix);
					vec3 Vp = normalize(cameraPosition - vPrismPos);
					float ndv = clamp(dot(Ng, Vp), 0.0, 1.0);

					// -- the room through the crystal --
					// The far wall, as the ray meets it from inside: for a ball
					// the front normal with its view component flipped (exact
					// seen straight on), for a pane the front normal itself.
					// A pane's back is flat whatever is cut into its front.
					vec3 flatWall = vPrismAxis * (dot(vPrismAxis, Vp) < 0.0 ? -1.0 : 1.0);
					vec3 Nin = normalize(mix(mix(Ng, flatWall, uPrismPane), 2.0 * dot(Ng, Vp) * Vp - Np, uPrismDepth));
					vec3 fire = vec3(0.0);
					vec3 glint = vec3(0.0);
					#ifdef ENVMAP_TYPE_CUBE_UV
					{
						// The lights swing a little, so the room inside drifts
						// even while the crystal hangs still.
						float swing = 0.3 * sin(tp * 0.37) + 0.14 * sin(tp * 0.83 + 1.3);
						float cs = cos(swing);
						float sn = sin(swing);
						float spread = 0.2 * (1.0 + 0.3 * sin(tp * 0.6));
						vec3 weight = vec3(0.0);
						for (int i = 0; i < 6; i++) {
							float w = float(i) / 5.0; // 0 red .. 1 violet
							vec3 colour = prismHue(0.78 * w);
							float eta = 1.0 / (1.5 + spread * (w - 0.35));
							vec3 r = refract(-Vp, Np, eta);
							vec3 o = prismLeave(r, Nin, 1.0 / eta);
							o = vec3(cs * o.x + sn * o.z, o.y, cs * o.z - sn * o.x);
							fire += textureCubeUV(envMap, envMapRotation * o, 0.0).rgb * colour;
							glint += prismLamps(o) * colour;
							weight += colour;
						}
						fire /= weight;
						glint /= weight;
						// And the lamps mirrored in the polished surface.
						vec3 m = reflect(-Vp, Np);
						m = vec3(cs * m.x + sn * m.z, m.y, cs * m.z - sn * m.x);
						glint += vec3(prismLamps(m)) * 0.6;
					}
					#else
					fire = vec3(0.42);
					#endif
					// Over a page the body multiplies what shows through it:
					// clear where it sees the studio's grey, deeper where it
					// sees the floor, lit where it sees a lamp. The lamps
					// themselves are added, so they also burn on a dark page.
					// At the very rim the bending outruns the mesh, so it fades there.
					float inside = smoothstep(0.02, 0.3, ndv);
					vec3 lens = clamp(fire / 0.4, 0.0, 3.0);
					lens = mix(vec3(1.0), 0.5 + 0.5 * lens, uPrism * inside);
					outgoingLight *= clamp(lens, 0.5, 1.7);
					outgoingLight += max(fire - 1.0, 0.0) * 0.16 * uPrism * inside;
					// Fire: where one colour of a lamp lands alone it shows
					// pure, where they all land it is white.
					glint *= 4.0 * uPrism * inside;
					outgoingLight = outgoingLight * (1.0 - 0.9 * clamp(max(glint.r, max(glint.g, glint.b)), 0.0, 1.0)) + glint;

					// -- the rainbow band --
					// The light gathers on the part of the far wall facing
					// away from it.
					vec3 Nb = normalize(Ng - 2.0 * dot(Ng, Vp) * Vp);
					float awayBack = dot(Nb, -uPrismLight);
					// And across the object: from the side facing the light (-1)
					// to the side away from it (+1), in the picture plane. Flat
					// glass, which has one normal, gets its spread from this.
					vec3 Lp = normalize(uPrismLight - dot(uPrismLight, Vp) * Vp);
					float across = clamp(dot(vPrismPos - vPrismCenter, -Lp), -1.0, 1.0);
					float s = 0.5 * awayBack + 0.6 * across;
					// It breathes: slow swells that travel along the band, so
					// its edge ripples the way a focus does when a gem turns.
					vec3 q = vPrismLocal;
					s += 0.05 * sin(tp * 0.9 + q.y * 3.1 + q.x * 1.7) + 0.035 * sin(tp * 0.53 - q.x * 4.3 + 2.0) + 0.03 * sin(tp * 0.31);
					// A crescent on the far side; the rest stays clear.
					float band = smoothstep(0.34, 0.46, s) * (1.0 - smoothstep(0.92, 1.08, s));
					// Strongest toward the rim, where the light gathers.
					band *= mix(uPrismFace, 1.0, smoothstep(0.1, 0.8, 1.0 - ndv));
					// Red at the band's outer edge through to violet; the
					// whole spectrum slides a little back and forth.
					float drift = 0.07 * sin(tp * 0.47) + 0.04 * sin(tp * 1.1 + q.y * 2.0);
					vec3 spectrum = prismHue(0.8 * clamp((s - 0.45) / 0.55 + drift, 0.0, 1.0));
					// Tint rather than add, so the colours stay saturated over a
					// light page instead of washing out to white; partly, so the
					// crystal shows through.
					vec3 tinted = outgoingLight * mix(vec3(1.0), spectrum * 1.7, 0.95) + spectrum * 0.14;
					outgoingLight = mix(outgoingLight, tinted, band * 0.9 * uPrism);
					// The second bow, fainter and reversed, inside the first.
					float bow = smoothstep(-0.05, 0.08, s) * (1.0 - smoothstep(0.16, 0.3, s));
					vec3 second = prismHue(0.8 * clamp(1.0 - (s + 0.05) / 0.35 - drift, 0.0, 1.0));
					outgoingLight = mix(outgoingLight, outgoingLight * mix(vec3(1.0), second * 1.6, 0.8), bow * 0.3 * uPrism * (0.4 + 0.6 * (1.0 - ndv)));

					// -- caustics inside --
					// A net of focused light on the far wall, each colour
					// focused a little further along, seen through the front
					// (so it slides against the surface as the crystal turns).
					{
						vec2 cq = q.xy * 0.5 + Nin.xy * 0.22 * uPrismDepth + vec2(tp * 0.011, -tp * 0.007);
						vec2 cd = -Lp.xy * 0.012;
						float ct = tp * 0.45;
						vec3 net = vec3(prismCaustic(cq - cd, ct), prismCaustic(cq, ct), prismCaustic(cq + cd, ct));
						float where = (0.25 + 0.75 * smoothstep(0.05, 0.6, s)) * (0.35 + 0.65 * ndv);
						float mean = dot(net, vec3(1.0 / 3.0));
						outgoingLight *= 1.0 + (net - mean) * 1.4 * where * uPrism;
						outgoingLight += net * 0.2 * where * uPrism;
					}

					// The focus: a white-hot core inside the band, pulsing.
					float core = exp(-pow((s - 0.62) / 0.06, 2.0)) * (0.8 + 0.2 * sin(tp * 1.3 + q.y * 5.0));
					outgoingLight += vec3(1.0, 0.98, 0.92) * core * 0.26 * uPrism;
					// Rainbow fringes where the rim bends light hardest; they
					// run round the rim.
					float rim = pow(1.0 - ndv, 2.2);
					outgoingLight = mix(outgoingLight, outgoingLight * mix(vec3(1.0), prismHue(fract(ndv * 1.2 + s * 0.6 + tp * 0.05)) * 1.5, 0.8), rim * 0.55 * uPrism);
				}
				#include <opaque_fragment>`
			);
	};
	// Strength and depth are uniforms, so every prism shares one program.
	material.customProgramCacheKey = () => `${beforeKey?.call(material) ?? ''}|prism`;
	return material;
}

/** Polished crystal: denser than window glass, so it mirrors the studio more
 * and bends light further, and perfectly smooth. */
export function prismGlass(tint = 0xfbfaff, thickness = 1, options: PrismOptions = {}): THREE.MeshPhysicalMaterial {
	const material = glass(tint, thickness);
	// The prism carries the colour; the thin-film sheen would muddy it.
	material.iridescence = 0;
	material.dispersion = 8;
	material.ior = 1.7;
	material.roughness = 0;
	material.clearcoat = 1;
	material.clearcoatRoughness = 0;
	material.envMapIntensity = 1.5;
	return withPrism(material, options);
}

/** The rainbow a crystal throws on the floor, inside its shadow: a white
 * core, red and orange one side, cyan through violet the other, woven from
 * the same moving caustic net as the light inside it. A plane of this is
 * laid on the floor by scene.ts, which drives uOpacity. */
export function causticMaterial(): THREE.ShaderMaterial {
	return new THREE.ShaderMaterial({
		transparent: true,
		depthWrite: false,
		toneMapped: false,
		uniforms: { uTime: prismTime, uOpacity: { value: 0 } },
		vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
		fragmentShader: /* glsl */ `
			varying vec2 vUv;
			uniform float uTime;
			uniform float uOpacity;
			${PRISM_GLSL}
			void main() {
				vec2 p = vUv * 2.0 - 1.0;
				float t = uTime;
				// The spectrum runs along the plane and slides as the light plays.
				float x = vUv.x + 0.05 * sin(t * 0.47) + 0.03 * sin(t * 0.9 + p.y * 3.0);
				float w = clamp((x - 0.1) / 0.8, 0.0, 1.0);
				// Red holds a little way in before the spectrum starts to run.
				vec3 colour = prismHue(0.8 * clamp((w - 0.14) / 0.86, 0.0, 1.0));
				// White where it focuses, in the middle.
				float core = exp(-pow((x - 0.5) / 0.09, 2.0));
				colour = mix(colour, vec3(1.0, 1.0, 0.96), core * 0.85);
				// A soft lens shape, brightest through the middle.
				float shape = 1.0 - smoothstep(0.25, 1.0, length(p));
				shape *= smoothstep(0.0, 0.12, w) * (1.0 - smoothstep(0.88, 1.0, w));
				// The net, each colour a little displaced.
				vec2 cq = vUv * vec2(0.5, 0.25) + vec2(t * 0.011, -t * 0.007);
				float ct = t * 0.45;
				vec3 net = vec3(prismCaustic(cq - vec2(0.012, 0.0), ct), prismCaustic(cq, ct), prismCaustic(cq + vec2(0.012, 0.0), ct));
				float lit = dot(net, vec3(1.0 / 3.0));
				colour = colour * (0.8 + 0.5 * lit) + net * 0.3;
				float alpha = shape * (0.72 + 0.5 * lit) * uOpacity;
				// Display colours as they are, like the shadow under it.
				gl_FragColor = vec4(colour, clamp(alpha, 0.0, 1.0));
			}
		`
	});
}
