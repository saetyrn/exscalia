// The Music page's 3D stage (MUSIC.md section 5): one floating object at a
// time over a soft floor shadow, lit by a photo studio. The objects are built
// in objects.ts, their materials in materials.ts.
//
// Motion, the owner's spec: idle, a disc bobs gently and turns slowly at a
// constant rate about its vertical axis, so its rainbow back shows; other
// objects bob and sway about 25 degrees. Play turns the disc to face front
// and settles the bob (about 0.8 s), then spins it about its hole up to one
// turn every 12 s. Pause freezes it where it is; after 5 s still paused it
// eases back into the idle bob and turn. Reduced motion holds everything still.
//
// The light inside the crystal icons, and in the rainbow they throw on the
// floor, runs on its own clock (materials.ts prismTime), advanced here; it
// too holds still under reduced motion.
import * as THREE from 'three';
import { causticMaterial, contactShadow, LIGHT_DIRECTIONS, prismTime, studioEnvironment } from './materials';
import { build, objectKey, type Built, type StageObject, type TextureLoader } from './objects';

export { objectKey, type StageObject, type TextureLoader } from './objects';

const TAU = Math.PI * 2;
const BOB_HEIGHT = 0.07;
const BOB_PERIOD = 3; // seconds, up and down
const IDLE_TURN = TAU / 10; // one turn every 10 s
const PLAY_SPIN = TAU / 12; // saetyrn.com's disc: one turn every 12 s
const SWAY = 0.44; // about 25 degrees
const SWAY_PERIOD = 9;
const RESUME_AFTER = 5; // seconds paused before the idle motion comes back
const FLOOR = -1.42;
const CAMERA_DISTANCE = 7.4;
// The key light, for everything but crystal: the studio map alone lights a
// surface evenly from all round, which is flat. A lamp where the key softbox
// hangs gives printed and moulded things a lit side, a highlight that travels
// as they turn, and the shadows their parts throw on each other. The map is
// turned down by what the lamp adds, so covers keep their printed colours.
const KEY_LIGHT = 1.5;
const KEY_ENVIRONMENT = 0.78;

/** Exponential approach: `rate` per second, frame-rate independent. */
function approach(current: number, target: number, rate: number, dt: number): number {
	return target + (current - target) * Math.exp(-rate * dt);
}

type Phase = 'idle' | 'align' | 'spinning' | 'frozen' | 'resuming';

interface Live {
	key: string;
	built: Built;
	phase: Phase;
	bobPhase: number;
	bobRate: number;
	bobAmp: number;
	yaw: number;
	turnSpeed: number;
	swayPhase: number;
	swayRate: number;
	spinAngle: number;
	spinSpeed: number;
	alignTo: number;
	frozenAt: number;
	enter: number;
}

export interface StageOptions {
	reducedMotion: boolean;
	/** The page colour behind the canvas, which glass refracts. */
	backdrop: string;
}

export class StageScene {
	readonly renderer: THREE.WebGLRenderer;
	readonly scene = new THREE.Scene();
	readonly camera = new THREE.PerspectiveCamera(28, 1, 0.1, 60);
	#env: THREE.Texture;
	#shadow: THREE.Mesh;
	#shadowTexture: THREE.Texture;
	#backdrop: THREE.Mesh;
	#caustic: THREE.Mesh;
	#key: THREE.DirectionalLight;
	#live: Live | null = null;
	#leaving: { live: Live; t: number }[] = [];
	#playing = false;
	#wanted: string | null = null;
	#clock = new THREE.Clock();
	#reduced: boolean;
	#load: TextureLoader;
	#textures = new Map<string, Promise<THREE.Texture | null>>();

