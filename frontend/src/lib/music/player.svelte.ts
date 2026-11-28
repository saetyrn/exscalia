// The music player. Since Deploy 3 the server holds what is playing: one
// session every device follows (backend/app/music_session.py, MUSIC.md 4).
// This tab plays sound only while it is the speaker; every other tab and
// device shows the same song and position and its buttons control the
// speaker. Pressing play here makes this tab the speaker.
//
// Every action changes state locally at once (no click waits on the
// network), then sends the whole new state with the version it came from.
// A refusal (someone else changed it first) hands back the current state,
// which wins. The <audio> element is owned by the player bar in the root
// layout, so playback survives every navigation.
import { isDesktop } from '$lib/runtime';
import { library, streamUrl } from './library.svelte';
import { refreshToken } from './links';
import { DEVICE_ID, SELF, deviceName } from './device';
import { sessionApi, StaleSession, type SessionState } from './api';
import { nextAfterEnd, nextByHand, previous, shuffleRest, type Repeat } from './queue';

const LOCAL_KEY = 'exscalia.music.local';
const HEARTBEAT_MS = 10_000;
const POLL_MS = 30_000;

type Shared = Pick<
	SessionState,
	| 'queue'
	| 'original'
	| 'index'
	| 'context'
	| 'shuffle'
	| 'repeat'
	| 'playing'
	| 'position'
	| 'seek'
	| 'speaker'
	| 'speaker_name'
>;

class Player {
	queue = $state<string[]>([]);
	original = $state<string[]>([]);
	index = $state(-1);
	context = $state<string | null>(null);
	shuffle = $state(false);
	repeat = $state<Repeat>('off');
	playing = $state(false);
	/** The position when it was last set (by a seek, a pause or a heartbeat). */
	position = $state(0);
	/** Local ms when `position` was true. */
	positionAt = $state(0);
	seek = $state(0);
	speaker = $state<string | null>(null);
	speakerName = $state<string | null>(null);
	version = $state(-1);

	// This device only.
	volume = $state(1);
	muted = $state(false);
	dismissed = $state(false);
	error = $state<string | null>(null);
	duration = $state(0);
	/** Ticks twice a second so a mirror's position counts on. */
	clock = $state(Date.now());

	current = $derived(this.index >= 0 ? (this.queue[this.index] ?? null) : null);
	isSpeaker = $derived(this.speaker === SELF);
	/** Playing somewhere else: this tab shows it and controls it. */
	remote = $derived(!!this.speaker && this.speaker !== SELF);
	shownPosition = $derived.by(() => {
		if (!this.playing || this.isSpeaker) return this.position;
		return this.position + Math.max(0, (this.clock - this.positionAt) / 1000);
	});

	#audio: HTMLAudioElement | null = null;
	#loaded: string | null = null;
	#appliedSeek = -1;
	#quiet = false;
	#inflight = 0;
	#chain: Promise<void> = Promise.resolve();
	#lastBeat = 0;
	#listen: { song: string; seconds: number; last: number; context: string | null; repeat: boolean } | null = null;
	#lastReported: string | null = null;

