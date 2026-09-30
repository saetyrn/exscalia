// The open note's save machine: the edit and acknowledgement counters, the
// autosave and retry timers, the crash draft, the three-way merge with what
// another device saved, and the recovery and conflict banners that ask when
// that merge cannot decide.
//
// It used to be closure state spread over a dozen functions of the note page,
// reachable only by hand in a browser, and the editor it drives was typed
// `any` — a renamed Editor export compiled cleanly and threw mid-save. Every
// side effect now comes in through `deps` (the API, the draft store, the
// editor, navigation, toasts, the clock and its timers), so
// notesession.test.ts scripts each outcome under fake timers.
//
// The page keeps what is not about saving: route prefetch and the load
// generation, side panels, autocorrect, proofread, tags, title editing.
import { ApiError, encodePath, type Note } from './api';
import type { LocalDraft } from './drafts';
import { alignManagedStamps, sameExceptManagedStamps } from './frontmatter';
import { mergeThreeWay } from './merge';
import { reconcileDraft } from './recovery';

export type SaveState = 'saved' | 'dirty' | 'saving' | 'error';

type Timer = ReturnType<typeof setTimeout>;

/** The part of Editor.svelte's exports the session drives. */
export interface SessionEditor {
	/** With a replacement held for an IME composition already in: the text
	 * the session's base and saves describe. */
	getDoc(): string;
	replaceDoc(
		doc: string,
		options?: { restoreFocus?: boolean; programmatic?: boolean; remote?: boolean }
	): void;
	hasFocus(): boolean;
	restoreFocusIfLost(shouldRestore?: boolean): void;
	adoptPath(newPath: string): void;
}

export interface NoteSessionDeps {
	/** The route's note path. Drafts and pulls are keyed by it, as they were
	 * on the page; during a rename it trails `note.path` until the URL moves. */
	path: () => string;
	api: {
		saveNote(
			path: string,
			content: string,
			baseRevision: string,
			onConflict: 'copy' | 'reject',
			options: { keepalive?: boolean }
		): Promise<Note>;
		readNote(path: string): Promise<Note>;
	};
	drafts: {
		writeDraft(draft: LocalDraft): Promise<void>;
		removeDraft(path: string, owner: string): Promise<void>;
		moveDraft(
			oldPath: string,
			newPath: string,
			options?: { owner?: string; dropBase?: boolean }
		): Promise<void>;
	};
	/** The mounted editor, if any. Its `onChange` must feed `onEdit`. */
	editor: () => SessionEditor | undefined;
	notify: (
		message: string,
		options?: {
			tone?: 'default' | 'success' | 'error';
			actionLabel?: string;
			action?: () => void;
			timeout?: number;
		}
	) => void;
	goto: (url: string) => Promise<unknown>;
	refreshNotes: () => Promise<unknown>;
	refreshRelated: () => void;
	/** Tell the sidebar its tag histogram may have moved. */
	announceSaved: () => void;
	/** This tab's id: the owner of the drafts it writes. */
	clientId: string;
	now: () => number;
	/** Wrap the globals (`(fn, ms) => setTimeout(fn, ms)`): a bare
	 * `window.setTimeout` called as a method of `deps` throws. */
	setTimeout: (callback: () => void, ms: number) => Timer;
	clearTimeout: (timer: Timer) => void;
}

const MAX_SAVE_RETRIES = 6;
// A note being hammered from another device must not spin here forever;
// after this many merge-and-retry rounds, fall back to a conflict copy.
const MAX_STALE_RETRIES = 3;

// A keepalive PUT survives the page being hidden and then killed, but
// browsers reject one whose body passes 64 KiB (Chrome throws before
// sending). Measured as sent: JSON-escaped, UTF-8, with room for the rest.
function fitsKeepalive(content: string): boolean {
	return (
		content.length < 60_000 && new TextEncoder().encode(JSON.stringify(content)).length < 60_000
	);
}