	constructor(canvas: HTMLCanvasElement, load: TextureLoader, options: StageOptions) {
		this.renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: 'high-performance' });
		this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
		this.renderer.outputColorSpace = THREE.SRGBColorSpace;
		// Khronos PBR Neutral: made for product renders, it keeps whites white
		// and printed colours true, where AgX greyed glass and covers.
		this.renderer.toneMapping = THREE.NeutralToneMapping;
		this.renderer.toneMappingExposure = 1;
		this.renderer.setClearColor(0x000000, 0);
		// Glass refracts what was drawn behind it in a separate pass. On a
		// see-through canvas that is nothing at all (alpha 0), which three
		// turns into blown-out white. This wall, in the page's colour, is
		// painted only in that pass: in the visible one it writes no colour.
		this.#backdrop = new THREE.Mesh(
			new THREE.PlaneGeometry(80, 80),
			new THREE.MeshBasicMaterial({ color: 0xffffff, depthWrite: false, toneMapped: false })
		);
		this.#backdrop.position.z = -8;
		this.#backdrop.renderOrder = -1;
		this.#backdrop.onBeforeRender = (renderer) => {
			(this.#backdrop.material as THREE.MeshBasicMaterial).colorWrite = renderer.getRenderTarget() !== null;
		};
		this.scene.add(this.#backdrop);
		this.setBackdrop(options.backdrop);
		this.#env = studioEnvironment(this.renderer);
		this.scene.environment = this.#env;
		this.scene.environmentIntensity = 1;

		// One shadow map, the stage's one extra pass. Crystal neither casts
		// nor takes it (objects.ts), and the lamp is dark for crystal.
		this.renderer.shadowMap.enabled = true;
		this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
		this.#key = new THREE.DirectionalLight(0xffffff, 0);
		this.#key.position.copy(LIGHT_DIRECTIONS[0]).multiplyScalar(9);
		this.#key.castShadow = true;
		this.#key.shadow.mapSize.set(1024, 1024);
		const lit = this.#key.shadow.camera;
		lit.left = lit.bottom = -1.9;
		lit.right = lit.top = 1.9;
		lit.near = 5;
		lit.far = 13;
		this.#key.shadow.bias = -0.0006;
		this.#key.shadow.normalBias = 0.012;
		this.#key.shadow.radius = 3;
		this.scene.add(this.#key);

		this.#shadowTexture = contactShadow();
		this.#shadow = new THREE.Mesh(
			new THREE.PlaneGeometry(3.4, 3.4),
			new THREE.MeshBasicMaterial({ map: this.#shadowTexture, transparent: true, depthWrite: false, toneMapped: false })
		);
		this.#shadow.rotation.x = -Math.PI / 2;
		this.#shadow.position.y = FLOOR;
		this.scene.add(this.#shadow);
		// Glass throws a rainbow onto the floor, inside its shadow and away
		// from the key light (high front left).
		this.#caustic = new THREE.Mesh(new THREE.PlaneGeometry(2.2, 1.1), causticMaterial());
		this.#caustic.rotation.x = -Math.PI / 2;
		this.#caustic.rotation.z = -0.5;
		this.#caustic.position.set(0.5, FLOOR + 0.002, 0.45);
		this.scene.add(this.#caustic);

		this.camera.position.set(0, 1.05, CAMERA_DISTANCE);
		this.camera.lookAt(0, -0.12, 0);
		this.#reduced = options.reducedMotion;
		this.#load = (url) => {
			let pending = this.#textures.get(url);
			if (!pending) {
				pending = load(url);
				this.#textures.set(url, pending);
			}
			return pending;
		};
	}

	/** Glass refracts what is behind it; the canvas is see-through, so the
	 * refraction pass is shown the page's colour instead. */
	setBackdrop(colour: string) {
		(this.#backdrop.material as THREE.MeshBasicMaterial).color.set(colour);
	}

	resize(width: number, height: number) {
		if (!width || !height) return;
		this.renderer.setSize(width, height, false);
		this.camera.aspect = width / height;
		// Keep the object the same size in a narrow box as in a wide one.
		const distance = width < height ? CAMERA_DISTANCE * (height / width) * 0.82 : CAMERA_DISTANCE;
		this.camera.position.set(0, 1.05 * (distance / CAMERA_DISTANCE), distance);
		this.camera.lookAt(0, -0.12, 0);
		this.camera.updateProjectionMatrix();
	}

	setReducedMotion(reduced: boolean) {
		this.#reduced = reduced;
	}

	/** Show this object. The same object again keeps its motion going. */
	async show(o: StageObject) {
		const key = objectKey(o);
		this.#wanted = key;
		if (this.#live?.key === key) return;
		const built = await build(o, this.#load);
		if (this.#wanted !== key) {
			built.dispose();
			return;
		}
		if (this.#live) this.#leaving.push({ live: this.#live, t: 0 });
		this.#live = {
			key,
			built,
			phase: 'idle',
			bobPhase: Math.random() * TAU,
			bobRate: 1,
			bobAmp: 1,
			yaw: built.turns ? -0.6 : 0,
			turnSpeed: IDLE_TURN,
			swayPhase: 0,
			swayRate: 1,
			spinAngle: 0,
			spinSpeed: 0,
			alignTo: 0,
			frozenAt: 0,
			enter: 0
		};
		this.scene.add(built.group);
		if (this.#playing && built.spin) this.#play(this.#live, true);
	}

	setPlaying(playing: boolean) {
		if (playing === this.#playing) return;
		this.#playing = playing;
		const live = this.#live;
		if (!live?.built.spin) return;
		if (playing) this.#play(live, false);
		else {
			// Freeze where it is; idle comes back after RESUME_AFTER.
			live.phase = 'frozen';
			live.frozenAt = this.#clock.elapsedTime;
			live.bobRate = 0;
			live.turnSpeed = 0;
			live.spinSpeed = 0;
		}
	}

	#play(live: Live, instantly: boolean) {
		live.phase = 'align';
		// The next front-facing angle ahead, so it keeps turning the same way.
		live.alignTo = Math.ceil(live.yaw / TAU) * TAU;
		if (instantly) {
			live.yaw = live.alignTo;
			live.bobAmp = 0;
			live.phase = 'spinning';
		}
	}

	/** Advance and draw one frame. */
	frame() {
		const dt = Math.min(this.#clock.getDelta(), 0.1);
		const now = this.#clock.elapsedTime;
		const live = this.#live;
		// The light in the crystals plays on; reduced motion holds it.
		if (!this.#reduced) prismTime.value += dt;
		if (live) this.#move(live, dt, now);
		// Crystal is lit by the studio alone, as it was made to be.
		const crystal = !live || live.built.prism;
		this.#key.intensity = approach(this.#key.intensity, crystal ? 0 : KEY_LIGHT, 14, dt);
		this.scene.environmentIntensity = approach(this.scene.environmentIntensity, crystal ? 1 : KEY_ENVIRONMENT, 14, dt);
		for (const leaving of this.#leaving) {
			leaving.t += dt;
			const k = Math.min(1, leaving.t / 0.18);
			const g = leaving.live.built.group;
			g.scale.setScalar(1 - k * 0.6);
			g.rotation.y = leaving.live.yaw + k * 1.2;
			g.visible = k < 1;
		}
		this.#leaving = this.#leaving.filter((leaving) => {
			if (leaving.t < 0.18) return true;
			this.scene.remove(leaving.live.built.group);
			leaving.live.built.dispose();
			return false;
		});
		this.renderer.render(this.scene, this.camera);
	}

	#move(live: Live, dt: number, now: number) {
		const g = live.built.group;
		live.enter = Math.min(1, live.enter + dt / 0.22);
		const e = 1 - Math.pow(1 - live.enter, 3);
		g.scale.setScalar(0.55 + 0.45 * e);
		const shadow = this.#shadow.material as THREE.MeshBasicMaterial;
		if (this.#reduced) {
			g.position.y = 0;
			g.rotation.set(0, 0, 0);
			if (live.built.spin) live.built.spin.rotation.z = 0;
			shadow.opacity = live.built.shadow * e;
			this.#causticOpacity(live.built.prism ? 0.6 * e : 0);
			return;
		}

		const disc = !!live.built.spin;
		switch (live.phase) {
			case 'idle':
				live.bobRate = approach(live.bobRate, 1, 4, dt);
				live.bobAmp = approach(live.bobAmp, 1, 4, dt);
				live.turnSpeed = approach(live.turnSpeed, IDLE_TURN, 4, dt);
				live.swayRate = approach(live.swayRate, 1, 4, dt);
				break;
			case 'align':
				live.bobAmp = approach(live.bobAmp, 0, 6, dt);
				live.yaw = approach(live.yaw, live.alignTo, 6, dt);
				live.turnSpeed = 0;
				if (Math.abs(live.yaw - live.alignTo) < 0.01 && live.bobAmp < 0.02) {
					live.yaw = live.alignTo;
					live.phase = 'spinning';
				}
				break;
			case 'spinning':
				live.bobAmp = approach(live.bobAmp, 0, 6, dt);
				live.spinSpeed = approach(live.spinSpeed, PLAY_SPIN, 3, dt);
				break;
			case 'frozen':
				if (now - live.frozenAt >= RESUME_AFTER) live.phase = 'resuming';
				break;
			case 'resuming':
				live.bobRate = approach(live.bobRate, 1, 1.6, dt);
				live.turnSpeed = approach(live.turnSpeed, IDLE_TURN, 1.6, dt);
				live.swayRate = approach(live.swayRate, 1, 1.6, dt);
				live.bobAmp = approach(live.bobAmp, 1, 1.6, dt);
				if (live.turnSpeed > IDLE_TURN * 0.98) live.phase = 'idle';
				break;
		}

		live.bobPhase += dt * (TAU / BOB_PERIOD) * live.bobRate;
		const lift = Math.sin(live.bobPhase) * BOB_HEIGHT * live.bobAmp;
		g.position.y = lift;
		if (disc) {
			live.yaw += live.turnSpeed * dt;
			live.spinAngle -= live.spinSpeed * dt;
			g.rotation.set(0.1 * live.bobAmp, live.yaw, 0);
			live.built.spin!.rotation.z = live.spinAngle;
		} else {
			live.swayPhase += dt * (TAU / SWAY_PERIOD) * live.swayRate;
			live.yaw = Math.sin(live.swayPhase) * SWAY;
			g.rotation.set(0.08 + Math.sin(live.bobPhase * 0.5) * 0.03, live.yaw, 0);
		}
		// The shadow softens and spreads as the object rises.
		const height = (lift / BOB_HEIGHT + 1) / 2;
		shadow.opacity = live.built.shadow * e * (0.85 - height * 0.25);
		this.#shadow.scale.setScalar(0.92 + height * 0.12);
		// The rainbow sharpens as the glass comes down, and drifts as it turns.
		this.#causticOpacity(live.built.prism ? e * (0.72 - height * 0.22) : 0);
		this.#caustic.scale.setScalar(0.9 + height * 0.15);
		this.#caustic.position.x = 0.5 + Math.sin(live.yaw) * 0.18;
	}

	#causticOpacity(opacity: number) {
		(this.#caustic.material as THREE.ShaderMaterial).uniforms.uOpacity.value = opacity;
		// Nothing to draw, and no shader to run, under anything but glass.
		this.#caustic.visible = opacity > 0;
	}

	dispose() {
		this.#live?.built.dispose();
		for (const leaving of this.#leaving) leaving.live.built.dispose();
		for (const pending of this.#textures.values()) void pending.then((t) => t?.dispose());
		this.#shadow.geometry.dispose();
		(this.#shadow.material as THREE.Material).dispose();
		this.#shadowTexture.dispose();
		this.#caustic.geometry.dispose();
		(this.#caustic.material as THREE.Material).dispose();
		this.#key.dispose();
		this.#backdrop.geometry.dispose();
		(this.#backdrop.material as THREE.Material).dispose();
		this.#env.dispose();
		this.renderer.dispose();
	}
}