	attach(audio: HTMLAudioElement): () => void {
		this.#audio = audio;
		this.#loadLocal();
		audio.volume = this.volume;
		audio.muted = this.muted;
		const on = (target: EventTarget, name: string, fn: (e: Event) => void) => {
			target.addEventListener(name, fn);
			return () => target.removeEventListener(name, fn);
		};
		const offs = [
			on(audio, 'timeupdate', () => this.#tick()),
			on(audio, 'durationchange', () => {
				this.duration = Number.isFinite(audio.duration) ? audio.duration : 0;
			}),
			on(audio, 'loadedmetadata', () => this.#seekLoaded()),
			on(audio, 'play', () => {
				this.error = null;
			}),
			on(audio, 'pause', () => {
				// Paused by the system (headphones out, another app): tell everyone.
				if (this.#quiet || !this.isSpeaker || !this.playing || audio.ended) return;
				this.#commit({ playing: false, position: audio.currentTime });
			}),
			on(audio, 'ended', () => this.#ended()),
			on(audio, 'error', () => void this.#failed()),
			on(window, 'exscalia:live', (event) => {
				const detail = (event as CustomEvent<{ type: string; path?: string; revision?: number }>).detail;
				if (detail?.type === 'music' && detail.path === 'music/session' && (detail.revision ?? 0) > this.version)
					void this.sync();
			}),
			on(window, 'exscalia:data-changed', () => void this.sync()),
			on(document, 'visibilitychange', () => {
				if (document.visibilityState === 'visible') void this.sync();
			}),
			on(window, 'pagehide', () => this.#leaving())
		];
		const clock = setInterval(() => {
			this.clock = Date.now();
		}, 500);
		const poll = setInterval(() => {
			if (document.visibilityState === 'visible' && this.remote && this.playing) void this.sync();
		}, POLL_MS);
		this.#mediaSession();
		void refreshToken();
		void this.sync(true);
		return () => {
			for (const off of offs) off();
			clearInterval(clock);
			clearInterval(poll);
			this.#audio = null;
		};
	}

	// -- following the server ---------------------------------------------------

	async sync(first = false) {
		if (this.#inflight) return;
		try {
			const state = await sessionApi.get();
			if (this.#inflight) return;
			this.#adopt(state, first);
			if (first && this.isSpeaker && this.playing) {
				// A reload of the speaker tab: sound cannot restart without a
				// tap, so it comes back paused where it was.
				this.#commit({ playing: false, position: this.shownPosition });
			}
		} catch {
			/* offline: keep what we have */
		}
	}

	#adopt(state: SessionState, force = false) {
		if (!force && state.version <= this.version) return;
		// Making sound here right now? Then a pause from another device was
		// sent with that device's estimate of the position; this tab knows
		// the real one and corrects it below.
		const audio = this.#audio;
		const soundingHere = this.isSpeaker && !!audio && !audio.paused;
		const skew = Date.now() / 1000 - state.now;
		this.queue = state.queue;
		this.original = state.original;
		this.index = state.index;
		this.context = state.context;
		this.shuffle = state.shuffle;
		this.repeat = state.repeat;
		this.playing = state.playing;
		this.position = state.position;
		this.positionAt = (state.position_at + skew) * 1000;
		this.seek = state.seek;
		this.speaker = state.speaker;
		this.speakerName = state.speaker_name;
		this.version = state.version;
		if (this.current) void library.ensure().then(() => this.#updateMetadata());
		this.#applyAudio();
		if (soundingHere && audio && this.isSpeaker && !this.playing) {
			const exact = audio.currentTime;
			if (Math.abs(exact - this.position) > 0.2) this.#commit({ position: exact });
		}
	}

	/** Make this tab's sound match the shared state. */
	#applyAudio() {
		const audio = this.#audio;
		if (!audio) return;
		const id = this.current;
		if (!this.isSpeaker || !id) {
			this.#finishListen('stopped');
			this.#quietPause();
			return;
		}
		if (this.#loaded !== id) {
			this.#finishListen('skipped');
			this.#loaded = id;
			audio.src = streamUrl(id);
			this.duration = library.songs[id]?.duration ?? 0;
			this.#appliedSeek = -1;
		} else if (this.seek !== this.#appliedSeek && audio.readyState > 0) {
			audio.currentTime = this.position;
			this.#appliedSeek = this.seek;
		}
		if (this.playing && audio.paused) void this.#start();
		else if (!this.playing && !audio.paused) this.#quietPause();
	}

	#seekLoaded() {
		const audio = this.#audio;
		if (!audio || !this.isSpeaker) return;
		if (this.#appliedSeek !== this.seek || this.position > 0) {
			audio.currentTime = Math.min(this.position, audio.duration || this.position);
			this.#appliedSeek = this.seek;
		}
	}

	#quietPause() {
		const audio = this.#audio;
		if (!audio || audio.paused) return;
		this.#quiet = true;
		audio.pause();
		this.#quiet = false;
	}

	async #start() {
		try {
			await this.#audio?.play();
		} catch (err) {
			// NotAllowedError: no tap has started sound in this tab yet.
			if (err instanceof DOMException && err.name === 'NotAllowedError' && this.playing)
				this.#commit({ playing: false, position: this.#audio?.currentTime ?? this.position });
		}
	}

	async #failed() {
		if (!this.isSpeaker || !this.current) return;
		// The desktop app's signed link may have run out: renew it once and
		// pick up where it stopped.
		const at = this.#audio?.currentTime ?? 0;
		await refreshToken(true);
		if (this.#audio && this.current && this.#audio.error) {
			this.#loaded = null;
			this.position = at;
			this.#applyAudio();
			setTimeout(() => {
				if (this.#audio?.error) this.error = 'This song could not be played';
			}, 4000);
		}
	}

	// -- changing it ----------------------------------------------------------------

	#shared(): Shared {
		return {
			queue: this.queue,
			original: this.original,
			index: this.index,
			context: this.context,
			shuffle: this.shuffle,
			repeat: this.repeat,
			playing: this.playing,
			position: this.position,
			seek: this.seek,
			speaker: this.speaker,
			speaker_name: this.speakerName
		};
	}

	#commit(changes: Partial<Shared>) {
		// Before `playing` flips: a mirror's position counts on only while playing.
		const position = changes.position ?? this.shownPosition;
		if (changes.queue) this.queue = changes.queue;
		if (changes.original) this.original = changes.original;
		if (changes.index !== undefined) this.index = changes.index;
		if (changes.context !== undefined) this.context = changes.context;
		if (changes.shuffle !== undefined) this.shuffle = changes.shuffle;
		if (changes.repeat !== undefined) this.repeat = changes.repeat;
		if (changes.playing !== undefined) this.playing = changes.playing;
		if (changes.position !== undefined || changes.playing !== undefined) {
			this.position = position;
			this.positionAt = Date.now();
		}
		if (changes.seek !== undefined) this.seek = changes.seek;
		if (changes.speaker !== undefined) this.speaker = changes.speaker;
		if (changes.speaker_name !== undefined) this.speakerName = changes.speaker_name;
		this.#applyAudio();
		this.#updateMetadata();
		this.#inflight += 1;
		this.#chain = this.#chain.then(async () => {
			try {
				this.#adopt(await sessionApi.put(Math.max(0, this.version), this.#shared()), true);
			} catch (err) {
				if (err instanceof StaleSession) this.#adopt(err.session, true);
				else this.error = err instanceof Error ? err.message : String(err);
			} finally {
				this.#inflight -= 1;
			}
		});
	}