export function createNoteSession(deps: NoteSessionDeps) {
	let note: Note | null = $state(null);
	// What the editor holds. Reactive so the outline, word count and tags track
	// live edits; `note.content` is the server text instead.
	let doc = $state('');
	let saveState: SaveState = $state('saved');
	let recoveryPending = $state(false);
	// The other device's version, while its edits and ours overlap.
	let remoteConflict: Note | null = $state(null);

	let saveTimer: Timer | null = null;
	let retryTimer: Timer | null = null;
	let retryAttempts = 0;
	// The path a "no longer exists" save failure was last reported for, so
	// each further edit doesn't repeat it.
	let missingNotified: string | null = null;
	let staleRetries = 0;
	let serverRevision = '';
	let editVersion = 0;
	let acknowledgedVersion = 0;
	let savePromise: Promise<void> | null = null;
	// The edit version a flush() that wants everything saved, not just a
	// debounce tick, asked to reach; see the overlap exit in flush().
	let drainTo = 0;
	// Set once the layout tears the page down. beforeNavigate's flush() is not
	// awaited, so its PUT finishes inside the dead instance.
	let destroyed = false;
	let serverContentBeforeRecovery = '';
	// Whose draft this note was recovered from (drafts are per tab). Its row
	// goes once the recovery is settled — Discard, Take theirs, or the save
	// after Keep — or a closed tab's draft would come back on every open.
	let recoveredOwner: string | null = null;
	let pulling = false;

	// Draft writes were unbatched, so every keystroke ran a full IndexedDB
	// round trip. The draft only has to be current enough to survive a crash,
	// and it is flushed explicitly before saving, navigating, or unloading.
	let draftTimer: Timer | null = null;
	let pendingDraft: LocalDraft | null = null;

	/** Drop the open note before a load: its timers stop, and the editor
	 * unmounts until adoptLoaded() hands it the next text. */
	function reset() {
		if (saveTimer) {
			deps.clearTimeout(saveTimer);
			saveTimer = null;
		}
		if (retryTimer) {
			deps.clearTimeout(retryTimer);
			retryTimer = null;
		}
		retryAttempts = 0;
		note = null;
		saveState = 'saved';
		recoveredOwner = null;
	}

	/** Open `loaded`, with the draft this device left for it, if any. */
	async function adoptLoaded(loaded: Note, draft: LocalDraft | null) {
		serverRevision = loaded.revision;
		serverContentBeforeRecovery = loaded.content;
		editVersion = 0;
		acknowledgedVersion = 0;
		// The editor mounts with `doc`; `note.content` stays the server text
		// paired with serverRevision, the base every later merge starts from.
		const recovery = reconcileDraft(draft, loaded);
		if (recovery.kind === 'none') {
			doc = loaded.content;
			recoveryPending = false;
			note = loaded;
			if (draft) await deps.drafts.removeDraft(draft.path, draft.owner);
		} else {
			recoveredOwner = draft?.owner ?? null;
			doc = recovery.doc;
			editVersion = 1;
			saveState = 'dirty';
			if (recovery.kind === 'recover') {
				// On top of what just loaded (merged onto it, if the note moved
				// on since the draft): Keep fast-forwards it, Discard puts the
				// loaded text back.
				note = loaded;
				recoveryPending = true;
			} else {
				// The draft and the server rewrote the same lines, so the Take
				// theirs / Keep mine banner decides. Until then the base stays
				// the draft's own, and a draft written meanwhile recovers the
				// same conflict instead of fast-forwarding over theirs.
				note = { ...loaded, content: recovery.base.content };
				serverRevision = recovery.base.revision;
				recoveryPending = false;
				remoteConflict = loaded;
			}
		}
	}

	function keepDraft() {
		recoveryPending = false;
		saveState = 'dirty';
		scheduleSave(120);
	}

	async function discardDraft() {
		recoveryPending = false;
		doc = serverContentBeforeRecovery;
		deps.editor()?.replaceDoc(doc);
		editVersion += 1;
		acknowledgedVersion = editVersion;
		note = note ? { ...note, content: doc } : note;
		saveState = 'saved';
		await forgetDrafts(deps.path());
	}

	/** Drop this tab's draft and the one the note was recovered from: once the
	 * server copy is adopted or the draft discarded, neither holds anything to
	 * recover. The pending write goes too — armed, its timer would put the
	 * draft straight back. */
	async function forgetDrafts(target: string) {
		pendingDraft = null;
		if (draftTimer) {
			deps.clearTimeout(draftTimer);
			draftTimer = null;
		}
		const recovered = recoveredOwner;
		recoveredOwner = null;
		await deps.drafts.removeDraft(target, deps.clientId);
		if (recovered !== null && recovered !== deps.clientId)
			await deps.drafts.removeDraft(target, recovered);
	}

	/** Every change the editor reports. */
	function onEdit(newDoc: string, programmatic: boolean) {
		// A replaceDoc of the server's text: whoever called it has already
		// accounted for the revision and the save state. The editor marks the
		// transaction itself — an armed flag outlived replacements that never
		// dispatched (identical text, held for an IME) and swallowed the next
		// real edit, which then was never drafted or saved.
		if (programmatic) {
			doc = newDoc;
			return;
		}
		doc = newDoc;
		editVersion += 1;
		saveState = 'dirty';
		scheduleDraft({
			path: deps.path(),
			owner: deps.clientId,
			content: newDoc,
			baseRevision: serverRevision,
			baseContent: note?.content,
			updatedAt: deps.now()
		});
		if (!recoveryPending) scheduleSave(800);
	}

	// --- live sync with the same note open elsewhere -----------------------
	//
	// The other device's save used to be discovered only when this one tried to
	// save on top of it, and the server parked the loser in a `(conflict …).md`
	// note. Now every write is announced, and this side folds it in while the
	// two versions are still a few characters apart.

	/** Guarded pull, shared by the live stream and OMNIA's data-changed events
	 * so the two never race each other into a double merge. */
	async function pull() {
		if (pulling || recoveryPending || remoteConflict) return;
		pulling = true;
		try {
			await pullRemote();
		} catch {
			// Offline or mid-deploy; the next change (or a reload) recovers.
		} finally {
			pulling = false;
		}
	}

	/** Adopt the server's version wholesale — used when we have nothing local
	 * worth protecting, and when the user picks "Take theirs". */
	function adoptRemote(incoming: Note) {
		deps.editor()?.replaceDoc(incoming.content, { remote: true });
		doc = incoming.content;
		note = incoming;
		serverRevision = incoming.revision;
		editVersion += 1;
		acknowledgedVersion = editVersion;
		saveState = 'saved';
	}

	async function pullRemote() {
		if (!note) return;
		const incoming = await deps.api.readNote(deps.path());
		if (destroyed || incoming.revision === serverRevision) return;

		// `note.content` is what this device last had in sync with the server,
		// which makes it the common ancestor of both sides' edits.
		const base = note.content;
		const mine = deps.editor()?.getDoc() ?? doc;
		const dirty = acknowledgedVersion < editVersion || saveState === 'error';
		if (!dirty || mine === base) {
			adoptRemote(incoming);
			return;
		}

		const merged = mergeThreeWay(base, alignManagedStamps(mine, base), incoming.content);
		if (!merged.ok) {
			remoteConflict = incoming;
			return;
		}
		if (merged.text !== mine) {
			deps.editor()?.replaceDoc(merged.text, { remote: true });
			doc = merged.text;
		}
		// The base for the *next* merge is what the server now holds; our own
		// unsaved work rides on top and is pushed back by the save below.
		note = { ...note, content: incoming.content };
		serverRevision = incoming.revision;
		editVersion += 1;
		saveState = 'dirty';
		scheduleSave(300);
		deps.notify('Merged an outside edit into your draft.');
	}

	/** The editor dropped a remote replacement it held for an IME composition:
	 * what was composed meanwhile overlaps it. The base already moved to the
	 * text that replacement came from, so the next save would write the
	 * composition over it without asking. Ask, as an overlapping pull does. */
	function replacementConflict() {
		if (!note || remoteConflict || recoveryPending) return;
		remoteConflict = { ...note, revision: serverRevision };
		saveState = 'dirty';
	}

	function keepMine() {
		const incoming = remoteConflict;
		remoteConflict = null;
		if (!incoming || !note) return;
		// Re-base onto their revision so the save is accepted instead of being
		// turned into a conflict copy.
		note = { ...note, content: incoming.content };
		serverRevision = incoming.revision;
		editVersion += 1;
		saveState = 'dirty';
		scheduleSave(120);
	}

	function takeTheirs() {
		const incoming = remoteConflict;
		remoteConflict = null;
		if (!incoming) return;
		adoptRemote(incoming);
		void forgetDrafts(deps.path());
	}

	function scheduleDraft(draft: LocalDraft) {
		pendingDraft = draft;
		if (draftTimer) return;
		draftTimer = deps.setTimeout(flushDraft, 300);
	}

	function flushDraft() {
		if (draftTimer) {
			deps.clearTimeout(draftTimer);
			draftTimer = null;
		}
		if (pendingDraft) {
			void deps.drafts.writeDraft(pendingDraft);
			pendingDraft = null;
		}
	}

	function scheduleSave(delay: number) {
		if (remoteConflict) return;
		if (saveTimer) {
			deps.clearTimeout(saveTimer);
		}
		saveTimer = deps.setTimeout(() => void flush(false), delay);
	}

	/** Save the note. `drain` keeps going until everything typed before the
	 * call is saved, which the callers that act on the saved note next rely
	 * on; the autosave timers pass false. Typing that overlapped the PUT waits
	 * for the next debounce either way. */
	async function flush(drain = true): Promise<void> {
		// Order matters: the draft must land before the save that removes it.
		flushDraft();
		if (saveTimer) {
			deps.clearTimeout(saveTimer);
			saveTimer = null;
		}
		// An unresolved conflict must freeze saving. Without this the loop below
		// re-fired straight after showing the banner and wrote our version over
		// theirs — deciding the conflict for the user before they had answered.
		if (recoveryPending || remoteConflict || !note) return;
		if (acknowledgedVersion >= editVersion) return;
		// Up to the version current now, not until nothing is left: Mod-S
		// mid-sentence drained for as long as the user kept typing, one PUT
		// per round trip.
		if (drain) drainTo = Math.max(drainTo, editVersion);
		if (savePromise) return savePromise;
		savePromise = (async () => {
			while (note && acknowledgedVersion < editVersion && !recoveryPending && !remoteConflict) {
				const savingPath: string = note.path;
				const savingContent = doc;
				const savingVersion = editVersion;
				// This PUT carries the recovered draft, if there is one.
				const savingRecovered = recoveredOwner;
				const editorHadFocus = deps.editor()?.hasFocus() ?? false;
				saveState = 'saving';
				try {
					// Ask the server to report a newer version rather than parking
					// ours in a conflict copy: a save scheduled before a remote
					// change landed would otherwise create one for edits that
					// merge perfectly well.
					const result = await deps.api.saveNote(
						savingPath,
						savingContent,
						serverRevision,
						staleRetries < MAX_STALE_RETRIES ? 'reject' : 'copy',
						{ keepalive: fitsKeepalive(savingContent) }
					);
					if (note?.path !== savingPath) {
						if (result.status === 'saved') {
							await deps.drafts.removeDraft(savingPath, deps.clientId);
							if (savingRecovered !== null)
								await deps.drafts.removeDraft(savingPath, savingRecovered);
						}
						return;
					}
					if (result.status === 'stale' && note) {
						// Torn down during the PUT (the layout remounts the page on
						// every pathname change): no merge for a page nobody sees.
						// The draft flush() wrote first holds the text, and the
						// next open recovers it.
						if (destroyed) return;
						// The note moved under us mid-save. Fold their change into
						// ours and try again; only a genuine overlap stops here.
						staleRetries += 1;
						// Merge the LIVE document, not `savingContent`. The
						// pre-request snapshot lacked whatever was typed during
						// the PUT, so replaceDoc below deleted it from the editor;
						// and a pullRemote merge that landed mid-PUT was reverted,
						// then overwritten on the server by the retry.
						const mine: string = deps.editor()?.getDoc() ?? doc;
						const merged = mergeThreeWay(
							note.content,
							alignManagedStamps(mine, note.content),
							result.content
						);
						if (!merged.ok) {
							// Keep the base ours descends from, as pullRemote does:
							// a draft written under the banner must recover this
							// conflict, not fast-forward over theirs. Keep mine
							// re-bases onto `result` itself.
							remoteConflict = result;
							saveState = 'dirty';
							return;
						}
						note = { ...note, content: result.content };
						serverRevision = result.revision;
						if (merged.text !== mine) {
							deps.editor()?.replaceDoc(merged.text, { remote: true });
							doc = merged.text;
						}
						editVersion += 1;
						continue;
					}
					if (result.status === 'conflict_copy' && result.conflict_path) {
						const copyPath = result.conflict_path;
						// Only this tab's draft follows its text into the copy, and
						// without its base: that described the original. The copy
						// holds the recovered draft now, too.
						await deps.drafts.moveDraft(savingPath, copyPath, {
							owner: deps.clientId,
							dropBase: true
						});
						if (savingRecovered !== null && savingRecovered !== deps.clientId)
							await deps.drafts.removeDraft(savingPath, savingRecovered);
						await deps.refreshNotes();
						deps.notify(
							'Another device changed this note. Your version was saved as a conflict copy.',
							{
								tone: 'error',
								actionLabel: 'Open copy',
								action: () => void deps.goto(`/note/${encodePath(copyPath)}`),
								timeout: 9000
							}
						);
						// Not from a torn-down page: the user has moved on.
						if (!destroyed) await deps.goto(`/note/${encodePath(copyPath)}`);
						return;
					}
					serverRevision = result.revision;
					if (
						savingVersion === editVersion &&
						result.content !== doc &&
						// The server stamps `modified` on every save, so the
						// response always differs from what we sent. Rewriting the
						// document for that alone blurred the editor to <body> on
						// every autosave — losing focus, and losing any keystroke
						// that landed before the blur guard restored it.
						!sameExceptManagedStamps(result.content, doc)
					) {
						doc = result.content;
						deps.editor()?.replaceDoc(result.content, { restoreFocus: editorHadFocus });
					}
					note = {
						...note,
						// Always the server's text: it is the merge base paired
						// with serverRevision above. Advancing it only when nothing
						// was typed during the PUT left the base a save behind its
						// revision exactly when the user typed through a save, and
						// the next merge raised a 'pick one' conflict for two edits
						// that merge cleanly.
						content: result.content,
						meta: result.meta,
						revision: result.revision
					};
					acknowledgedVersion = Math.max(acknowledgedVersion, savingVersion);
					// The recovered draft is on the server now. This tab's own draft
					// is left to the check below: typing may have overlapped.
					if (savingRecovered !== null) {
						if (savingRecovered !== deps.clientId)
							void deps.drafts.removeDraft(savingPath, savingRecovered);
						if (recoveredOwner === savingRecovered) recoveredOwner = null;
					}
					// A successful PUT ends both streaks, even one typing overlapped.
					retryAttempts = 0;
					missingNotified = null;
					staleRetries = 0;
					if (acknowledgedVersion === editVersion) {
						saveState = 'saved';
						// The pending draft describes the version just saved. Left
						// armed, its timer wrote it back up to 300 ms after the
						// removeDraft below, and the next open offered already-saved
						// text as a recovered draft.
						pendingDraft = null;
						if (draftTimer) {
							deps.clearTimeout(draftTimer);
							draftTimer = null;
						}
						await deps.drafts.removeDraft(savingPath, deps.clientId);
						if (!destroyed) deps.refreshRelated();
						deps.announceSaved();
					}
					// Typing overlapped this PUT. Going round again here sent one
					// whole-note PUT per round trip for as long as the user kept
					// typing, worst on a slow link. Autosave hands the rest to the
					// debounce; only a merged stale retry (above) or a drain that
					// has not reached its version goes again at once. A torn-down
					// page has no next debounce, so it drains too.
					if (
						acknowledgedVersion < editVersion &&
						acknowledgedVersion >= drainTo &&
						!destroyed
					) {
						saveState = 'dirty';
						scheduleSave(800);
						return;
					}
				} catch (error) {
					// Moved while this save was in flight: the loop saves again
					// at the new path.
					if (note && note.path !== savingPath) continue;
					saveState = 'error';
					console.error('save failed', error);
					// Nobody to retry for or tell once the page is gone: the
					// draft written at the top of flush() covers the next open.
					if (destroyed) return;
					if (retryTimer) deps.clearTimeout(retryTimer);
					retryTimer = null;
					if (error instanceof ApiError && error.status === 404) {
						// Gone, not unreachable: retrying cannot help, and the
						// connection message would send the user chasing the
						// wrong problem. The draft keeps the text.
						if (missingNotified !== savingPath) {
							missingNotified = savingPath;
							deps.notify(
								'This note no longer exists at this path. Your text is kept as a draft.',
								{ tone: 'error' }
							);
						}
						return;
					}
					// Backed off and capped. This used to retry every 2.2s with
					// no limit and nothing cancelling it on unmount, so leaving
					// a note while the backend was down left a PUT firing for
					// that note forever — one leaked loop per note visited.
					retryAttempts += 1;
					if (retryAttempts <= MAX_SAVE_RETRIES) {
						const delay = Math.min(2200 * 2 ** (retryAttempts - 1), 30_000);
						retryTimer = deps.setTimeout(() => void flush(false), delay);
					} else {
						deps.notify('Could not save — check your connection, then edit to retry.', {
							tone: 'error'
						});
					}
					return;
				} finally {
					if (note?.path === savingPath) {
						deps.editor()?.restoreFocusIfLost(editorHadFocus);
					}
				}
			}
		})().finally(() => {
			savePromise = null;
			drainTo = 0;
			// Debounced, not immediate: `void flush()` here was the second way
			// PUTs chained back to back while the user kept typing.
			if (
				!destroyed &&
				!recoveryPending &&
				!remoteConflict &&
				acknowledgedVersion < editVersion &&
				saveState !== 'error'
			) {
				scheduleSave(800);
			}
		});
		return savePromise;
	}

	async function flushBeforeOmnia(): Promise<void> {
		// flush() saves up to the version current at the call; typing or a
		// merged pull during its PUT is the next save's, not a failure.
		const target = editVersion;
		await flush();
		if (saveState === 'error' || acknowledgedVersion < target) {
			throw new Error('Save this note successfully before sending it to OMNIA.');
		}
	}

	/** Pre-flight for the actions that change the open note on the server
	 * (task status, /task, Pin, + link). Their result comes back through
	 * pull(), which adopts it in place only when nothing local is pending;
	 * with a banner up or a save outstanding it would land on unsaved work. */
	async function settledForMutation(): Promise<boolean> {
		// Everything typed before the call, as flush() drains it. Typing after
		// it (a fast typist carries on past /task's Enter) is merged by pull()
		// like any other unsaved work; checking the live version failed the
		// action, and /task lost its text.
		const target = editVersion;
		await flush();
		if (
			recoveryPending ||
			remoteConflict ||
			saveState === 'error' ||
			acknowledgedVersion < target
		) {
			deps.notify('Save this note before changing it from here.', { tone: 'error' });
			return false;
		}
		return true;
	}

	/** A rename, done on the server: the same note under `target`. The path
	 * (and the realigned frontmatter, if the server rewrote it) are swapped
	 * into the live state; the editor stays mounted. */
	function rebind(
		sourcePath: string,
		target: string,
		result: { path: string; revision?: string; content?: string }
	) {
		if (!note || note.path !== sourcePath) return;
		note = { ...note, path: target };
		// Before the keying effect sees the new path — else it rebuilds the
		// EditorView and cursor, scroll and undo history go with it.
		deps.editor()?.adoptPath(target);
		// The revision moves only with the text it names. Unsaved work keeps
		// its old base, so its next save comes back stale and the merge folds
		// in whatever the move changed (a realigned title, rewritten links)
		// instead of passing the base check and writing over it.
		if (!result.revision || result.content === undefined || saveState !== 'saved') return;
		serverRevision = result.revision;
		note = { ...note, content: result.content };
		if (result.content !== doc) {
			doc = result.content;
			deps.editor()?.replaceDoc(result.content);
		}
	}

	/** beforeunload: true when the page should ask before closing. */
	function guardUnload(): boolean {
		if (saveState !== 'dirty' && saveState !== 'saving' && saveState !== 'error') return false;
		// 'saving' writes too. The draft flush() stored before its PUT
		// predates any merge that replaced the text since (stale retry,
		// pullRemote), and a draft whose PUT did land is not offered
		// back: load() compares it past the managed stamps.
		void deps.drafts.writeDraft({
			path: deps.path(),
			owner: deps.clientId,
			content: doc,
			baseRevision: serverRevision,
			baseContent: note?.content,
			updatedAt: deps.now()
		});
		return true;
	}

	/** The page was hidden. A backgrounded PWA that the OS later kills never
	 * sees beforeunload; hiding is the last moment that reliably runs.
	 * flush() lands the draft first, and its PUT is keepalive. */
	function suspend() {
		void flush();
	}

	/** The page is gone. The layout remounts it on every pathname change, and
	 * before this existed a save-retry loop outlived the route. */
	function dispose() {
		destroyed = true;
		flushDraft();
		if (saveTimer) deps.clearTimeout(saveTimer);
		if (retryTimer) deps.clearTimeout(retryTimer);
	}

	return {
		get note() {
			return note;
		},
		get doc() {
			return doc;
		},
		get saveState() {
			return saveState;
		},
		get recoveryPending() {
			return recoveryPending;
		},
		get remoteConflict() {
			return remoteConflict;
		},
		/** Bumped by every user edit, and by each merge or adoption. */
		get editVersion() {
			return editVersion;
		},
		/** The server revision the next save is based on. */
		get serverRevision() {
			return serverRevision;
		},
		reset,
		adoptLoaded,
		onEdit,
		flush,
		flushBeforeOmnia,
		settledForMutation,
		pull,
		replacementConflict,
		keepMine,
		takeTheirs,
		keepDraft,
		discardDraft,
		rebind,
		guardUnload,
		suspend,
		dispose
	};
}

export type NoteSession = ReturnType<typeof createNoteSession>;
