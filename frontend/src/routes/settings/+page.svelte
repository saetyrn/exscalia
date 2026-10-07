<script lang="ts">
	import { onDestroy, onMount, tick } from 'svelte';
	import { cubicOut } from 'svelte/easing';
	import { fade, fly } from '$lib/motion';
	import { goto } from '$app/navigation';
	import {
		api,
		encodePath,
		type AIConfig,
		type AIProvider,
		type AuthSession,
		type FailedLoginAttempt,
		type OmniaSettings,
		type UsageRow
	} from '$lib/api';
	import { parseTokenBudget } from '$lib/budget';
	import { recipesEffortTitle, recipesEffortValue } from '$lib/recipes/settings';
	import { isDesktop } from '$lib/runtime';
	import { disablePush, enablePush, pushState, type PushSnapshot } from '$lib/push';
	import { refreshNotes, refreshOverseerStatus, syncState, vault } from '$lib/stores.svelte';
	import type { ConflictRow, OpRow } from '$lib/sync/db';

	// Launch-at-startup is an OS launch agent on this machine — per device by
	// nature, never part of the synced account settings.
	let autostartOn = $state(false);
	let autostartBusy = $state(false);
	let autostartReady = $state(false);

	onMount(() => {
		if (!isDesktop) return;
		void import('@tauri-apps/plugin-autostart')
			.then(async (mod) => {
				autostartOn = await mod.isEnabled();
				autostartReady = true;
			})
			.catch(() => {
				// Plugin unavailable (old app build) — leave the toggle hidden.
			});
	});

	async function toggleAutostart() {
		if (autostartBusy || !autostartReady) return;
		autostartBusy = true;
		try {
			const mod = await import('@tauri-apps/plugin-autostart');
			if (autostartOn) {
				await mod.disable();
				autostartOn = false;
			} else {
				await mod.enable();
				autostartOn = await mod.isEnabled();
			}
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			autostartBusy = false;
		}
	}

	// Web Push is a per-device browser subscription, so it lives here like the
	// desktop "launch at startup" toggle — never part of synced account settings.
	let push = $state<PushSnapshot | null>(null);
	let pushBusy = $state(false);

	async function refreshPush() {
		push = await pushState();
	}

	onMount(() => {
		if (!isDesktop) void refreshPush();
	});

	async function togglePush() {
		if (pushBusy) return;
		pushBusy = true;
		try {
			if (push?.subscribed) {
				await disablePush();
				notify('Notifications turned off for this device.');
			} else {
				await enablePush();
				notify('Notifications on — OMNIA can now reach this device.', { tone: 'success' });
			}
			await refreshPush();
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			pushBusy = false;
		}
	}

	async function sendTestPush() {
		if (pushBusy) return;
		pushBusy = true;
		try {
			const { sent } = await api.testPush();
			notify(sent > 0 ? 'Test notification sent.' : 'No devices are subscribed yet.');
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			pushBusy = false;
		}
	}
	import { confirmDialog, notify, promptDialog } from '$lib/ui.svelte';
	import {
		THEME_COLOR_VARS,
		type AppearanceSettingsInput,
		type CustomTheme,
		type FontId,
		type SavedTheme,
		type ThemeId,
		type ThemeVar,
		ACTIVE_SAVED_THEME_KEY,
		applyCustomTheme,
		applyAppearanceSettings,
		clearCustomTheme,
		deriveTheme,
		loadLocalAppearance,
		loadCustomTheme,
		saveCustomTheme,
		saveThemeLibrary,
		themeFromCurrent
	} from '$lib/theme';

	const themes: {
		id: ThemeId;
		label: string;
		description: string;
		colors: [string, string, string, string];
	}[] = [
		{
			id: 'system',
			label: 'System',
			description: 'Follow this device',
			colors: ['#faf9f7', '#1e1e22', '#6c5ce7', '#8d7cf6']
		},
		{
			id: 'light',
			label: 'Light',
			description: 'Clean paper',
			colors: ['#faf9f7', '#f1efeb', '#24292f', '#6c5ce7']
		},
		{
			id: 'dark',
			label: 'Dark',
			description: 'Soft charcoal',
			colors: ['#1e1e22', '#26262c', '#e6e6ea', '#8d7cf6']
		},
		{
			id: 'black',
			label: 'True Black',
			description: 'Ink and violet',
			colors: ['#000000', '#0c0c0e', '#ececf0', '#9d8cff']
		},
		{
			id: 'beige',
			label: 'Beige',
			description: 'Warm notebook',
			colors: ['#f2e9d8', '#eaddc4', '#3b2f21', '#a05e2c']
		},
		{
			id: 'forest',
			label: 'Forest',
			description: 'Deep moss',
			colors: ['#0f1711', '#15211a', '#dfe8dc', '#6fbf73']
		}
	];

	let themeChoice = $state<ThemeId>('system');
	let custom = $state<CustomTheme | null>(null);
	let savedThemes: SavedTheme[] = $state([]);
	let savedThemeName = $state('');
	let activeSavedThemeId: string | null = $state(null);

	// Friendly labels for the fine-tune list.
	const varLabels: Record<ThemeVar, string> = {
		bg: 'Background',
		'bg-panel': 'Panels',
		'bg-hover': 'Hover',
		'bg-active': 'Selection',
		border: 'Borders',
		text: 'Text',
		'text-muted': 'Muted text',
		accent: 'Accent',
		'accent-soft': 'Accent wash',
		danger: 'Danger',
		added: 'Added (diffs)',
		removed: 'Removed (diffs)'
	};

	function setActiveSavedTheme(id: string | null) {
		activeSavedThemeId = id;
		try {
			if (id) localStorage.setItem(ACTIVE_SAVED_THEME_KEY, id);
			else localStorage.removeItem(ACTIVE_SAVED_THEME_KEY);
		} catch {
			// Selection still works for this page when storage is unavailable.
		}
	}

	function chooseTheme(theme: ThemeId) {
		setActiveSavedTheme(null);
		if (theme === 'custom') {
			// Seed from whatever is on screen so editing starts from a look
			// the user already likes, not from scratch.
			if (!custom) custom = loadCustomTheme() ?? themeFromCurrent();
			applyCustomTheme(custom);
			saveCustomTheme(custom);
		} else {
			clearCustomTheme();
		}
		themeChoice = theme;
		document.documentElement.dataset.theme = theme;
		try {
			localStorage.setItem('omnia.theme', theme);
		} catch {
			// The theme still applies for this page when storage is unavailable.
		}
		scheduleAppearanceSave();
	}

	const fonts: { id: FontId; label: string; stack: string }[] = [
		{ id: 'system', label: 'System', stack: 'var(--font-ui)' },
		{
			id: 'serif',
			label: 'Serif',
			stack: "'Iowan Old Style', 'Palatino Linotype', Palatino, Georgia, serif"
		},
		{
			id: 'rounded',
			label: 'Rounded',
			stack: "ui-rounded, 'SF Pro Rounded', -apple-system, sans-serif"
		},
		{ id: 'mono', label: 'Mono', stack: 'var(--font-mono)' }
	];

	let fontChoice = $state<FontId>('system');
	let intoxicateOn = $state(false);
	let textureOn = $state(false);
	let appearanceReady = false;
	let appearanceSaveTimer: ReturnType<typeof setTimeout> | null = null;
	let appearanceSaving = false;
	let appearanceQueued = false;
	let appearanceSyncError = $state<string | null>(null);

	function appearanceSnapshot(): AppearanceSettingsInput {
		return {
			theme: themeChoice,
			font: fontChoice,
			intoxicate: intoxicateOn,
			texture: textureOn,
			custom_theme: custom ? cloneTheme(custom) : null,
			saved_themes: savedThemes.map((saved) => ({
				id: saved.id,
				name: saved.name,
				theme: cloneTheme(saved.theme)
			})),
			active_saved_theme_id: activeSavedThemeId
		};
	}

	function hydrateAppearance(settings: AppearanceSettingsInput) {
		themeChoice = settings.theme;
		fontChoice = settings.font;
		intoxicateOn = settings.intoxicate;
		textureOn = settings.texture === true;
		custom = settings.custom_theme ? cloneTheme(settings.custom_theme) : null;
		savedThemes = settings.saved_themes.map((saved) => ({
			id: saved.id,
			name: saved.name,
			theme: cloneTheme(saved.theme)
		}));
		activeSavedThemeId = savedThemes.some(
			(saved) => saved.id === settings.active_saved_theme_id
		)
			? settings.active_saved_theme_id
			: null;
		const active = savedThemes.find((saved) => saved.id === activeSavedThemeId);
		savedThemeName = active?.name ?? '';
		applyAppearanceSettings(appearanceSnapshot());
	}

	async function loadSyncedAppearance() {
		const local = loadLocalAppearance();
		hydrateAppearance(local);
		try {
			const remote = await api.getAppearanceSettings();
			if (remote.configured) {
				hydrateAppearance(remote);
			} else {
				// First upgraded device seeds the shared profile with its
				// existing local themes instead of losing them to defaults.
				await api.saveAppearanceSettings(local);
			}
			appearanceSyncError = null;
		} catch (cause) {
			appearanceSyncError = (cause as Error).message;
		} finally {
			appearanceReady = true;
			if (appearanceQueued) scheduleAppearanceSave();
		}
	}

	function scheduleAppearanceSave() {
		appearanceQueued = true;
		if (!appearanceReady) return;
		if (appearanceSaveTimer) clearTimeout(appearanceSaveTimer);
		appearanceSaveTimer = setTimeout(() => {
			appearanceSaveTimer = null;
			void flushAppearance();
		}, 320);
	}

	async function flushAppearance() {
		if (!appearanceReady || appearanceSaving || !appearanceQueued) return;
		appearanceSaving = true;
		try {
			while (appearanceQueued) {
				appearanceQueued = false;
				const snapshot = appearanceSnapshot();
				applyAppearanceSettings(snapshot);
				try {
					await api.saveAppearanceSettings(snapshot);
					appearanceSyncError = null;
				} catch (cause) {
					appearanceSyncError = (cause as Error).message;
					break;
				}
			}
		} finally {
			appearanceSaving = false;
		}
	}

	onDestroy(() => {
		if (appearanceSaveTimer) clearTimeout(appearanceSaveTimer);
		if (appearanceQueued) void flushAppearance();
	});

	function chooseFont(font: FontId) {
		fontChoice = font;
		if (font === 'system') delete document.documentElement.dataset.font;
		else document.documentElement.dataset.font = font;
		try {
			localStorage.setItem('omnia.font', font);
		} catch {
			// Still applies for this page when storage is unavailable.
		}
		scheduleAppearanceSave();
	}

	function toggleTexture() {
		textureOn = !textureOn;
		if (textureOn) document.documentElement.dataset.texture = '1';
		else delete document.documentElement.dataset.texture;
		try {
			localStorage.setItem('omnia.texture', textureOn ? '1' : '0');
		} catch {
			// Still applies for this page when storage is unavailable.
		}
		scheduleAppearanceSave();
	}

	function toggleIntoxicate() {
		intoxicateOn = !intoxicateOn;
		if (intoxicateOn) document.documentElement.dataset.intoxicate = '1';
		else delete document.documentElement.dataset.intoxicate;
		try {
			localStorage.setItem('omnia.intoxicate', intoxicateOn ? '1' : '0');
		} catch {
			// Still applies for this page when storage is unavailable.
		}
		scheduleAppearanceSave();
	}

	function updateCustom(next: CustomTheme) {
		custom = next;
		setActiveSavedTheme(null);
		applyCustomTheme(next);
		saveCustomTheme(next);
		scheduleAppearanceSave();
	}

	function cloneTheme(theme: CustomTheme): CustomTheme {
		return { mode: theme.mode, colors: { ...theme.colors } };
	}

	function applySavedTheme(saved: SavedTheme) {
		const next = cloneTheme(saved.theme);
		custom = next;
		savedThemeName = saved.name;
		themeChoice = 'custom';
		document.documentElement.dataset.theme = 'custom';
		applyCustomTheme(next);
		saveCustomTheme(next);
		setActiveSavedTheme(saved.id);
		try {
			localStorage.setItem('omnia.theme', 'custom');
		} catch {
			// The theme still applies for this page when storage is unavailable.
		}
		scheduleAppearanceSave();
	}

	async function saveNamedTheme(event: SubmitEvent) {
		event.preventDefault();
		if (!custom) return;
		const name = savedThemeName.trim().slice(0, 40);
		if (!name) return;

		const existing = savedThemes.find((saved) => saved.name.toLowerCase() === name.toLowerCase());
		const theme = cloneTheme(custom);
		let id: string;
		if (existing) {
			if (
				!(await confirmDialog(`Replace the saved theme “${existing.name}” with the current colors?`, {
					title: 'Replace saved theme',
					confirmLabel: 'Replace'
				}))
			)
				return;
			id = existing.id;
			savedThemes = savedThemes.map((saved) =>
				saved.id === existing.id ? { ...saved, name, theme } : saved
			);
		} else {
			id =
				typeof crypto.randomUUID === 'function'
					? crypto.randomUUID()
					: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
			savedThemes = [...savedThemes, { id, name, theme }];
		}
		savedThemeName = name;
		saveThemeLibrary(savedThemes);
		setActiveSavedTheme(id);
		scheduleAppearanceSave();
	}

	async function deleteSavedTheme(saved: SavedTheme) {
		if (
			!(await confirmDialog(`Delete the saved theme “${saved.name}”?`, {
				title: 'Delete saved theme',
				confirmLabel: 'Delete',
				danger: true
			}))
		)
			return;
		savedThemes = savedThemes.filter((theme) => theme.id !== saved.id);
		saveThemeLibrary(savedThemes);
		if (activeSavedThemeId === saved.id) setActiveSavedTheme(null);
		scheduleAppearanceSave();
	}

	// Changing a base color rebuilds the dependent surface colors from it.
	function setBase(part: 'bg' | 'text' | 'accent', value: string) {
		if (!custom) return;
		const base = { ...custom.colors, [part]: value };
		updateCustom(deriveTheme(base.bg, base.text, base.accent, custom.mode));
	}

	function setMode(mode: 'light' | 'dark') {
		if (!custom) return;
		const { bg, text, accent } = custom.colors;
		updateCustom(deriveTheme(bg, text, accent, mode));
	}

	// Fine-tuning edits one variable without touching the rest.
	function setVar(name: ThemeVar, value: string) {
		if (!custom) return;
		updateCustom({ ...custom, colors: { ...custom.colors, [name]: value } });
	}

	function resetCustom() {
		if (!custom) return;
		const { bg, text, accent } = custom.colors;
		updateCustom(deriveTheme(bg, text, accent, custom.mode));
	}

	let appTimezone = $state('America/New_York');
	let savingTimezone = $state(false);

	async function loadGeneralSettings() {
		try {
			appTimezone = (await api.getGeneralSettings()).timezone;
		} catch {
			// Keep the documented default if an older backend is still loading.
		}
	}

	async function saveTimezone() {
		savingTimezone = true;
		try {
			appTimezone = (await api.saveGeneralSettings(appTimezone.trim())).timezone;
			notify('Timezone saved.', { tone: 'success' });
		} catch (cause) {
			notify((cause as Error).message, { tone: 'error' });
		} finally {
			savingTimezone = false;
		}
	}

	let trashCount = $state<number | null>(null);

	// Task lifecycle timeline (created / started / completed / deleted).
	let timeline = $state<{ at: string; kind: string; id: string; text: string }[]>([]);
	let timelineError = $state<string | null>(null);

	async function loadTimeline() {
		timelineError = null;
		try {
			timeline = await api.taskTimeline();
		} catch (e) {
			timelineError = (e as Error).message;
		}
	}

	const timelineGlyphs: Record<string, string> = {
		created: '+',
		started: '›',
		completed: '✓',
		deleted: '✕'
	};

	function timelineDate(at: string): string {
		const d = new Date(at);
		const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' };
		if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
		return d.toLocaleDateString(undefined, opts);
	}

	// Nightly conversation cleanup. Server-side and account-wide, unlike the
	// per-device autostart switch above.
	let housekeeping = $state(true);
	let housekeepingBusy = $state(false);
	let omniaSettings = $state<OmniaSettings | null>(null);
	// The switches below render only once real settings are in: their $state
	// defaults must never be shown as if they were the server's values.
	let omniaLoadError = $state<string | null>(null);
	let budgetInput = $state('');

	async function loadOmniaSettings() {
		omniaLoadError = null;
		try {
			applyOmniaSettings(await api.getOmniaSettings());
		} catch (e) {
			omniaLoadError = (e as Error).message;
		}
	}

	async function toggleHousekeeping() {
		if (housekeepingBusy) return;
		housekeepingBusy = true;
		const next = !housekeeping;
		try {
			const saved = await api.saveOmniaSettings({ housekeeping: next });
			applyOmniaSettings(saved);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			housekeepingBusy = false;
		}
	}

	let fullAuto = $state(false);
	let fullAutoBusy = $state(false);

	async function toggleFullAuto() {
		if (fullAutoBusy) return;
		fullAutoBusy = true;
		try {
			applyOmniaSettings(await api.saveOmniaSettings({ full_auto: !fullAuto }));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			fullAutoBusy = false;
		}
	}

	let autocorrect = $state(false);
	let autocorrectBusy = $state(false);

	async function toggleAutocorrect() {
		if (autocorrectBusy) return;
		autocorrectBusy = true;
		try {
			applyOmniaSettings(await api.saveOmniaSettings({ autocorrect: !autocorrect }));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		} finally {
			autocorrectBusy = false;
		}
	}

	function applyOmniaSettings(s: OmniaSettings) {
		omniaSettings = s;
		housekeeping = s.housekeeping;
		fullAuto = s.full_auto;
		autocorrect = s.autocorrect;
		budgetInput = s.monthly_token_budget > 0 ? String(s.monthly_token_budget) : '';
	}

	async function saveBudget() {
		const parsed = parseTokenBudget(budgetInput);
		if (parsed === null) {
			notify('Budget must be a number of tokens', { tone: 'error' });
			// Put the saved value back so the rejected text does not linger
			// looking as if it took.
			budgetInput = omniaSettings?.monthly_token_budget
				? String(omniaSettings.monthly_token_budget)
				: '';
			return;
		}
		if (parsed === (omniaSettings?.monthly_token_budget ?? 0)) return;
		try {
			applyOmniaSettings(await api.saveOmniaSettings({ monthly_token_budget: parsed }));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function saveBackgroundModel(model: string) {
		try {
			applyOmniaSettings(await api.saveOmniaSettings({ background_model: model }));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function saveMediaModel(model: string) {
		try {
			applyOmniaSettings(await api.saveOmniaSettings({ media_model: model }));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function saveAutocorrectModel(model: string) {
		try {
			applyOmniaSettings(await api.saveOmniaSettings({ autocorrect_model: model }));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	async function saveRecipesModel(model: string) {
		try {
			applyOmniaSettings(await api.saveOmniaSettings({ recipes_model: model }));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	function tokens(n: number | undefined): string {
		const v = n ?? 0;
		if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
		if (v >= 1_000) return `${Math.round(v / 1_000)}k`;
		return String(v);
	}

	// A caller's month, counted the way the budget counts (input + output +
	// cache writes).
	function callerTotal(row: UsageRow): number {
		return (row.input ?? 0) + (row.output ?? 0) + (row.cache_write ?? 0);
	}

	onMount(() => {
		void loadGeneralSettings();
		void api
			.getMediaSettings()
			.then((keys) => (mediaKeys = keys))
			.catch((e) => (mediaKeysError = (e as Error).message));
		void loadOmniaSettings();
		void loadSyncedAppearance();
		void loadTimeline();
		// The count is cosmetic: without it the Trash link just shows no number.
		void api
			.listTrash()
			.then((r) => (trashCount = r.items.length))
			.catch(() => {});
	});

	// The omnia/ and templates/ folders live here instead of the sidebar tree.
	let memoryNotes = $derived(vault.notes.filter((n) => n.path.startsWith('omnia/')));
	let templateNotes = $derived(vault.notes.filter((n) => n.path.startsWith('templates/')));

	async function newTemplate() {
		const name = (
			await promptDialog('Choose a template name:', {
				title: 'New template',
				placeholder: 'Meeting notes',
				confirmLabel: 'Create'
			})
		)?.trim();
		if (!name) return;
		const path = `templates/${name.replace(/\.md$/, '')}.md`;
		try {
			await api.createNote(path, '');
			await refreshNotes();
			await goto(`/note/${encodePath(path)}`);
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}

	let semantic = $state(false);
	let semanticError: string | null = $state(null);
	let authRequired = $state(false);
	let signedInSessions: AuthSession[] = $state([]);
	let failedLoginAttempts: FailedLoginAttempt[] = $state([]);
	let sessionsLoading = $state(false);
	let sessionsError: string | null = $state(null);
	let reindexing = $state(false);
	let reindexResult: string | null = $state(null);

	let backup: Awaited<ReturnType<typeof api.backupStatus>> | null = $state(null);
	let pushing = $state(false);
	let pushResult: string | null = $state(null);

	async function pushBackup() {
		pushing = true;
		pushResult = null;
		try {
			const r = await api.backupPush();
			pushResult = r.pushed
				? `Pushed ${r.commit?.slice(0, 8)}.`
				: (r.error ?? r.reason ?? 'Nothing to push.');
			backup = await api.backupStatus();
		} catch (e) {
			pushResult = (e as Error).message;
		} finally {
			pushing = false;
		}
	}

	let aiConfig: AIConfig | null = $state(null);
	let providers: AIProvider[] = $state([]);
	let providerChoice = $state('anthropic');
	let keyInput = $state('');
	// Set only by typing or pasting, so a password manager filling the key
	// field never turns into the stored provider key.
	let keyTouched = $state(false);
	let modelChoice = $state('');
	let reasoningChoice = $state('default');
	const REASONING_OPTIONS = [
		{ id: 'default', label: 'Default', blurb: "The model's own balance of speed and depth." },
		{ id: 'low', label: 'Low', blurb: 'Quick answers, minimal deliberation.' },
		{ id: 'medium', label: 'Medium', blurb: 'Some thinking on harder questions.' },
		{ id: 'high', label: 'High', blurb: 'Thorough reasoning; slower and costlier.' },
		{ id: 'max', label: 'Max', blurb: 'Deepest reasoning the model offers.' }
	];
	let savingAI = $state(false);
	let aiResult: string | null = $state(null);
	let aiError: string | null = $state(null);
	let aiLoadError: string | null = $state(null);

	let activeProvider = $derived(providers.find((p) => p.id === providerChoice));
	let keyStatus = $derived.by(() => {
		const cfg: AIConfig | null = aiConfig;
		return cfg?.keys?.[providerChoice] ?? null;
	});

	$effect(() => {
		// Status lines only: a failed fetch leaves them at their defaults.
		api
			.semanticStatus()
			.then((s) => {
				semantic = s.available;
				semanticError = s.error;
			})
			.catch(() => {});
		api
			.authStatus()
			.then((s) => {
				authRequired = s.auth_required;
				if (s.auth_required) void loadAuthSessions();
			})
			.catch(() => {});
		api
			.backupStatus()
			.then((b) => (backup = b))
			.catch(() => {});
		void loadAISection();
	});

	async function loadAISection() {
		aiLoadError = null;
		try {
			// Providers first, so the stored model can be validated against the list.
			providers = (await api.overseerModels()).providers;
			await loadAIConfig();
		} catch (e) {
			aiLoadError = (e as Error).message;
		}
	}

	function validModel(providerId: string, model: string | undefined | null): string {
		const p = providers.find((x) => x.id === providerId);
		if (!p) return model ?? '';
		// Fall back to the provider default when the remembered model is no
		// longer in the list (e.g. it was retired upstream).
		return model && p.models.some((m) => m.id === model) ? model : p.default_model;
	}

	async function loadAIConfig() {
		aiConfig = await api.getAIConfig();
		providerChoice = aiConfig.provider;
		modelChoice = validModel(providerChoice, aiConfig.model);
		reasoningChoice = aiConfig.reasoning ?? 'default';
	}

	// Media-lookup keys (TMDB / RAWG); books use Open Library and need none.
	type MediaKeyStatus = { set: boolean; hint: string | null };
	type MediaKeyName = 'tmdb' | 'rawg' | 'igdb_id' | 'igdb_secret' | 'brave';
	let mediaKeys: Record<MediaKeyName, MediaKeyStatus> | null = $state(null);
	let tmdbInput = $state('');
	let rawgInput = $state('');
	let igdbIdInput = $state('');
	let igdbSecretInput = $state('');
	let braveInput = $state('');
	// Same gate as keyTouched, per field; only read on submit.
	const mediaKeysTouched = new Set<MediaKeyName>();
	let savingMediaKeys = $state(false);
	let mediaKeysResult = $state<string | null>(null);
	let mediaKeysError = $state<string | null>(null);

	async function saveMediaKeys(e: Event) {
		e.preventDefault();
		savingMediaKeys = true;
		mediaKeysResult = null;
		mediaKeysError = null;
		try {
			const body: Partial<Record<MediaKeyName, string>> = {};
			let filled = false;
			const put = (name: MediaKeyName, value: string) => {
				if (!value.trim()) return;
				if (mediaKeysTouched.has(name)) body[name] = value.trim();
				else filled = true;
			};
			put('tmdb', tmdbInput);
			put('rawg', rawgInput);
			put('igdb_id', igdbIdInput);
			put('igdb_secret', igdbSecretInput);
			put('brave', braveInput);
			// An autofilled key is never sent, so saving the rest would report
			// success and clear a field whose key was not stored.
			if (filled) {
				mediaKeysError = FILLED_KEY_ERROR;
				return;
			}
			mediaKeys = await api.saveMediaSettings(body);
			tmdbInput = '';
			rawgInput = '';
			igdbIdInput = '';
			igdbSecretInput = '';
			braveInput = '';
			mediaKeysTouched.clear();
			mediaKeysResult = 'Saved.';
		} catch (err) {
			mediaKeysError = (err as Error).message;
		} finally {
			savingMediaKeys = false;
		}
	}

	async function clearMediaKey(name: MediaKeyName) {
		mediaKeysError = null;
		try {
			// IGDB credentials are a pair; clearing one clears both.
			const body =
				name === 'igdb_id' || name === 'igdb_secret'
					? { igdb_id: '', igdb_secret: '' }
					: { [name]: '' };
			mediaKeys = await api.saveMediaSettings(body);
		} catch (err) {
			mediaKeysError = (err as Error).message;
		}
	}

	// A keystroke or paste by the person at the keyboard. Password-manager
	// extensions fill with scripted (untrusted) events, and the browser's own
	// autofill fires a plain Event rather than an InputEvent.
	function typedByUser(e: Event): boolean {
		return e.isTrusted && e instanceof InputEvent;
	}
	const FILLED_KEY_ERROR = 'Type or paste the key — autofilled keys are not saved.';

	function pickProvider(id: string) {
		// Re-picking the active provider must not wipe a key being typed.
		if (id === providerChoice) return;
		providerChoice = id;
		keyInput = '';
		keyTouched = false;
		// Each provider remembers its own model choice.
		modelChoice = validModel(id, aiConfig?.models?.[id]);
	}

	async function saveAI(e: Event) {
		e.preventDefault();
		aiResult = null;
		aiError = null;
		// Same as the media keys: never report a save for a key left out.
		if (keyInput.trim() && !keyTouched) {
			aiError = FILLED_KEY_ERROR;
			return;
		}
		savingAI = true;
		try {
			const body: { provider: string; api_key?: string; model?: string; reasoning?: string } = {
				provider: providerChoice,
				model: modelChoice,
				reasoning: reasoningChoice
			};
			if (keyTouched && keyInput.trim()) body.api_key = keyInput.trim();
			aiConfig = await api.saveAIConfig(body);
			keyInput = '';
			keyTouched = false;
			providerChoice = aiConfig.provider;
			modelChoice = aiConfig.model;
			reasoningChoice = aiConfig.reasoning ?? 'default';
			await refreshOverseerStatus();
			// The OMNIA section's model list follows the active provider —
			// switching providers here would otherwise leave it showing the
			// previous provider's models until a reload.
			applyOmniaSettings(await api.getOmniaSettings());
			aiResult = aiConfig.available
				? `Saved — OMNIA is on (${activeProvider?.label ?? aiConfig.provider}, ${aiConfig.model}).`
				: `Saved. Add a ${activeProvider?.label ?? ''} API key to turn OMNIA on.`;
		} catch (err) {
			aiError = (err as Error).message;
		} finally {
			savingAI = false;
		}
	}

	async function clearKey() {
		if (
			!(await confirmDialog(`Remove the stored ${activeProvider?.label ?? ''} API key?`, {
				title: 'Remove API key',
				confirmLabel: 'Remove',
				danger: true
			}))
		)
			return;
		savingAI = true;
		aiError = null;
		try {
			aiConfig = await api.saveAIConfig({ provider: providerChoice, clear_key: true });
			await refreshOverseerStatus();
			aiResult = 'Key removed.';
		} catch (err) {
			aiError = (err as Error).message;
		} finally {
			savingAI = false;
		}
	}

	async function reindex() {
		reindexing = true;
		reindexResult = null;
		try {
			const r = await api.rebuildIndex();
			semantic = r.semantic;
			semanticError = r.semantic_error;
			reindexResult =
				!r.semantic && r.semantic_error
					? `Reindexed ${r.notes_indexed} notes. Semantic index failed: ${r.semantic_error}`
					: `Reindexed ${r.notes_indexed} notes${r.semantic ? `, ${r.chunks} semantic chunks` : ''}${r.semantic_failed ? `, ${r.semantic_failed} failed` : ''}.`;
		} catch (e) {
			reindexResult = `Failed: ${(e as Error).message}`;
		} finally {
			reindexing = false;
		}
	}

	async function logout() {
		await api.logout();
		// Desktop: the session is revoked server-side; drop the keychain token
		// too so the reload lands on the login screen instead of a dead token.
		const { isDesktop, setToken } = await import('$lib/runtime');
		if (isDesktop) await setToken(null);
		location.reload();
	}

	async function loadAuthSessions() {
		sessionsLoading = true;
		sessionsError = null;
		try {
			const [sessions, failures] = await Promise.all([api.authSessions(), api.failedLogins()]);
			signedInSessions = sessions.sessions;
			failedLoginAttempts = failures.attempts;
		} catch (e) {
			sessionsError = (e as Error).message;
		} finally {
			sessionsLoading = false;
		}
	}

	async function clearFailedLoginHistory() {
		if (
			!(await confirmDialog('Clear the failed login history?', {
				title: 'Clear login history',
				confirmLabel: 'Clear',
				danger: true
			}))
		)
			return;
		try {
			await api.clearFailedLogins();
			failedLoginAttempts = [];
		} catch (e) {
			sessionsError = (e as Error).message;
		}
	}

	async function revokeSession(session: AuthSession) {
		if (
			!session.current &&
			!(await confirmDialog(`Sign out ${session.browser} on ${session.os}?`, {
				title: 'Sign out device',
				confirmLabel: 'Sign out',
				danger: true
			}))
		)
			return;
		try {
			const result = await api.revokeAuthSession(session.id);
			if (result.current) {
				location.reload();
				return;
			}
			await loadAuthSessions();
		} catch (e) {
			sessionsError = (e as Error).message;
		}
	}

	function sessionTime(value: string): string {
		const date = new Date(value);
		return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
	}

	// One section visible at a time, remembered in the URL hash so reload and
	// back/forward restore it. Everything still loads up front — the sections
	// only control what is rendered, never what is fetched.
	const SETTINGS_SECTIONS = [
		{ id: 'general', label: 'General' },
		{ id: 'appearance', label: 'Appearance' },
		{ id: 'omnia', label: 'OMNIA' },
		{ id: 'media', label: 'Media' },
		{ id: 'notifications', label: 'Notifications' },
		{ id: 'vault', label: 'Vault' },
		{ id: 'devices', label: 'Devices & security' }
	] as const;
	type SectionId = (typeof SETTINGS_SECTIONS)[number]['id'];

	let section = $state<SectionId>('general');

	// Notifications are web-only and the devices list needs auth — hide the
	// entries that can never show anything here.
	let visibleSections = $derived(
		SETTINGS_SECTIONS.filter((s) =>
			s.id === 'notifications' ? !isDesktop : s.id === 'devices' ? authRequired : true
		)
	);

	function sectionFromHash(raw: string): SectionId {
		// The command palette still deep-links to #intoxicate (a toggle inside
		// Appearance) — keep that working.
		const id = raw === 'intoxicate' || raw === 'texture' ? 'appearance' : raw;
		return SETTINGS_SECTIONS.some((s) => s.id === id) ? (id as SectionId) : 'general';
	}

	function applyHash() {
		const raw = location.hash.replace(/^#/, '');
		section = sectionFromHash(raw);
		if (raw === 'intoxicate' || raw === 'texture') {
			void tick().then(() =>
				document.getElementById(raw)?.scrollIntoView({ block: 'center' })
			);
		}
	}

	function openSection(id: SectionId) {
		if (location.hash.replace(/^#/, '') === id) return;
		location.hash = id;
	}

	onMount(() => {
		applyHash();
		window.addEventListener('hashchange', applyHash);
		return () => window.removeEventListener('hashchange', applyHash);
	});

	// Desktop sync, read from this device's cache: what has not reached the
	// server yet, why, and the conflict copies replays have parked.
	let syncPending = $state<{ path: string; updated_at: number | null }[]>([]);
	let syncOps = $state<OpRow[]>([]);
	let syncConflicts = $state<ConflictRow[]>([]);
	let syncListError = $state('');
	/** False when the app runs without its cache; syncState.lastError says why. */
	let syncCache = $state(false);

	async function loadSyncStatus() {
		const db = await import('$lib/sync/db');
		syncCache = db.isOpen();
		if (!syncCache) return;
		try {
			const [rows, ops, conflicts] = await Promise.all([
				db.pendingNotes(),
				db.listOps(),
				db.listConflicts()
			]);
			syncPending = rows.map(({ path, updated_at }) => ({ path, updated_at }));
			syncOps = ops;
			syncConflicts = conflicts;
			syncListError = '';
		} catch (e) {
			syncListError = e instanceof Error ? e.message : String(e);
		}
	}

	// Re-read on open and whenever the engine's picture changes: a pass
	// starting or ending, the pending count moving, a new failure.
	$effect(() => {
		if (!isDesktop || section !== 'general') return;
		void [syncState.mode, syncState.pending, syncState.lastError];
		void loadSyncStatus();
	});

	async function retrySync() {
		const engine = await import('$lib/sync/engine');
		engine.requestReplay();
		await loadSyncStatus();
	}

	function syncWhen(ms: number | null): string {
		if (!ms) return '';
		const d = new Date(ms);
		return d.toDateString() === new Date().toDateString()
			? d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
			: timelineDate(d.toISOString());
	}

	async function saveRoleReasoning(
		role: 'background' | 'media' | 'autocorrect' | 'recipes',
		level: string
	) {
		try {
			const body =
				role === 'background'
					? { background_reasoning: level }
					: role === 'media'
						? { media_reasoning: level }
						: role === 'recipes'
							? { recipes_reasoning: level }
							: { autocorrect_reasoning: level };
			applyOmniaSettings(await api.saveOmniaSettings(body));
		} catch (e) {
			notify((e as Error).message, { tone: 'error' });
		}
	}
</script>

<div class="settings">
	<h1>Settings</h1>

	<div class="settings-layout">
	<nav class="section-nav" aria-label="Settings sections">
		{#each visibleSections as s (s.id)}
			<button
				type="button"
				class:active={section === s.id}
				aria-current={section === s.id ? 'true' : undefined}
				onclick={() => openSection(s.id)}
			>
				{s.label}
			</button>
		{/each}
	</nav>

	<div class="bento">
	{#if section === 'general'}
	<section class="span-3">
		<h2>General</h2>
		<p class="muted">
			Calendar activity, daily OMNIA threads, and morning greetings use this timezone.
			Timestamps are still stored safely in UTC.
		</p>
		<div class="timezone-row">
			<label>
				<span>Timezone</span>
				<input bind:value={appTimezone} list="timezone-options" />
			</label>
			<datalist id="timezone-options">
				<option value="America/New_York"></option>
				<option value="America/Chicago"></option>
				<option value="America/Denver"></option>
				<option value="America/Los_Angeles"></option>
				<option value="Europe/London"></option>
				<option value="Europe/Paris"></option>
				<option value="Asia/Tokyo"></option>
				<option value="Australia/Sydney"></option>
			</datalist>
			<button class="primary" onclick={saveTimezone} disabled={savingTimezone || !appTimezone.trim()}>
				{savingTimezone ? 'Saving…' : 'Save timezone'}
			</button>
		</div>
	</section>
	{/if}

	{#if section === 'notifications' && !isDesktop && push}
		<section class="span-6">
			<h2>Notifications</h2>
			{#if !push.supported}
				<p class="muted">This browser can't show notifications.</p>
			{:else if !push.standalone}
				<p class="muted">
					Add EXSCALIA to your Home Screen, then open it from there to turn on notifications.
				</p>
			{:else if push.permission === 'denied'}
				<p class="muted">
					Notifications are blocked for EXSCALIA — allow them in your device settings, then reload.
				</p>
			{:else}
				<button
					type="button"
					class="device-toggle"
					class:active={push.subscribed}
					role="switch"
					aria-checked={push.subscribed}
					disabled={pushBusy}
					onclick={togglePush}
				>
					<span class="device-toggle-copy">
						<strong>Notify me on this device</strong>
						<small>OMNIA sends reminders and nudges here even when the app is closed</small>
					</span>
					<span class="toggle-track" aria-hidden="true"><span></span></span>
				</button>
				{#if push.subscribed}
					<button type="button" class="push-test" disabled={pushBusy} onclick={sendTestPush}>
						Send test notification
					</button>
				{/if}
			{/if}
		</section>
	{/if}

	{#if section === 'vault'}
	<section class="span-3">
		<h2>OMNIA history</h2>
		<div class="note-links">
			<a class="note-link" href="/history/omnia">What OMNIA has done</a>
		</div>
	</section>

	<section class="span-3">
		<h2>Trash</h2>
		<div class="note-links">
			<a class="note-link" href="/trash">
				Open Trash{trashCount ? ` (${trashCount})` : ''}
			</a>
		</div>
	</section>
	{/if}

	{#if section === 'general' && isDesktop}
		<section class="span-3">
			<h2>This device</h2>
			{#if autostartReady}
				<button
					type="button"
					class="device-toggle"
					class:active={autostartOn}
					role="switch"
					aria-checked={autostartOn}
					disabled={autostartBusy}
					onclick={toggleAutostart}
				>
					<span class="device-toggle-copy">
						<strong>Launch at startup</strong>
						<small>Open EXSCALIA when you log in to this computer. Applies only to this device.</small>
					</span>
					<span class="toggle-track" aria-hidden="true"><span></span></span>
				</button>
			{/if}
			<p class="muted">Build {__BUILD_SHA__}</p>
		</section>

		<section class="span-3">
			<h2>Sync</h2>
			{#if syncState.lastError}<p class="ai-error">{syncState.lastError}</p>{/if}
			{#if syncCache}
				{#if syncListError}
					<p class="ai-error">{syncListError}</p>
				{:else if syncPending.length === 0 && syncOps.length === 0}
					<p class="muted">Nothing waiting to sync</p>
				{/if}
				{#if syncPending.length > 0}
					<h3 class="vault-h3">Notes</h3>
					<ul class="task-timeline">
						{#each syncPending as row (row.path)}
							<li>
								<span class="tl-when">{syncWhen(row.updated_at)}</span>
								<span class="tl-text" title={row.path}>{row.path}</span>
							</li>
						{/each}
					</ul>
				{/if}
				{#if syncOps.length > 0}
					<h3 class="vault-h3">Queued</h3>
					<ul class="task-timeline">
						{#each syncOps as op (op.id)}
							{@const target = `${op.path}${op.new_path ? ` → ${op.new_path}` : ''}`}
							<li>
								<span class="tl-when">{syncWhen(op.created_at)}</span>
								<span class="sync-item">
									<span class="tl-text" title={target}>{op.op.replace('_', ' ')} {target}</span>
									{#if op.last_error}
										<span class="ai-error">
											{op.attempts > 0 ? `${op.attempts}× ` : ''}{op.last_error}
										</span>
									{/if}
								</span>
							</li>
						{/each}
					</ul>
				{/if}
				{#if syncConflicts.length > 0}
					<h3 class="vault-h3">Conflicts</h3>
					<ul class="task-timeline">
						{#each syncConflicts as conflict (conflict.id)}
							<li>
								<span class="tl-when">{syncWhen(conflict.created_at)}</span>
								<span class="tl-text" title={conflict.path}>{conflict.path}</span>
								<a
									class="note-link sync-copy"
									href="/note/{encodePath(conflict.conflict_path)}"
									title={conflict.conflict_path}
								>
									Open copy
								</a>
							</li>
						{/each}
					</ul>
				{/if}
				<div class="session-actions">
					<button onclick={retrySync} disabled={syncState.mode === 'replaying'}>
						{syncState.mode === 'replaying' ? 'Syncing…' : 'Retry now'}
					</button>
				</div>
			{/if}
		</section>
	{/if}

	{#if section === 'appearance'}
	<section id="appearance" class="span-6">
		<h2>Appearance</h2>
		<p class="muted">
			Choose a palette. Your appearance and saved themes follow you across signed-in devices.
			{#if appearanceSyncError}
				<span class="sync-error"> Sync paused: {appearanceSyncError}</span>
			{/if}
		</p>
		<div class="theme-grid" aria-label="Color theme">
			{#each themes as theme (theme.id)}
				<button
				type="button"
				class="theme-card"
				class:selected={themeChoice === theme.id}
				aria-pressed={themeChoice === theme.id}
				onclick={() => chooseTheme(theme.id)}
				>
					<span
						class="theme-preview"
						style={`--swatch-1: ${theme.colors[0]}; --swatch-2: ${theme.colors[1]}; --swatch-3: ${theme.colors[2]}; --swatch-4: ${theme.colors[3]}`}
						aria-hidden="true"
					>
						<span></span><span></span><span></span><span></span>
					</span>
					<span class="theme-copy">
						<strong>{theme.label}</strong>
						<small>{theme.description}</small>
					</span>
					<span class="theme-check" aria-hidden="true">✓</span>
				</button>
			{/each}
			<button
				type="button"
				class="theme-card"
				class:selected={themeChoice === 'custom' && activeSavedThemeId === null}
				aria-pressed={themeChoice === 'custom' && activeSavedThemeId === null}
				onclick={() => chooseTheme('custom')}
			>
				<span
					class="theme-preview"
					style={custom
						? `--swatch-1: ${custom.colors.bg}; --swatch-2: ${custom.colors['bg-panel']}; --swatch-3: ${custom.colors.text}; --swatch-4: ${custom.colors.accent}`
						: '--swatch-1: var(--bg); --swatch-2: var(--bg-panel); --swatch-3: var(--text); --swatch-4: var(--accent)'}
					aria-hidden="true"
				>
					<span></span><span></span><span></span><span></span>
				</span>
				<span class="theme-copy">
					<strong>Custom</strong>
					<small>Make it yours</small>
				</span>
				<span class="theme-check" aria-hidden="true">✓</span>
			</button>
		</div>

		{#if themeChoice === 'custom' && custom}
			<div class="custom-editor" transition:fly={{ y: -6, duration: 180, easing: cubicOut }}>
				<div class="custom-row">
					<span class="custom-label">Base</span>
					<div class="mode-row" role="radiogroup" aria-label="Light or dark base">
						<button
							type="button"
							class:selected={custom.mode === 'light'}
							role="radio"
							aria-checked={custom.mode === 'light'}
							onclick={() => setMode('light')}>Light</button
						>
						<button
							type="button"
							class:selected={custom.mode === 'dark'}
							role="radio"
							aria-checked={custom.mode === 'dark'}
							onclick={() => setMode('dark')}>Dark</button
						>
					</div>
				</div>
				<div class="custom-row">
					<span class="custom-label">Colors</span>
					<div class="base-pickers">
						{#each [['bg', 'Background'], ['text', 'Text'], ['accent', 'Accent']] as [part, label] (part)}
							<label class="color-pick">
								<input
									type="color"
									value={custom.colors[part as ThemeVar]}
									oninput={(e) => setBase(part as 'bg' | 'text' | 'accent', e.currentTarget.value)}
								/>
								<span>{label}</span>
							</label>
						{/each}
					</div>
				</div>
				<p class="muted small">
					Pick three colors — panels, borders, hover, and selection are generated to match.
				</p>
				<details class="fine-tune">
					<summary>Fine-tune every color</summary>
					<div class="var-grid">
						{#each THEME_COLOR_VARS as v (v)}
							<label class="color-pick">
								<input
									type="color"
									value={custom.colors[v]}
									oninput={(e) => setVar(v, e.currentTarget.value)}
								/>
								<span>{varLabels[v]}</span>
							</label>
						{/each}
					</div>
					<button type="button" class="reset-btn" onclick={resetCustom}>
						Regenerate from base colors
					</button>
				</details>
				<form class="save-theme-form" onsubmit={saveNamedTheme}>
					<label>
						<span>Theme name</span>
						<input
							type="text"
							bind:value={savedThemeName}
							maxlength="40"
							placeholder="Midnight violet"
							autocomplete="off"
							required
						/>
					</label>
					<button type="submit" class="primary">Save theme</button>
				</form>
			</div>
		{/if}

		{#if savedThemes.length > 0}
			<div class="saved-library" transition:fade={{ duration: 150 }}>
				<h3>Saved themes</h3>
				<div class="saved-theme-list">
					{#each savedThemes as saved (saved.id)}
						<div class="saved-theme-item" class:active={activeSavedThemeId === saved.id}>
							<button
								type="button"
								class="saved-theme-apply"
								aria-pressed={activeSavedThemeId === saved.id}
								onclick={() => applySavedTheme(saved)}
							>
								<span
									class="theme-preview"
									style={`--swatch-1: ${saved.theme.colors.bg}; --swatch-2: ${saved.theme.colors['bg-panel']}; --swatch-3: ${saved.theme.colors.text}; --swatch-4: ${saved.theme.colors.accent}`}
									aria-hidden="true"
								>
									<span></span><span></span><span></span><span></span>
								</span>
								<span class="theme-copy">
									<strong>{saved.name}</strong>
									<small>{saved.theme.mode === 'dark' ? 'Dark palette' : 'Light palette'}</small>
								</span>
							</button>
							<button
								type="button"
								class="saved-theme-delete"
								aria-label={`Delete ${saved.name}`}
								title={`Delete ${saved.name}`}
								onclick={() => deleteSavedTheme(saved)}
							>×</button>
						</div>
					{/each}
				</div>
			</div>
		{/if}

		<div class="font-row" role="radiogroup" aria-label="Font">
			{#each fonts as f (f.id)}
				<button
					type="button"
					class="font-card"
					class:selected={fontChoice === f.id}
					aria-pressed={fontChoice === f.id}
					style:font-family={f.stack}
					onclick={() => chooseFont(f.id)}
				>
					<span class="font-sample">Aa</span>
					<span class="font-name">{f.label}</span>
				</button>
			{/each}
		</div>

		<button
			id="intoxicate"
			type="button"
			class="intoxicate-toggle"
			class:active={intoxicateOn}
			role="switch"
			aria-checked={intoxicateOn}
			onclick={toggleIntoxicate}
		>
			<span class="intoxicate-copy">
				<strong>Intoxicate</strong>
				<small>Merge every surface and remove dividers for one uninterrupted canvas</small>
			</span>
			<span class="toggle-track" aria-hidden="true"><span></span></span>
		</button>

		<button
			id="texture"
			type="button"
			class="intoxicate-toggle"
			class:active={textureOn}
			role="switch"
			aria-checked={textureOn}
			title="Scanlines and grain on dark themes"
			onclick={toggleTexture}
		>
			<span class="intoxicate-copy">
				<strong>Texture</strong>
			</span>
			<span class="toggle-track" aria-hidden="true"><span></span></span>
		</button>
	</section>

	{/if}

	{#if section === 'omnia'}
	<section class="span-6">
		<h2>OMNIA (AI overseer)</h2>
		{#if aiConfig}
			<p>
				{#if aiConfig.available}
					<span class="on">● On</span> —
					{providers.find((p) => p.id === aiConfig?.provider)?.label ?? aiConfig.provider}
					/ <code>{aiConfig.model}</code>
				{:else}
					<span class="off">● Off</span> — add an API key to enable OMNIA. The notebook is
					fully usable without it.
				{/if}
			</p>

			<form class="ai-form" onsubmit={saveAI}>
				<div class="field">
					<span id="ai-provider-label">Provider</span>
					<div class="provider-row" role="radiogroup" aria-labelledby="ai-provider-label">
						{#each providers as p (p.id)}
							<button
								type="button"
								class="provider-btn"
								class:selected={providerChoice === p.id}
								role="radio"
								aria-checked={providerChoice === p.id}
								onclick={() => pickProvider(p.id)}
							>
								{p.label}
								{#if aiConfig.keys[p.id]?.set}<span class="keyed">●</span>{/if}
							</button>
						{/each}
					</div>
				</div>

				<label>
					<span>API key</span>
					<input
						type="text"
						class="secret"
						bind:value={keyInput}
						oninput={(e) => typedByUser(e) && (keyTouched = true)}
						autocomplete="off"
						spellcheck="false"
						autocapitalize="off"
						autocorrect="off"
						data-1p-ignore
						data-lpignore="true"
						data-bwignore
						placeholder={keyStatus?.set
							? `Key configured (${keyStatus.hint}) — enter a new key to replace`
							: (activeProvider?.key_placeholder ?? '')}
					/>
					{#if keyStatus?.source === 'env'}
						<small>
							Currently using the key from the server environment. Entering one here
							overrides it.
						</small>
					{:else if !keyStatus?.set}
						<small>
							{{
								anthropic: 'Get one at console.anthropic.com.',
								openai: 'Get one at platform.openai.com.',
								google: 'Get one at aistudio.google.com/apikey.'
							}[providerChoice] ?? ''}
							Stored server-side in <code>data/ai.json</code> — never in the vault repo.
						</small>
					{/if}
				</label>

				<label>
					<span>Model</span>
					<select bind:value={modelChoice}>
						{#each activeProvider?.models ?? [] as m (m.id)}
							<option value={m.id}>{m.label}</option>
						{/each}
					</select>
					{#if activeProvider}
						<small>
							{activeProvider.models.find((m) => m.id === modelChoice)?.blurb ?? ''}
						</small>
					{/if}
				</label>

				<label>
					<span>Reasoning</span>
					<select bind:value={reasoningChoice}>
						{#each REASONING_OPTIONS as r (r.id)}
							<option value={r.id}>{r.label}</option>
						{/each}
					</select>
					<small>
						{REASONING_OPTIONS.find((r) => r.id === reasoningChoice)?.blurb ?? ''}
					</small>
				</label>

				<div class="ai-actions">
					<button class="primary" type="submit" disabled={savingAI}>
						{savingAI ? 'Saving…' : 'Save'}
					</button>
					{#if keyStatus?.source === 'ui'}
						<button type="button" onclick={clearKey} disabled={savingAI}>Remove key</button>
					{/if}
				</div>
				{#if aiResult}<p class="result" transition:fade={{ duration: 150 }}>{aiResult}</p>{/if}
				{#if aiError}
					<p class="ai-error" transition:fly={{ y: -4, duration: 160, easing: cubicOut }}>
						{aiError}
					</p>
				{/if}
			</form>
		{:else if aiLoadError}
			<p class="ai-error load-error">
				{aiLoadError}
				<button type="button" onclick={() => void loadAISection()}>Retry</button>
			</p>
		{/if}
		{#if omniaSettings}
			<h3 class="vault-h3">Usage</h3>
			{#if omniaSettings.usage && omniaSettings.usage.total > 0}
				<p class="usage-line">
					{omniaSettings.usage.month} — {tokens(omniaSettings.usage.total)} tokens
					{#if omniaSettings.monthly_token_budget > 0}
						of {tokens(omniaSettings.monthly_token_budget)}
					{/if}
					<span class="usage-callers">
						{#each Object.entries(omniaSettings.usage.by_caller) as [caller, row] (caller)}
							<span title="{(row.input ?? 0).toLocaleString()} in / {(row.output ?? 0).toLocaleString()} out / {row.calls ?? 0} calls">
								{caller} {tokens(callerTotal(row))}
							</span>
						{/each}
					</span>
				</p>
			{:else}
				<p class="usage-line">No usage recorded this month.</p>
			{/if}
			<div class="usage-controls">
				<label class="usage-field">
					<span>Monthly budget</span>
					<input
						type="text"
						inputmode="numeric"
						placeholder="No limit"
						bind:value={budgetInput}
						onblur={saveBudget}
						onkeydown={(e) => e.key === 'Enter' && (e.currentTarget as HTMLInputElement).blur()}
					/>
					<small>
						Input + output tokens per month. Past it, unattended work (nightly
						cleanup) pauses; chat always answers.
					</small>
				</label>
				{#if omniaSettings.models.length > 0}
					<div class="usage-role">
					<label class="usage-field">
						<span>Background model</span>
						<select
							value={omniaSettings.background_model}
							onchange={(e) => saveBackgroundModel((e.currentTarget as HTMLSelectElement).value)}
						>
							{#each omniaSettings.models as m (m.id)}
								<option value={m.id}>
									{m.label}{m.id === omniaSettings.chat_model ? ' (chat model)' : ''}
								</option>
							{/each}
						</select>
						<small>Runs the nightly cleanup and other unattended jobs.</small>
					</label>
					<label class="usage-field usage-effort">
						<span>Effort</span>
						<select
							value={omniaSettings.reasoning_levels?.background ?? 'inherit'}
							onchange={(e) =>
								saveRoleReasoning('background', (e.currentTarget as HTMLSelectElement).value)}
						>
							<option value="inherit">Same as chat</option>
							{#each REASONING_OPTIONS as r (r.id)}
								<option value={r.id}>{r.label}</option>
							{/each}
						</select>
					</label>
					</div>
					<div class="usage-role">
					<label class="usage-field">
						<span>Media model</span>
						<select
							value={omniaSettings.media_model}
							onchange={(e) => saveMediaModel((e.currentTarget as HTMLSelectElement).value)}
						>
							{#each omniaSettings.models as m (m.id)}
								<option value={m.id}>
									{m.label}{m.id === omniaSettings.chat_model ? ' (chat model)' : ''}
								</option>
							{/each}
						</select>
						<small>
							Art-directs media pages: looks at each item's artwork and picks its
							backdrop and color palette.
						</small>
					</label>
					<label class="usage-field usage-effort">
						<span>Effort</span>
						<select
							value={omniaSettings.reasoning_levels?.media ?? 'inherit'}
							onchange={(e) =>
								saveRoleReasoning('media', (e.currentTarget as HTMLSelectElement).value)}
						>
							<option value="inherit">Same as chat</option>
							{#each REASONING_OPTIONS as r (r.id)}
								<option value={r.id}>{r.label}</option>
							{/each}
						</select>
					</label>
					</div>
					<div class="usage-role">
					<label class="usage-field">
						<span>Recipes model</span>
						<select
							value={omniaSettings.recipes_model}
							onchange={(e) => saveRecipesModel((e.currentTarget as HTMLSelectElement).value)}
						>
							{#each omniaSettings.models as m (m.id)}
								<option value={m.id}>
									{m.label}{m.id === omniaSettings.chat_model ? ' (chat model)' : ''}
								</option>
							{/each}
						</select>
					</label>
					<label class="usage-field usage-effort">
						<span>Effort</span>
						<!-- Unset is recipe work's own default, not the chat knob. -->
						<select
							value={recipesEffortValue(omniaSettings)}
							title={recipesEffortTitle(omniaSettings) || undefined}
							onchange={(e) =>
								saveRoleReasoning('recipes', (e.currentTarget as HTMLSelectElement).value)}
						>
							<option value="inherit">Auto</option>
							{#each REASONING_OPTIONS as r (r.id)}
								<option value={r.id}>{r.label}</option>
							{/each}
						</select>
					</label>
					</div>
					<div class="usage-role">
					<label class="usage-field">
						<span>Autocorrect model</span>
						<select
							value={omniaSettings.autocorrect_model}
							onchange={(e) => saveAutocorrectModel((e.currentTarget as HTMLSelectElement).value)}
						>
							{#each omniaSettings.models as m (m.id)}
								<option value={m.id}>
									{m.label}{m.id === omniaSettings.chat_model ? ' (chat model)' : ''}
								</option>
							{/each}
						</select>
						<small>Scans the open note for typos while you write.</small>
					</label>
					<label class="usage-field usage-effort">
						<span>Effort</span>
						<select
							value={omniaSettings.reasoning_levels?.autocorrect ?? 'inherit'}
							onchange={(e) =>
								saveRoleReasoning('autocorrect', (e.currentTarget as HTMLSelectElement).value)}
						>
							<option value="inherit">Same as chat</option>
							{#each REASONING_OPTIONS as r (r.id)}
								<option value={r.id}>{r.label}</option>
							{/each}
						</select>
					</label>
					</div>
				{/if}
			</div>
			<h3 class="vault-h3">Autonomy</h3>
			<button
				type="button"
				class="device-toggle"
				class:active={fullAuto}
				role="switch"
				aria-checked={fullAuto}
				disabled={fullAutoBusy}
				onclick={toggleFullAuto}
			>
				<span class="device-toggle-copy">
					<strong>Full auto</strong>
					<small>
						OMNIA applies its changes without asking first. Every change still lands
						as an undoable action and a vault commit, so anything can be rolled back.
					</small>
				</span>
				<span class="toggle-track" aria-hidden="true"><span></span></span>
			</button>
			<h3 class="vault-h3">Autocorrect</h3>
			<button
				type="button"
				class="device-toggle"
				class:active={autocorrect}
				role="switch"
				aria-checked={autocorrect}
				disabled={autocorrectBusy}
				onclick={toggleAutocorrect}
			>
				<span class="device-toggle-copy">
					<strong>Fix typos while you write</strong>
					<small>
						While a note is open, OMNIA periodically scans it and fixes unambiguous
						typos in place. Each fix flashes briefly and is a single undo away.
					</small>
				</span>
				<span class="toggle-track" aria-hidden="true"><span></span></span>
			</button>
			<h3 class="vault-h3">Nightly cleanup</h3>
			<button
				type="button"
				class="device-toggle"
				class:active={housekeeping}
				role="switch"
				aria-checked={housekeeping}
				disabled={housekeepingBusy}
				onclick={toggleHousekeeping}
			>
				<span class="device-toggle-copy">
					<strong>Tidy conversations overnight</strong>
					<small>
						Just after midnight OMNIA deletes conversations where nothing happened and
						rewrites the rest's preview line to say what they were about. Conversations
						where it changed something are never touched, and deletions stay recoverable
						in the vault's history.
					</small>
				</span>
				<span class="toggle-track" aria-hidden="true"><span></span></span>
			</button>
		{:else if omniaLoadError}
			<p class="ai-error load-error">
				{omniaLoadError}
				<button type="button" onclick={() => void loadOmniaSettings()}>Retry</button>
			</p>
		{/if}
		{#if memoryNotes.length > 0}
			<h3 class="vault-h3">Memory</h3>
			<div class="note-links">
				{#each memoryNotes as n (n.path)}
					<a class="note-link" href="/note/{encodePath(n.path)}">{n.name.replace(/\.md$/, '')}</a>
				{/each}
			</div>
		{/if}
	</section>

	{/if}

	{#if section === 'media'}
	<section class="span-6">
		<h2>Media lookup</h2>
		<p>
			Metadata for the Media tab. Books use Open Library and need no key; movies and TV use
			TMDB. Games try IGDB, then RAWG, then Steam — Steam needs no key, so game lookups work
			with nothing configured. All keys are free and stored server-side in
			<code>data/media.json</code>, never in the vault repo.
		</p>
		<form class="ai-form" onsubmit={saveMediaKeys}>
			<label>
				<span>TMDB API key</span>
				<input
					type="text"
					class="secret"
					bind:value={tmdbInput}
					oninput={(e) => typedByUser(e) && mediaKeysTouched.add('tmdb')}
					autocomplete="off"
					spellcheck="false"
					autocapitalize="off"
					autocorrect="off"
					data-1p-ignore
					data-lpignore="true"
					data-bwignore
					placeholder={mediaKeys?.tmdb.set
						? `Key configured (${mediaKeys.tmdb.hint}) — enter a new key to replace`
						: 'From themoviedb.org (Settings, API)'}
				/>
			</label>
			<label>
				<span>RAWG API key</span>
				<input
					type="text"
					class="secret"
					bind:value={rawgInput}
					oninput={(e) => typedByUser(e) && mediaKeysTouched.add('rawg')}
					autocomplete="off"
					spellcheck="false"
					autocapitalize="off"
					autocorrect="off"
					data-1p-ignore
					data-lpignore="true"
					data-bwignore
					placeholder={mediaKeys?.rawg.set
						? `Key configured (${mediaKeys.rawg.hint}) — enter a new key to replace`
						: 'From rawg.io/apidocs (optional)'}
				/>
			</label>
			<label>
				<span>IGDB Client ID</span>
				<input
					type="text"
					class="secret"
					bind:value={igdbIdInput}
					oninput={(e) => typedByUser(e) && mediaKeysTouched.add('igdb_id')}
					autocomplete="off"
					spellcheck="false"
					autocapitalize="off"
					autocorrect="off"
					data-1p-ignore
					data-lpignore="true"
					data-bwignore
					placeholder={mediaKeys?.igdb_id.set
						? `Configured (${mediaKeys.igdb_id.hint}) — enter a new id to replace`
						: 'Twitch developer application (dev.twitch.tv)'}
				/>
			</label>
			<label>
				<span>IGDB Client Secret</span>
				<input
					type="text"
					class="secret"
					bind:value={igdbSecretInput}
					oninput={(e) => typedByUser(e) && mediaKeysTouched.add('igdb_secret')}
					autocomplete="off"
					spellcheck="false"
					autocapitalize="off"
					autocorrect="off"
					data-1p-ignore
					data-lpignore="true"
					data-bwignore
					placeholder={mediaKeys?.igdb_secret.set
						? `Configured (${mediaKeys.igdb_secret.hint}) — enter a new secret to replace`
						: 'Paired with the Client ID above'}
				/>
			</label>
			<label>
				<span>Brave Search API key</span>
				<input
					type="text"
					class="secret"
					bind:value={braveInput}
					oninput={(e) => typedByUser(e) && mediaKeysTouched.add('brave')}
					autocomplete="off"
					spellcheck="false"
					autocapitalize="off"
					autocorrect="off"
					data-1p-ignore
					data-lpignore="true"
					data-bwignore
					placeholder={mediaKeys?.brave.set
						? `Key configured (${mediaKeys.brave.hint}) — enter a new key to replace`
						: 'From brave.com/search/api — lets OMNIA search the web for images'}
				/>
			</label>
			<div class="ai-actions">
				<button
					class="primary"
					type="submit"
					disabled={savingMediaKeys ||
						(!tmdbInput.trim() &&
							!rawgInput.trim() &&
							!igdbIdInput.trim() &&
							!igdbSecretInput.trim() &&
							!braveInput.trim())}
				>
					{savingMediaKeys ? 'Saving…' : 'Save'}
				</button>
				{#if mediaKeys?.tmdb.set}
					<button type="button" onclick={() => void clearMediaKey('tmdb')}>Remove TMDB key</button>
				{/if}
				{#if mediaKeys?.rawg.set}
					<button type="button" onclick={() => void clearMediaKey('rawg')}>Remove RAWG key</button>
				{/if}
				{#if mediaKeys?.igdb_id.set || mediaKeys?.igdb_secret.set}
					<button type="button" onclick={() => void clearMediaKey('igdb_id')}>
						Remove IGDB credentials
					</button>
				{/if}
				{#if mediaKeys?.brave.set}
					<button type="button" onclick={() => void clearMediaKey('brave')}>
						Remove Brave key
					</button>
				{/if}
			</div>
			{#if mediaKeysResult}
				<p class="result" transition:fade={{ duration: 150 }}>{mediaKeysResult}</p>
			{/if}
			{#if mediaKeysError}<p class="ai-error">{mediaKeysError}</p>{/if}
		</form>
	</section>

	{/if}

	{#if section === 'general'}
	<div class="stack span-3">
	<section>
		<h2>Templates</h2>
		<div class="note-links">
			{#each templateNotes as n (n.path)}
				<a class="note-link" href="/note/{encodePath(n.path)}">{n.name.replace(/\.md$/, '')}</a>
			{/each}
			<button onclick={newTemplate}>+ New template</button>
		</div>
	</section>

	<section>
		<h2>Tasks</h2>
		<div class="note-links">
			<a class="note-link" href="/tasks">Board</a>
			<a class="note-link" href="/note/{encodePath('Tasks Inbox.md')}">Inbox note</a>
		</div>
		{#if timeline.length > 0}
			<ul class="task-timeline">
				{#each timeline.slice(0, 60) as event (event.id + event.kind + event.at)}
					<li>
						<span class="tl-when">{timelineDate(event.at)}</span>
						<span class="tl-kind" title={event.kind}>{timelineGlyphs[event.kind] ?? '·'}</span>
						<span class="tl-text" class:tl-deleted={event.kind === 'deleted'}>{event.text}</span>
					</li>
				{/each}
			</ul>
		{:else if timelineError}
			<p class="ai-error load-error">
				{timelineError}
				<button type="button" onclick={() => void loadTimeline()}>Retry</button>
			</p>
		{/if}
	</section>

	</div>
	{/if}

	{#if section === 'vault'}
	<section class="span-3">
		<h2>Search index</h2>
		<p>
			Keyword search: SQLite FTS5. Semantic search:
			{#if semantic}
				available (local embeddings, LanceDB).
			{:else}
				<span title={semanticError ?? undefined}>unavailable</span>.
			{/if}
		</p>
		<p class="muted">
			Indexes update incrementally on every save. If search results ever look wrong or stale,
			rebuild from scratch:
		</p>
		<button class="primary" onclick={reindex} disabled={reindexing}>
			{reindexing ? 'Rebuilding…' : 'Rebuild search index'}
		</button>
		{#if reindexResult}<p class="result" transition:fade={{ duration: 150 }}>{reindexResult}</p>{/if}
	</section>

	<section class="span-3">
		<h2>Backup</h2>
		{#if backup?.configured}
			<p>
				Pushing to <code>{backup.remote}</code> every {Math.round((backup.interval ?? 3600) / 60)}
				minutes.
				{#if backup.last_push}Last push: {backup.last_push}.{:else}No push yet this session.{/if}
			</p>
			{#if backup.last_error}
				<p class="ai-error">Last attempt failed: {backup.last_error}</p>
			{/if}
			<button class="primary" onclick={pushBackup} disabled={pushing}>
				{pushing ? 'Pushing…' : 'Push now'}
			</button>
			{#if pushResult}<p class="result" transition:fade={{ duration: 150 }}>{pushResult}</p>{/if}
		{:else}
			<p class="muted">
				The vault is a plain git repository on the server (<code>data/vault</code>). Every save
				and every approved AI change is a commit — clone it to back it up, or set
				<code>OMNIA_BACKUP_REMOTE</code> in the server environment to push it to a private git
				remote automatically on an interval.
			</p>
		{/if}
	</section>
	{/if}

	{#if section === 'devices' && authRequired}
		<section class="span-6">
			<h2>Signed-in devices</h2>
			<p class="muted">
				Locations are approximate from the login IP. VPNs and mobile networks may show their
				exit location instead.
			</p>
			{#if sessionsLoading && signedInSessions.length === 0}
				<p class="muted">Loading devices…</p>
			{:else if signedInSessions.length > 0}
				<div class="session-list">
					{#each signedInSessions as session (session.id)}
						<article class="session-card" class:current={session.current}>
							<div class="session-main">
								<div class="session-title">
									<strong>{session.browser} on {session.os}</strong>
									{#if session.current}<span class="current-badge">This device</span>{/if}
								</div>
								<p>{session.device_type} · {session.location}</p>
								<p class="session-detail">
									{session.ip_address} · Last active {sessionTime(session.last_seen_at)}
								</p>
							</div>
							<button
								class:danger={session.current}
								onclick={() => revokeSession(session)}
							>
								{session.current ? 'Log out' : 'Revoke'}
							</button>
						</article>
					{/each}
				</div>
			{:else}
				<p class="muted">No active sessions found.</p>
			{/if}
			{#if sessionsError}<p class="ai-error">{sessionsError}</p>{/if}
			<div class="session-actions">
				<button onclick={loadAuthSessions} disabled={sessionsLoading}>
					{sessionsLoading ? 'Refreshing…' : 'Refresh'}
				</button>
				<button onclick={logout}>Log out this device</button>
			</div>

			<div class="failed-heading">
				<div>
					<h3>Failed login attempts</h3>
					<p class="muted">Stored for up to 90 days, with the newest attempts first.</p>
				</div>
				{#if failedLoginAttempts.length > 0}
					<button onclick={clearFailedLoginHistory}>Clear history</button>
				{/if}
			</div>
			{#if failedLoginAttempts.length > 0}
				<div class="failed-list">
					{#each failedLoginAttempts as attempt (attempt.id)}
						<article class="failed-card">
							<div>
								<strong>{attempt.location}</strong>
								<p>{attempt.browser} on {attempt.os} · {attempt.device_type}</p>
							</div>
							<p class="session-detail">
								{attempt.ip_address} · {sessionTime(attempt.occurred_at)}
							</p>
						</article>
					{/each}
				</div>
			{:else if !sessionsLoading}
				<p class="muted">No failed login attempts recorded.</p>
			{/if}
		</section>
	{/if}
	</div>
	</div>
</div>

<style>
	.settings {
		flex: 1;
		overflow-y: auto;
		max-width: 1060px;
		width: 100%;
		margin: 0 auto;
		padding: 28px 32px;
	}
	h1 {
		font-size: 19px;
	}
	/* Nav rail on the left, one section's cards on the right. Collapses to a
	   horizontal chip row at the same breakpoint the bento collapses. */
	.settings-layout {
		display: grid;
		grid-template-columns: 168px minmax(0, 1fr);
		gap: 22px;
		align-items: start;
	}
	.section-nav {
		position: sticky;
		top: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.section-nav button {
		padding: 7px 12px;
		border: 0;
		border-left: 2px solid transparent;
		border-radius: 0 var(--radius) var(--radius) 0;
		background: transparent;
		color: var(--text-muted);
		font-size: 13px;
		text-align: left;
	}
	.section-nav button:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.section-nav button.active {
		border-left-color: var(--accent);
		background: var(--accent-soft);
		color: var(--accent);
		font-weight: 600;
	}
	/* Bento: sections are cards on a six-column grid. Spans are chosen so
	   every row sums to six on both platforms; rows equalize heights, which
	   is what makes it read as tiles rather than a ragged column. */
	.bento {
		display: grid;
		grid-template-columns: repeat(6, minmax(0, 1fr));
		gap: 14px;
	}
	.stack {
		display: grid;
		gap: 14px;
		align-content: start;
	}
	section {
		margin: 0;
		padding: 16px 18px 18px;
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 14px;
		min-width: 0;
		transition:
			background-color var(--motion-slow) var(--ease-out),
			border-color var(--motion-slow) var(--ease-out);
	}
	section > h2:first-child {
		margin-top: 0;
	}
	.span-3 {
		grid-column: span 3;
	}
	.span-6 {
		grid-column: span 6;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.5px;
		color: var(--text-muted);
	}
	code {
		font-family: var(--font-mono);
		background: var(--bg);
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.9em;
	}
	.muted {
		color: var(--text-muted);
		font-size: 13.5px;
	}
	.timezone-row {
		display: flex;
		align-items: flex-end;
		/* The row was sized for the old full-width column; in a narrow bento
		   card the button wraps under the input instead of poking out. */
		flex-wrap: wrap;
		gap: 8px;
		max-width: 480px;
	}
	.timezone-row label {
		flex: 1 1 180px;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.timezone-row input {
		width: 100%;
		min-width: 0;
	}
	.timezone-row label span {
		color: var(--text-muted);
		font-size: 11px;
		font-weight: 700;
	}
	.result {
		font-size: 13px;
		color: var(--accent);
		margin-top: 8px;
	}
	.on {
		color: var(--added);
		font-weight: 600;
	}
	.off {
		color: var(--text-muted);
		font-weight: 600;
	}
	.ai-form {
		display: flex;
		flex-direction: column;
		gap: 14px;
		margin-top: 10px;
		max-width: 460px;
	}
	.ai-form label,
	.ai-form .field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.ai-form label > span,
	.ai-form .field > span {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.4px;
	}
	.ai-form small {
		color: var(--text-muted);
		font-size: 12px;
	}
	/* Keys are masked text fields, not type=password: password managers leave
	   them alone and never offer to save a key as the site password. */
	.secret {
		-webkit-text-security: disc;
	}
	.ai-form select {
		font-family: inherit;
		font-size: 14px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 6px 10px;
	}
	.ai-actions {
		display: flex;
		gap: 8px;
	}
	.usage-line {
		margin: 0;
		font: 13px var(--font-mono);
		color: var(--text-muted);
	}
	.usage-callers {
		display: inline-flex;
		gap: 12px;
		margin-left: 12px;
	}
	.usage-controls {
		display: flex;
		flex-wrap: wrap;
		gap: 14px 24px;
		margin-top: 10px;
		max-width: 460px;
	}
	.usage-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		flex: 1 1 200px;
	}
	/* A special-role row: the model picker and its effort override side by
	   side, wrapping as one unit inside .usage-controls. */
	.usage-role {
		display: flex;
		flex: 1 1 100%;
		flex-wrap: wrap;
		gap: 14px 24px;
	}
	.usage-role .usage-field {
		flex: 2 1 200px;
	}
	.usage-role .usage-effort {
		flex: 1 1 130px;
	}
	.usage-field > span {
		font-size: 12px;
		font-weight: 600;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.4px;
	}
	.usage-field small {
		color: var(--text-muted);
		font-size: 12px;
	}
	.usage-field input,
	.usage-field select {
		font-family: inherit;
		font-size: 14px;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 6px 10px;
	}
	.ai-error {
		color: var(--danger);
		font-size: 13px;
		margin: 0;
	}
	.load-error {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.provider-row {
		display: flex;
		gap: 8px;
	}
	.provider-btn {
		border: 1px solid var(--border);
		font-size: 13px;
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.provider-btn.selected {
		border-color: var(--accent);
		background: var(--accent-soft);
		color: var(--accent);
		font-weight: 600;
		box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 25%, transparent);
	}
	.keyed {
		color: var(--added);
		font-size: 10px;
	}
	.theme-grid {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 10px;
		margin-top: 12px;
	}
	.theme-card {
		display: grid;
		grid-template-columns: 46px minmax(0, 1fr) 18px;
		align-items: center;
		gap: 10px;
		min-width: 0;
		padding: 10px;
		text-align: left;
		border: 1px solid var(--border);
		background: var(--bg);
	}
	.theme-card:hover {
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.theme-card.selected {
		border-color: var(--accent);
		background: var(--accent-soft);
		box-shadow: 0 0 0 1px var(--accent);
	}
	.theme-card:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.theme-preview {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		width: 46px;
		height: 34px;
		overflow: hidden;
		border: 1px solid color-mix(in srgb, var(--text) 20%, transparent);
		border-radius: 6px;
	}
	.theme-preview span:nth-child(1) {
		background: var(--swatch-1);
	}
	.theme-preview span:nth-child(2) {
		background: var(--swatch-2);
	}
	.theme-preview span:nth-child(3) {
		background: var(--swatch-3);
	}
	.theme-preview span:nth-child(4) {
		background: var(--swatch-4);
	}
	.theme-copy {
		display: flex;
		min-width: 0;
		flex-direction: column;
	}
	.theme-copy strong {
		font-size: 13px;
	}
	.theme-copy small {
		overflow: hidden;
		color: var(--text-muted);
		font-size: 11.5px;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.theme-check {
		color: var(--accent);
		font-size: 14px;
		font-weight: 700;
		opacity: 0;
		transform: scale(0.55) rotate(-12deg);
		transition:
			opacity var(--motion-base) var(--ease-out),
			transform var(--motion-base) var(--ease-snap);
	}
	.theme-card.selected .theme-check {
		opacity: 1;
		transform: scale(1) rotate(0deg);
	}
	.custom-editor {
		margin-top: 12px;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg);
	}
	.custom-row {
		display: flex;
		align-items: center;
		gap: 14px;
		margin-bottom: 10px;
	}
	.custom-label {
		width: 52px;
		flex-shrink: 0;
		color: var(--text-muted);
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.4px;
	}
	.mode-row {
		display: flex;
		gap: 6px;
	}
	.mode-row button {
		padding: 4px 14px;
		font-size: 12.5px;
	}
	.mode-row button.selected {
		border-color: var(--accent);
		background: var(--accent-soft);
		color: var(--accent);
	}
	.base-pickers,
	.var-grid {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 16px;
	}
	.color-pick {
		display: flex;
		align-items: center;
		gap: 7px;
		font-size: 12.5px;
		cursor: pointer;
	}
	.color-pick input[type='color'] {
		width: 30px;
		height: 24px;
		padding: 1px;
		border: 1px solid var(--border);
		border-radius: 5px;
		background: var(--bg);
		cursor: pointer;
	}
	.small {
		margin: 4px 0 10px;
		font-size: 12px;
	}
	.fine-tune summary {
		color: var(--text-muted);
		font-size: 12.5px;
		cursor: pointer;
		user-select: none;
	}
	.fine-tune[open] summary {
		margin-bottom: 10px;
	}
	.var-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
	}
	.reset-btn {
		margin-top: 12px;
		font-size: 12.5px;
	}
	.save-theme-form {
		display: flex;
		align-items: flex-end;
		gap: 8px;
		margin-top: 14px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
	}
	.save-theme-form label {
		display: flex;
		min-width: 0;
		flex: 1;
		flex-direction: column;
		gap: 5px;
	}
	.save-theme-form label > span,
	.saved-library h3 {
		color: var(--text-muted);
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.4px;
		text-transform: uppercase;
	}
	.save-theme-form input {
		width: 100%;
		min-width: 0;
	}
	.save-theme-form button {
		flex-shrink: 0;
	}
	.saved-library {
		margin-top: 16px;
	}
	.saved-library h3 {
		margin: 0 0 8px;
	}
	.saved-theme-list {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
	}
	.saved-theme-item {
		display: flex;
		align-items: stretch;
		min-width: 0;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--bg);
		transition:
			border-color var(--motion-base) var(--ease-out),
			background var(--motion-base) var(--ease-out),
			box-shadow var(--motion-base) var(--ease-out);
	}
	.saved-theme-item:hover {
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.saved-theme-item.active {
		border-color: var(--accent);
		background: var(--accent-soft);
		box-shadow: 0 0 0 1px var(--accent);
	}
	.saved-theme-apply {
		display: grid;
		grid-template-columns: 46px minmax(0, 1fr);
		align-items: center;
		gap: 10px;
		min-width: 0;
		flex: 1;
		padding: 9px 10px;
		text-align: left;
		border: 0;
		border-radius: 0;
		background: transparent;
		box-shadow: none;
	}
	.saved-theme-apply:hover {
		background: color-mix(in srgb, var(--accent) 7%, transparent);
	}
	.saved-theme-delete {
		width: 34px;
		flex-shrink: 0;
		padding: 0;
		border: 0;
		border-left: 1px solid var(--border);
		border-radius: 0;
		background: transparent;
		color: var(--text-muted);
		font-size: 18px;
		box-shadow: none;
	}
	.saved-theme-delete:hover {
		background: color-mix(in srgb, var(--danger) 12%, transparent);
		color: var(--danger);
	}
	.font-row {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		margin-top: 14px;
	}
	.font-card {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		min-width: 74px;
		padding: 10px 12px 8px;
		border: 1px solid var(--border);
		background: var(--bg);
	}
	.font-card:hover {
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.font-card.selected {
		border-color: var(--accent);
		background: var(--accent-soft);
		box-shadow: 0 0 0 1px var(--accent);
	}
	.font-sample {
		font-size: 21px;
		line-height: 1.1;
	}
	.font-name {
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.intoxicate-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		width: 100%;
		margin-top: 14px;
		padding: 12px 14px;
		border: 1px solid var(--border);
		background: var(--bg);
		text-align: left;
	}
	.intoxicate-toggle.active {
		background: var(--accent-soft);
		color: var(--accent);
	}
	.intoxicate-copy {
		display: flex;
		min-width: 0;
		flex-direction: column;
	}
	.intoxicate-copy strong {
		font-size: 13px;
	}
	.intoxicate-copy small {
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.toggle-track {
		position: relative;
		width: 38px;
		height: 22px;
		flex-shrink: 0;
		border-radius: 999px;
		background: var(--bg-hover);
		transition: background-color var(--motion-base) var(--ease-out);
	}
	.toggle-track span {
		position: absolute;
		top: 3px;
		left: 3px;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: var(--text-muted);
		transition:
			transform var(--motion-base) var(--ease-snap),
			background-color var(--motion-base) var(--ease-out);
	}
	.intoxicate-toggle.active .toggle-track,
	.device-toggle.active .toggle-track {
		background: var(--accent);
	}
	.intoxicate-toggle.active .toggle-track span,
	.device-toggle.active .toggle-track span {
		background: white;
		transform: translateX(16px);
	}
	.device-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		width: 100%;
		padding: 12px 14px;
		border: 1px solid var(--border);
		background: var(--bg);
		text-align: left;
	}
	.device-toggle:disabled {
		opacity: 0.6;
	}
	.device-toggle-copy {
		display: flex;
		min-width: 0;
		flex-direction: column;
	}
	.device-toggle-copy strong {
		font-size: 13px;
	}
	.device-toggle-copy small {
		color: var(--text-muted);
		font-size: 11.5px;
	}
	.push-test {
		margin-top: 12px;
	}
	.task-timeline {
		max-height: 300px;
		overflow-y: auto;
		margin: 14px 0 0;
		padding: 0;
		list-style: none;
	}
	.task-timeline li {
		display: flex;
		align-items: baseline;
		gap: 10px;
		padding: 3px 0;
		font-size: 13px;
	}
	.tl-when {
		width: 64px;
		flex-shrink: 0;
		color: var(--text-muted);
		font-size: 12px;
	}
	.tl-kind {
		width: 14px;
		flex-shrink: 0;
		color: var(--text-muted);
		text-align: center;
	}
	.tl-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.tl-deleted {
		color: var(--text-muted);
		text-decoration: line-through;
	}
	.vault-h3 + .task-timeline {
		margin-top: 0;
	}
	.sync-item {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	/* Beats .note-link, which comes later: a compact pill in a list row. */
	.note-link.sync-copy {
		flex: none;
		margin-left: auto;
		padding: 1px 10px;
		font-size: 12px;
	}
	.vault-h3 {
		margin: 18px 0 8px;
		color: var(--text-muted);
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.4px;
	}
	.note-links {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
	}
	.note-link {
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--bg);
		color: var(--text);
		font-size: 13px;
		text-decoration: none;
		transition:
			border-color var(--motion-base) var(--ease-out),
			color var(--motion-base) var(--ease-out);
	}
	.note-link:hover {
		border-color: var(--accent);
		color: var(--accent);
	}
	.session-list {
		display: grid;
		gap: 8px;
		margin: 12px 0;
	}
	.session-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 12px 14px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--bg);
		transition:
			border-color var(--motion-base) var(--ease-out),
			background-color var(--motion-slow) var(--ease-out);
	}
	.session-card.current {
		border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
	}
	.session-main {
		min-width: 0;
	}
	.session-title {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 7px;
	}
	.session-card p {
		margin: 4px 0 0;
	}
	.session-detail {
		color: var(--text-muted);
		font-size: 12px;
	}
	.current-badge {
		padding: 2px 7px;
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent);
		font-size: 10px;
		font-weight: 700;
		letter-spacing: 0.2px;
		text-transform: uppercase;
	}
	.session-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 10px;
	}
	.failed-heading {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		gap: 12px;
		margin-top: 24px;
	}
	.failed-heading h3 {
		margin: 0 0 3px;
		font-size: 14px;
	}
	.failed-heading p {
		margin: 0;
	}
	.failed-list {
		display: grid;
		gap: 6px;
		margin-top: 10px;
	}
	.failed-card {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 16px;
		padding: 10px 12px;
		border-left: 2px solid color-mix(in srgb, var(--danger) 70%, var(--border));
		border-radius: 6px;
		background: color-mix(in srgb, var(--bg) 88%, var(--danger));
	}
	.failed-card p {
		margin: 3px 0 0;
	}
	/* 760px, not the 620px this used to sit at: the app switches to its mobile
	   shell at 760, so anything between the two rendered the two-column bento
	   inside a phone chrome. */
	@media (max-width: 760px) {
		.settings {
			padding: 20px var(--m-gutter) 32px;
		}
		h1 {
			margin: 0 0 18px;
			font-size: var(--m-title);
			font-weight: 700;
			letter-spacing: -0.2px;
		}
		h2 {
			font-size: var(--m-label);
			font-weight: 600;
		}
		/* Narrow screens: the nav becomes a scrollable chip row above a plain
		   stack of cards. */
		.settings-layout {
			grid-template-columns: 1fr;
			gap: 14px;
		}
		.section-nav {
			position: static;
			flex-direction: row;
			gap: 6px;
			overflow-x: auto;
			padding-bottom: 2px;
			-webkit-overflow-scrolling: touch;
		}
		.section-nav button {
			flex-shrink: 0;
			min-height: 44px;
			padding: 5px 13px;
			border: 1px solid var(--border);
			border-radius: 999px;
			background: var(--bg-panel);
			white-space: nowrap;
		}
		.section-nav button.active {
			border-color: var(--accent);
			background: var(--accent-soft);
		}
		.bento {
			grid-template-columns: 1fr;
		}
		.span-3,
		.span-6,
		.stack {
			grid-column: auto;
		}
		.theme-grid {
			grid-template-columns: 1fr;
		}
		.theme-card {
			min-height: 62px;
		}
		.saved-theme-list {
			grid-template-columns: 1fr;
		}
		.save-theme-form {
			align-items: stretch;
			flex-direction: column;
		}
		.save-theme-form button {
			width: 100%;
		}
		.ai-form {
			max-width: none;
		}
		.provider-row {
			flex-wrap: wrap;
			gap: 7px;
		}
		.provider-btn {
			flex: 1 1 calc(50% - 7px);
			justify-content: center;
			min-width: 130px;
		}
		.ai-form input,
		.ai-form select {
			width: 100%;
		}
		/* Under 16px iOS zooms in on focus and does not zoom back out. */
		.ai-form select,
		.usage-field input,
		.usage-field select {
			font-size: 16px;
		}
		.ai-actions button {
			flex: 1;
		}
		.session-card {
			align-items: stretch;
			flex-direction: column;
			gap: 10px;
		}
		.session-card > button {
			width: 100%;
		}
		.failed-heading {
			align-items: stretch;
			flex-direction: column;
		}
		.failed-card {
			align-items: flex-start;
			flex-direction: column;
			gap: 4px;
		}
		.settings p,
		.settings code {
			overflow-wrap: anywhere;
		}
	}
</style>