	#take(): Partial<Shared> {
		return { speaker: SELF, speaker_name: deviceName() };
	}

	/** Play `ids` from `start` on this device, the rest queued behind it. */
	playList(ids: string[], start: number, context: string | null) {
		const playable = ids.filter((id) => library.songs[id] && !library.songs[id].missing);
		if (!playable.length) return;
		const first = playable.includes(ids[start]) ? ids[start] : playable[0];
		const queue = this.shuffle ? [first, ...shuffleRest(playable.filter((id) => id !== first), -1)] : playable;
		this.dismissed = false;
		this.#commit({
			queue,
			original: playable,
			index: queue.indexOf(first),
			context,
			playing: true,
			position: 0,
			seek: this.seek + 1,
			...this.#take()
		});
	}

	/** Pause wherever it plays; play here, taking over from any other device. */
	toggle() {
		if (!this.current) return;
		if (this.playing) {
			this.#commit({
				playing: false,
				position: this.isSpeaker ? (this.#audio?.currentTime ?? this.position) : this.shownPosition
			});
		} else {
			this.dismissed = false;
			this.#commit({ playing: true, position: this.shownPosition, seek: this.seek + 1, ...this.#take() });
		}
	}

	next() {
		const index = nextByHand(this.index, this.queue.length);
		if (index !== null) this.#commit({ index, position: 0, seek: this.seek + 1 });
	}

	prev() {
		const step = previous(this.index, this.queue.length, this.shownPosition, this.repeat);
		this.#commit({ index: step.index, position: 0, seek: this.seek + 1 });
	}

	seekTo(seconds: number) {
		this.#commit({ position: Math.max(0, seconds), seek: this.seek + 1 });
	}

	/** Right after the current song. Starts playing when nothing is loaded. */
	playNext(ids: string[]) {
		const songs = ids.filter((id) => library.songs[id] && !library.songs[id].missing);
		if (!songs.length) return;
		if (!this.current) return this.playList(songs, 0, null);
		const queue = this.queue.filter((id, i) => i <= this.index || !songs.includes(id));
		queue.splice(this.index + 1, 0, ...songs);
		const original = [...this.original, ...songs.filter((id) => !this.original.includes(id))];
		this.#commit({ queue, original });
	}

	/** At the end of the queue. */
	addToQueue(ids: string[]) {
		const songs = ids.filter((id) => library.songs[id] && !library.songs[id].missing);
		if (!songs.length) return;
		if (!this.current) return this.playList(songs, 0, null);
		this.#commit({ queue: [...this.queue, ...songs], original: [...this.original, ...songs] });
	}

	toggleShuffle() {
		const current = this.current;
		if (!this.shuffle) {
			this.#commit({ shuffle: true, original: [...this.queue], queue: shuffleRest(this.queue, this.index) });
		} else {
			const queue = this.original.length ? [...this.original] : this.queue;
			this.#commit({ shuffle: false, queue, index: current ? Math.max(0, queue.indexOf(current)) : -1 });
		}
	}

	cycleRepeat() {
		this.#commit({ repeat: this.repeat === 'off' ? 'all' : this.repeat === 'all' ? 'one' : 'off' });
	}

	setVolume(value: number) {
		this.volume = Math.min(1, Math.max(0, value));
		this.muted = this.volume === 0;
		if (this.#audio) {
			this.#audio.volume = this.volume;
			this.#audio.muted = this.muted;
		}
		this.#saveLocal();
	}

	toggleMute() {
		this.muted = !this.muted;
		if (this.#audio) this.#audio.muted = this.muted;
		if (!this.muted && this.volume === 0) this.setVolume(0.5);
		this.#saveLocal();
	}

	dismiss() {
		if (this.playing) this.toggle();
		this.dismissed = true;
	}

	#ended() {
		if (!this.isSpeaker) return;
		this.#finishListen('finished');
		const before = this.current;
		if (this.repeat === 'one') {
			this.#commit({ position: 0, seek: this.seek + 1 });
		} else {
			const index = nextAfterEnd(this.index, this.queue.length, this.repeat);
			if (index === null) this.#commit({ playing: false, position: 0, seek: this.seek + 1 });
			else this.#commit({ index, position: 0, seek: this.seek + 1 });
		}
		// The same song again (repeat one, or a queue of one): it ended, so
		// play() starts it from the top.
		if (this.#audio && this.current === before && this.playing) {
			this.#audio.currentTime = 0;
			void this.#start();
		}
	}

	// -- the speaker's duties -------------------------------------------------------

	#tick() {
		const audio = this.#audio;
		if (!audio || !this.isSpeaker) return;
		this.position = audio.currentTime;
		this.positionAt = Date.now();
		// Listening time for the history: real play time, replayed bits
		// included, never a jump.
		const id = this.current;
		if (id && !audio.paused) {
			if (!this.#listen || this.#listen.song !== id) {
				this.#listen = {
					song: id,
					seconds: 0,
					last: audio.currentTime,
					context: this.context,
					repeat: this.#lastReported === id
				};
			}
			const delta = audio.currentTime - this.#listen.last;
			if (delta > 0 && delta < 2) this.#listen.seconds += delta;
			this.#listen.last = audio.currentTime;
		} else if (this.#listen) {
			this.#listen.last = audio.currentTime;
		}
		if (Date.now() - this.#lastBeat > HEARTBEAT_MS && this.playing) {
			this.#lastBeat = Date.now();
			void sessionApi
				.heartbeat({ speaker: SELF, position: audio.currentTime, playing: !audio.paused })
				.catch(() => {});
		}
	}

	#finishListen(ended: 'finished' | 'skipped' | 'stopped') {
		const listen = this.#listen;
		this.#listen = null;
		if (!listen || listen.seconds < 1) return;
		this.#lastReported = listen.song;
		void sessionApi
			.play({
				song: listen.song,
				device: DEVICE_ID,
				seconds: Math.round(listen.seconds * 10) / 10,
				duration: library.songs[listen.song]?.duration ?? null,
				ended,
				repeat: listen.repeat,
				context: listen.context
			})
			.catch(() => {});
	}

	#leaving() {
		if (!this.isSpeaker || !this.playing) return;
		this.#finishListen('stopped');
		// A beacon cannot carry the desktop app's Bearer token; there the
		// server pauses the silent speaker after its heartbeats stop.
		if (isDesktop) return;
		// The tab is going: tell the others it stopped, in a request that
		// outlives the page.
		const body = JSON.stringify({
			speaker: SELF,
			position: this.#audio?.currentTime ?? this.position,
			playing: false,
			final: true
		});
		try {
			navigator.sendBeacon?.('/api/music/session/heartbeat', new Blob([body], { type: 'application/json' }));
		} catch {
			/* best effort: the server pauses a silent speaker anyway */
		}
	}

	// -- this device ------------------------------------------------------------------

	#loadLocal() {
		try {
			const saved = JSON.parse(localStorage.getItem(LOCAL_KEY) ?? 'null');
			if (saved) {
				this.volume = typeof saved.volume === 'number' ? saved.volume : 1;
				this.muted = !!saved.muted;
			}
		} catch {
			/* defaults */
		}
	}

	#saveLocal() {
		try {
			localStorage.setItem(LOCAL_KEY, JSON.stringify({ volume: this.volume, muted: this.muted }));
		} catch {
			/* private mode */
		}
	}

	#updateMetadata() {
		if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
		const song = this.current ? library.songs[this.current] : undefined;
		if (!song || !this.isSpeaker) return;
		navigator.mediaSession.metadata = new MediaMetadata({
			title: song.title,
			artist: song.artist ?? '',
			album: song.album ?? '',
			artwork: song.cover ? [{ src: `/api/${song.cover}` }] : []
		});
	}

	#mediaSession() {
		if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
		const session = navigator.mediaSession;
		const set = (action: MediaSessionAction, fn: MediaSessionActionHandler) => {
			try {
				session.setActionHandler(action, fn);
			} catch {
				/* not supported here */
			}
		};
		set('play', () => !this.playing && this.toggle());
		set('pause', () => this.playing && this.toggle());
		set('nexttrack', () => this.next());
		set('previoustrack', () => this.prev());
		set('seekto', (details) => {
			if (details.seekTime !== undefined) this.seekTo(details.seekTime);
		});
	}
}

export const player = new Player();
