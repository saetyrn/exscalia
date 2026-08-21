import { CLIENT_ID } from './live';
import { apiBase, authHeaders } from './runtime';
import { readSseStream } from './sse';
import type { AppearanceSettings, AppearanceSettingsInput } from './theme';

export interface NoteListItem {
	path: string;
	name: string;
	pinned: boolean;
	/** This note is a folder's task board: hidden from the tree, opened as a
	 * board from the folder row. */
	board?: boolean;
	/** ISO timestamps (from the index) — drive the sidebar's sort-by-date menu. */
	created?: string | null;
	modified?: string | null;
}

export interface Note {
	path: string;
	content: string;
	meta: Record<string, unknown>;
	revision: string;
	status?: 'saved' | 'conflict_copy' | 'stale';
	conflict_path?: string;
	conflict_revision?: string;
}

export type MediaType = 'book' | 'movie' | 'tv' | 'game';
export type MediaStatus = 'planned' | 'active' | 'finished' | 'dropped';

export interface MediaItem {
	path: string;
	title: string;
	alias: string | null;
	media_type: MediaType;
	status: MediaStatus;
	creator: string | null;
	year: number | null;
	cover: string | null;
	rating: number | null;
	started: string | null;
	finished: string | null;
	total: number | null;
	progress: string | null;
	modified: string | null;
}

export interface MediaEvent {
	at: string;
	kind: 'started' | 'progress' | 'watched' | 'finished' | 'rated' | 'note';
	value: string | null;
	text: string;
	path: string;
	title: string;
	media_type: MediaType;
	cover: string | null;
	year: number | null;
	rating: number | null;
	status: MediaStatus;
}

export interface MediaCandidate {
	title: string | null;
	creator: string | null;
	year: number | null;
	cover_url: string | null;
	total: number | null;
	source: string | null;
	/** Tagged client-side after a per-type lookup (the backend result omits it),
	 * so the collective search can show a type badge and create the right item. */
	media_type?: MediaType | null;
}

export interface MediaMutation {
	path: string;
	item: MediaItem | null;
	revision: string;
	content: string;
	summary: string;
}

export interface MediaDetail {
	path: string;
	item: MediaItem | null;
	events: Pick<MediaEvent, 'at' | 'kind' | 'value' | 'text'>[];
	content: string;
	meta: Record<string, unknown>;
	revision: string;
}

export interface MediaProfile {
	name: string;
	location: string;
	avatar: string;
	banner: string;
	avatar_zoom: number;
	avatar_x: number;
	avatar_y: number;
	banner_zoom: number;
	banner_x: number;
	banner_y: number;
	avatars: string[];
	banners: string[];
}

export interface MediaPeriodStats {
	pages: number;
	movies: number;
	shows: number;
	games: number;
}

export interface MediaHub {
	profile: MediaProfile;
	stats: {
		watchlist: number;
		this_year: MediaPeriodStats;
		lifetime: MediaPeriodStats;
	};
	ratings: { counts: number[]; total: number };
	favorites: MediaItem[];
	watchlist: MediaItem[];
	recent: { at: string; kind: string; value: string | null; text: string; item: MediaItem }[];
	diary: {
		at: string;
		kind: string;
		value: string | null;
		text: string;
		path: string;
		title: string;
		media_type: MediaType;
	}[];
}

export class ApiError extends Error {
	status: number;
	constructor(status: number, detail: string) {
		super(detail);
		this.status = status;
	}
}

export const encodePath = (p: string) => p.split('/').map(encodeURIComponent).join('/');

/** All API traffic funnels through here: on the web it is a plain same-origin
 * fetch; on desktop it targets the configured server and carries the bearer
 * token. */
export function apiFetch(path: string, init?: RequestInit): Promise<Response> {
	return fetch(apiBase() + path, {
		...init,
		headers: { ...(init?.headers as Record<string, string>), ...authHeaders() }
	});
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
	const res = await apiFetch(url, {
		headers: { 'content-type': 'application/json' },
		...init
	});
	if (!res.ok) {
		let detail = res.statusText;
		try {
			detail = (await res.json()).detail ?? detail;
		} catch {
			/* not json */
		}
		throw new ApiError(res.status, detail);
	}
	return res.json();
}

export type CardGrade = 'again' | 'hard' | 'good' | 'easy';

export interface Flashcard {
	id: string;
	front: string;
	back: string;
	ease: number;
	interval: number;
	due: string;
	reps: number;
	lapses: number;
	last_grade: CardGrade | null;
	last_reviewed: string | null;
}

export interface DeckStats {
	new: number;
	learning: number;
	known: number;
	total: number;
	due: number;
}

export interface Deck {
	id: string;
	name: string;
	source_notes: string[];
	created_at: string | null;
	updated_at: string | null;
	stats: DeckStats;
	cards: Flashcard[];
	queue: string[];
}

/** Upload a binary attachment. Multipart, so it bypasses `req`'s JSON headers. */
export async function uploadAttachment(
	file: File
): Promise<{ path: string; size: number; url: string }> {
	const body = new FormData();
	body.append('file', file);
	const res = await apiFetch('/api/attachments', { method: 'POST', body });
	if (!res.ok) {
		let detail = res.statusText;
		try {
			detail = (await res.json()).detail ?? detail;
		} catch {
			/* not json */
		}
		throw new ApiError(res.status, detail);
	}
	return res.json();
}

export interface OmniaUiContext {
	selection?: string;
	view?: string;
	open_tabs?: string[];
	selection_range?: {
		from: number;
		to: number;
		head: number;
		doc_len: number;
		path: string;
	};
}

export async function streamOmniaMessage(
	threadId: string,
	message: string,
	contextNote: string | null,
	onEvent: (event: string, value: Record<string, unknown>) => void,
	signal?: AbortSignal,
	images: string[] = [],
	ui?: OmniaUiContext
): Promise<void> {
	const response = await apiFetch(
		`/api/omnia/threads/${encodeURIComponent(threadId)}/messages`,
		{
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ message, context_note: contextNote, images, ...(ui ?? {}) }),
			signal
		}
	);
	if (!response.ok) {
		let detail = response.statusText;
		try {
			detail = (await response.json()).detail ?? detail;
		} catch {
			// Keep the HTTP status text.
		}
		throw new ApiError(response.status, detail);
	}
	await readSseStream(response, ({ event, data }) => {
		try {
			onEvent(event, JSON.parse(data));
		} catch {
			onEvent('error', { detail: 'OMNIA returned an invalid stream event.' });
		}
	});
}

export interface SearchResult {
	type?: 'note' | 'task';
	id?: string;
	path: string;
	title: string;
	snippet?: string;
	score?: number;
	highlights?: { text: string; highlight: boolean }[];
	status?: TaskStatus;
	due?: string | null;
}

export interface TagCount {
	tag: string;
	count: number;
}

export interface Backlink {
	path: string;
	title: string;
}

export interface RelatedNote {
	path: string;
	title: string;
	score: number;
	snippet: string;
	linked: boolean;
}

export interface AIModel {
	id: string;
	label: string;
	blurb: string;
}

export interface AIProvider {
	id: string;
	label: string;
	key_placeholder: string;
	default_model: string;
	models: AIModel[];
}

export interface AIKeyStatus {
	set: boolean;
	hint: string | null;
	source: 'ui' | 'env' | null;
}

export interface AIConfig {
	provider: string;
	model: string;
	reasoning: string;
	available: boolean;
	keys: Record<string, AIKeyStatus>;
	models: Record<string, string>;
}

export interface UsageRow {
	input?: number;
	output?: number;
	cache_read?: number;
	cache_write?: number;
	calls?: number;
}

export interface OmniaSettings {
	housekeeping: boolean;
	full_auto: boolean;
	autocorrect: boolean;
	monthly_token_budget: number;
	background_model: string;
	media_model: string;
	autocorrect_model: string;
	chat_model: string;
	reasoning_levels: { background?: string; media?: string; autocorrect?: string };
	models: AIModel[];
	usage: { month: string; total: number; by_caller: Record<string, UsageRow> } | null;
}

export interface GraphicSpec {
	type: 'bar' | 'line' | 'pie' | 'heatmap';
	title?: string;
	x_title?: string;
	y_title?: string;
	x_labels?: string[];
	y_labels?: string[];
	series?: { name: string; values: number[] }[];
	cells?: number[][];
}

export interface ChatReply {
	session_id: string | null;
	reply: string;
	trace: { tool: string; input: string }[];
	graphics?: GraphicSpec[];
	pending_count: number;
	error?: string;
}

export type TaskStatus = 'todo' | 'in_progress' | 'done';

export interface CalTask {
	id: string;
	path: string;
	line: number;
	text: string;
	status: TaskStatus;
	done: boolean;
	due: string | null;
	due_visible: boolean;
	created_at: string | null;
	started_at: string | null;
	completed_at: string | null;
	reopened_at?: string | null;
	description?: string | null;
	/** Manual board position, set by dragging. null until the column is ordered. */
	sort?: number | null;
	tombstone?: boolean;
}

export interface TaskEvent {
	type: 'created' | 'started' | 'completed' | 'due';
	date: string;
	timestamp: string | null;
	task: CalTask;
}

export interface MonthView {
	daily_notes: string[];
	activity: Record<string, number>;
	task_activity: Record<string, number>;
	event_activity: Record<string, number>;
	events: CalendarEventOccurrence[];
	activity_hours: Record<string, number>;
	tasks: CalTask[];
}

export interface CalendarEventOccurrence {
	id: string;
	event_id: string;
	title: string;
	all_day: boolean;
	timezone: string;
	notes?: string | null;
	note_path?: string | null;
	occurrence_date: string;
	occurrence_start: string;
	occurrence_end: string;
	recurrence?: Record<string, unknown> | null;
	reminders?: number[];
	revision: string;
}

export interface ActivityEntry {
	id: string;
	date: string;
	activity: string;
	hours: number;
	created_at: string;
	updated_at: string;
}

export interface ActivityTotal {
	activity: string;
	hours: number;
}

export interface ActivityPeriod {
	start: string;
	end: string;
	total_hours: number;
	activities: ActivityTotal[];
}

export interface ActivitySummary {
	date: string;
	entries: ActivityEntry[];
	suggestions: string[];
	day: ActivityPeriod;
	week: ActivityPeriod;
	month: ActivityPeriod;
}

export interface DayView {
	date: string;
	daily_note: string | null;
	notes: {
		path: string;
		title: string;
		created: string;
		modified: string;
		created_today: number;
	}[];
	tasks: CalTask[];
	task_events: TaskEvent[];
	events: CalendarEventOccurrence[];
	activity_hours?: number;
}

export interface HistoryVersion {
	sha: string;
	short_sha: string;
	version_path: string;
	message: string;
	author: string;
	committed_at: string;
	size: number;
	preview: string;
	deleted: boolean;
}

export interface TrashItem {
	id: string;
	kind: 'note' | 'folder';
	original_path: string;
	deleted_at: string;
	expires_at: string;
	note_count: number;
}

export interface ThreadSummary {
	id: string;
	title: string;
	kind: 'named' | 'daily';
	date: string | null;
	created_at: string;
	updated_at: string;
	message_count: number;
	preview: string;
}

export interface MediaGallery {
	kind: string;
	query: string;
	images: { url: string; thumb: string; origin: string }[];
}

export interface ThreadMessage {
	id: string;
	role: 'user' | 'assistant';
	text: string;
	created_at: string;
	status: 'complete' | 'interrupted' | 'error' | 'streaming';
	context_note?: string | null;
	trace?: { tool: string; input: string }[];
	graphics?: GraphicSpec[];
	galleries?: MediaGallery[];
	actions?: OmniaAction[];
	confirmations?: OmniaConfirmation[];
	images?: string[];
}

export interface ProofreadSuggestion {
	id: string;
	category: 'spelling' | 'grammar' | 'clarity';
	before: string;
	after: string;
	reason: string;
	offset: number;
	end: number;
}

export interface ProofreadReview {
	path: string;
	/** Note revision the pass was made against. */
	revision: string | null;
	current_revision: string | null;
	suggestions: ProofreadSuggestion[];
}

export interface AutocorrectSuggestion {
	id: string;
	category: string;
	before: string;
	after: string;
	offset: number;
	end: number;
}

export interface AutocorrectResult {
	enabled: boolean;
	suggestions: AutocorrectSuggestion[];
}

export interface OmniaAction {
	id: string;
	tool: string;
	summary: string;
	paths: string[];
	refresh: string[];
	commit: string | null;
	created_at: string;
	undoable: boolean;
	undone?: boolean;
	/** In-app destination to open after the action (e.g. the study screen). */
	navigate?: string | null;
	/** Directive to close editor tabs (by name-match or all-but-current). */
	close_tabs?: { match?: string | null; others?: boolean } | null;
}

/** A persisted action record from the history endpoint. Carries `thread_id`
 * because undo is bound to the thread that performed the action. */
export interface OmniaActionRecord extends OmniaAction {
	thread_id: string;
	undone_at: string | null;
	rationale?: string | null;
}

export interface OmniaConfirmation {
	token: string;
	tool: string;
	summary: string;
	created_at: string;
	expires_at: string;
	status?: 'pending' | 'approved' | 'cancelled' | 'failed';
}

export interface OmniaThread extends Omit<ThreadSummary, 'message_count' | 'preview'> {
	messages: ThreadMessage[];
}

export interface PendingChange {
	id: number;
	session_id: string;
	op: 'create' | 'edit' | 'delete' | 'move';
	path: string;
	new_path: string | null;
	rationale: string | null;
	created_at: string;
	stale: boolean;
	old_content: string | null;
	new_content: string | null;
}

export interface AuthSession {
	id: string;
	current: boolean;
	browser: string;
	os: string;
	device_type: string;
	ip_address: string;
	location: string;
	created_at: string;
	last_seen_at: string;
	expires_at: string;
}

export interface FailedLoginAttempt {
	id: number;
	occurred_at: string;
	ip_address: string;
	location: string;
	browser: string;
	os: string;
	device_type: string;
}

export const api = {
	listNotes: () => req<NoteListItem[]>('/api/notes'),
	listFolders: () => req<string[]>('/api/notes/folders'),
	createFolder: (path: string) =>
		req<{ path: string }>('/api/notes/folders', {
			method: 'POST',
			body: JSON.stringify({ path })
		}),
	pinNote: (path: string, pinned: boolean) =>
		req<{ path: string; pinned: boolean }>(`/api/notes/${encodePath(path)}/pin`, {
			method: 'POST',
			body: JSON.stringify({ pinned })
		}),
	search: (q: string) => req<SearchResult[]>(`/api/search?q=${encodeURIComponent(q)}`),
	unifiedSearch: (
		q: string,
		filters: {
			folder?: string;
			tag?: string;
			result_type?: 'all' | 'note' | 'task';
			created_from?: string;
			modified_from?: string;
		} = {}
	) => {
		const params = new URLSearchParams({ q });
		for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
		return req<{ query: string; semantic_available: boolean; results: SearchResult[] }>(
			`/api/search/unified?${params}`
		);
	},
	semanticSearch: (q: string) =>
		req<SearchResult[]>(`/api/search/semantic?q=${encodeURIComponent(q)}`),
	semanticStatus: () => req<{ available: boolean }>('/api/search/semantic/status'),
	listTags: () => req<TagCount[]>('/api/tags'),
	notesForTag: (tag: string) =>
		req<{ path: string; title: string; modified: string }[]>(
			`/api/tags/${encodeURIComponent(tag)}/notes`
		),
	backlinks: (path: string) => req<Backlink[]>(`/api/backlinks/${encodePath(path)}`),
	relatedNotes: (path: string) =>
		req<{ available: boolean; related: RelatedNote[] }>(`/api/related/${encodePath(path)}`),
	addLink: (path: string, target: string) =>
		req<{ path: string; linked: string }>(`/api/notes/${encodePath(path)}/add-link`, {
			method: 'POST',
			body: JSON.stringify({ target })
		}),
	morningCheck: () =>
		req<{ greet: boolean; message?: string; thread_id?: string; pending_count?: number }>(
			'/api/overseer/morning',
			{ method: 'POST' }
		),
	graph: () =>
		req<{
			nodes: { path: string; title: string }[];
			edges: { source: string; target: string }[];
		}>('/api/graph'),
	listTemplates: () => req<string[]>('/api/templates'),
	openDaily: (date?: string) =>
		req<{ path: string; created: boolean }>(date ? `/api/daily/${date}` : '/api/daily', {
			method: 'POST'
		}),
	readNote: (path: string) => req<Note>(`/api/notes/${encodePath(path)}`),
	createNote: (path: string, content?: string, template?: string) =>
		req<Note>('/api/notes', {
			method: 'POST',
			body: JSON.stringify({ path, content, template })
		}),
	saveNote: (
		path: string,
		content: string,
		baseRevision?: string | null,
		onConflict: 'copy' | 'reject' = 'copy'
	) =>
		req<Note>(`/api/notes/${encodePath(path)}`, {
			method: 'PUT',
			// client_id lets the server echo this change to other devices while
			// telling this one to ignore it. on_conflict: 'reject' asks for the
			// newer content back instead of a conflict copy, so the editor can
			// merge and retry.
			body: JSON.stringify({
				content,
				base_revision: baseRevision ?? null,
				client_id: CLIENT_ID,
				on_conflict: onConflict
			})
		}),
	authStatus: () =>
		req<{ auth_required: boolean; authenticated: boolean }>('/api/auth/status'),
	login: (
		username: string,
		password: string,
		opts?: { createToken?: boolean; client?: string }
	) =>
		req<{ ok: boolean; token?: string }>('/api/auth/login', {
			method: 'POST',
			body: JSON.stringify({
				username,
				password,
				create_token: opts?.createToken ?? false,
				client: opts?.client ?? null
			})
		}),
	logout: () => req<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
	authSessions: () => req<{ sessions: AuthSession[] }>('/api/auth/sessions'),
	revokeAuthSession: (id: string) =>
		req<{ ok: boolean; current: boolean }>(`/api/auth/sessions/${encodeURIComponent(id)}`, {
			method: 'DELETE'
		}),
	failedLogins: () => req<{ attempts: FailedLoginAttempt[] }>('/api/auth/failed-logins'),
	clearFailedLogins: () =>
		req<{ ok: boolean }>('/api/auth/failed-logins', { method: 'DELETE' }),
	overseerStatus: () =>
		req<{ available: boolean; provider: string | null; model: string | null }>(
			'/api/overseer/status'
		),
	overseerChat: (message: string, sessionId?: string | null, contextNote?: string | null) =>
		req<ChatReply>('/api/overseer/chat', {
			method: 'POST',
			body: JSON.stringify({
				message,
				session_id: sessionId ?? null,
				context_note: contextNote ?? null
			})
		}),
	overseerModels: () => req<{ providers: AIProvider[] }>('/api/overseer/models'),
	getAIConfig: () => req<AIConfig>('/api/overseer/config'),
	saveAIConfig: (body: {
		provider?: string;
		api_key?: string;
		clear_key?: boolean;
		model?: string;
		reasoning?: string;
	}) => req<AIConfig>('/api/overseer/config', { method: 'POST', body: JSON.stringify(body) }),
	listPending: () => req<PendingChange[]>('/api/pending'),
	pendingCount: () => req<{ count: number }>('/api/pending/count'),
	approvePending: (id: number, force = false) =>
		req<{ applied: string; commit: string | null }>(`/api/pending/${id}/approve`, {
			method: 'POST',
			body: JSON.stringify({ force })
		}),
	discardPending: (id: number) =>
		req<{ ok: boolean }>(`/api/pending/${id}/discard`, { method: 'POST' }),
	approveAllPending: (force = false) =>
		req<{ applied: string[]; commit: string | null }>('/api/pending-approve-all', {
			method: 'POST',
			body: JSON.stringify({ force })
		}),
	discardAllPending: () =>
		req<{ discarded: number }>('/api/pending-discard-all', { method: 'POST' }),
	monthView: (year: number, month: number) =>
		req<MonthView>(`/api/calendar/${year}/${month}`),
	dayView: (day: string) => req<DayView>(`/api/calendar/day/${day}`),
	activitySummary: (date: string) =>
		req<ActivitySummary>(`/api/activities?date=${encodeURIComponent(date)}`),
	createActivity: (body: { date: string; activity: string; hours: number }) =>
		req<ActivityEntry>('/api/activities', {
			method: 'POST',
			body: JSON.stringify(body)
		}),
	updateActivity: (id: string, body: { activity?: string; hours?: number }) =>
		req<ActivityEntry>(`/api/activities/${encodeURIComponent(id)}`, {
			method: 'PATCH',
			body: JSON.stringify(body)
		}),
	deleteActivity: (id: string) =>
		req<{ ok: boolean }>(`/api/activities/${encodeURIComponent(id)}`, {
			method: 'DELETE'
		}),
	toggleTask: (path: string, line: number, done: boolean) =>
		req<{ ok: boolean; task: CalTask }>('/api/tasks/toggle', {
			method: 'POST',
			body: JSON.stringify({ path, line, done })
		}),
	listTasks: (includeCompleted = false, q = '', folder = '') =>
		req<CalTask[]>(
			`/api/tasks?include_completed=${includeCompleted}` +
				(q ? `&q=${encodeURIComponent(q)}` : '') +
				(folder ? `&folder=${encodePath(folder)}` : '')
		),
	createTaskBoard: (folder: string) =>
		req<{ folder: string; path: string }>('/api/notes/task-board', {
			method: 'POST',
			body: JSON.stringify({ folder })
		}),
	createTask: (body: {
		text: string;
		path?: string;
		folder?: string;
		status?: TaskStatus;
		due?: string | null;
		due_visible?: boolean;
		description?: string | null;
	}) => req<CalTask>('/api/tasks', { method: 'POST', body: JSON.stringify(body) }),
	listDecks: () => req<Omit<Deck, 'cards' | 'queue'>[]>('/api/flashcards'),
	getDeck: (id: string, order = 'due') =>
		req<Deck>(`/api/flashcards/${encodeURIComponent(id)}?order=${order}`),
	reviewCard: (deckId: string, cardId: string, grade: CardGrade) =>
		req<Flashcard>(`/api/flashcards/${encodeURIComponent(deckId)}/review`, {
			method: 'POST',
			body: JSON.stringify({ card_id: cardId, grade })
		}),
	taskTimeline: () =>
		req<{ at: string; kind: 'created' | 'started' | 'completed' | 'reopened' | 'deleted'; id: string; text: string; deleted?: boolean }[]>(
			'/api/tasks/timeline'
		),
	mediaList: (params: { type?: string; status?: string; sort?: string; dir?: string } = {}) => {
		const q = new URLSearchParams();
		for (const [key, value] of Object.entries(params)) if (value) q.set(key, value);
		const qs = q.toString();
		return req<MediaItem[]>(`/api/media${qs ? `?${qs}` : ''}`);
	},
	mediaTimeline: (limit = 200) => req<MediaEvent[]>(`/api/media/timeline?limit=${limit}`),
	mediaHub: () => req<MediaHub>('/api/media/hub'),
	mediaSaveProfile: (body: {
		name?: string;
		location?: string;
		avatar?: string;
		banner?: string;
		avatar_zoom?: number;
		avatar_x?: number;
		avatar_y?: number;
		banner_zoom?: number;
		banner_x?: number;
		banner_y?: number;
		favorites?: string[];
	}) =>
		req<{ profile: MediaProfile; favorites: MediaItem[] }>('/api/media/profile', {
			method: 'PUT',
			body: JSON.stringify(body)
		}),
	mediaGet: (path: string) => req<MediaDetail>(`/api/media/${encodePath(path)}`),
	mediaDelete: (path: string) =>
		req<{ ok: boolean }>(`/api/notes/${encodePath(path)}`, { method: 'DELETE' }),
	mediaEnrich: (path: string) =>
		req<{ path: string; skipped?: string; item: MediaItem | null }>(
			`/api/media/${encodePath(path)}/enrich`,
			{ method: 'POST' }
		),
	mediaLookup: (type: MediaType, q: string) =>
		req<{ candidates: MediaCandidate[]; error?: string }>(
			`/api/media/lookup?type=${type}&q=${encodeURIComponent(q)}`
		),
	mediaCreate: (body: {
		title: string;
		media_type: MediaType;
		status?: MediaStatus;
		creator?: string | null;
		year?: number | null;
		total?: number | null;
		rating?: number | null;
		cover?: string | null;
		cover_url?: string | null;
		source?: string | null;
		review?: string | null;
	}) =>
		req<{ path: string; item: MediaItem | null; cover_error?: string }>('/api/media', {
			method: 'POST',
			body: JSON.stringify({ ...body, client_id: CLIENT_ID })
		}),
	mediaMatch: (title: string) =>
		req<{ tracked: boolean; path?: string; item?: MediaItem | null; plays?: number }>(
			`/api/media/match?title=${encodeURIComponent(title)}`
		),
	// No client_id on purpose: the open editor must receive the note event for
	// the server-side rewrite and merge it, not skip it as its own echo.
	mediaPatch: (
		path: string,
		body: {
			rating?: number | null;
			status?: MediaStatus;
			alias?: string;
			base_revision?: string;
		}
	) =>
		req<MediaMutation>(`/api/media/${encodePath(path)}`, {
			method: 'PATCH',
			body: JSON.stringify(body)
		}),
	mediaSetReview: (path: string, body: { review: string; base_revision?: string }) =>
		req<MediaMutation>(`/api/media/${encodePath(path)}/review`, {
			method: 'PUT',
			body: JSON.stringify(body)
		}),
	mediaLog: (
		path: string,
		body: {
			kind: MediaEvent['kind'];
			value?: string | number | null;
			text?: string | null;
			date?: string | null;
			rating?: number | null;
			base_revision?: string;
		}
	) =>
		req<MediaMutation>(`/api/media/${encodePath(path)}/log`, {
			method: 'POST',
			body: JSON.stringify(body)
		}),
	mediaDeleteLog: (path: string, body: { at: string; text: string; base_revision?: string }) =>
		req<MediaMutation>(`/api/media/${encodePath(path)}/log/delete`, {
			method: 'POST',
			body: JSON.stringify(body)
		}),
	getMediaSettings: () =>
		req<
			Record<
				'tmdb' | 'rawg' | 'igdb_id' | 'igdb_secret' | 'brave',
				{ set: boolean; hint: string | null }
			>
		>('/api/media/settings'),
	saveMediaSettings: (body: {
		tmdb?: string;
		rawg?: string;
		igdb_id?: string;
		igdb_secret?: string;
		brave?: string;
	}) =>
		req<
			Record<
				'tmdb' | 'rawg' | 'igdb_id' | 'igdb_secret' | 'brave',
				{ set: boolean; hint: string | null }
			>
		>('/api/media/settings', {
			method: 'PUT',
			body: JSON.stringify(body)
		}),
	updateTask: (
		id: string,
		body: {
			text?: string;
			status?: TaskStatus;
			due?: string | null;
			due_set?: boolean;
			due_visible?: boolean;
		}
	) =>
		req<CalTask>(`/api/tasks/${encodeURIComponent(id)}`, {
			method: 'PATCH',
			body: JSON.stringify(body)
		}),
	/** `ids` is a column's complete order, top card first. */
	reorderTasks: (ids: string[]) =>
		req<CalTask[]>('/api/tasks/reorder', {
			method: 'POST',
			body: JSON.stringify({ ids })
		}),
	deleteTask: (id: string) =>
		req<{ ok: boolean; id: string; path: string }>(`/api/tasks/${encodeURIComponent(id)}`, {
			method: 'DELETE'
		}),
	history: (path: string) =>
		req<{ path: string; versions: HistoryVersion[] }>(
			`/api/history?path=${encodeURIComponent(path)}`
		),
	historyContent: (path: string, sha: string, versionPath?: string) =>
		req<{ path: string; sha: string; content: string }>(
			`/api/history/content?path=${encodeURIComponent(path)}&sha=${encodeURIComponent(sha)}${versionPath ? `&version_path=${encodeURIComponent(versionPath)}` : ''}`
		),
	historyDiff: (path: string, sha: string, versionPath?: string) =>
		req<{ path: string; sha: string; diff: string }>(
			`/api/history/diff?path=${encodeURIComponent(path)}&sha=${encodeURIComponent(sha)}${versionPath ? `&version_path=${encodeURIComponent(versionPath)}` : ''}`
		),
	restoreHistory: (body: {
		path: string;
		sha: string;
		version_path?: string;
		mode: 'replace' | 'copy';
		target_path?: string;
	}) =>
		req<Note>('/api/history/restore', {
			method: 'POST',
			body: JSON.stringify(body)
		}),
	listTrash: () => req<{ items: TrashItem[]; retention_days: number }>('/api/trash'),
	previewTrash: (id: string) =>
		req<TrashItem & { content: string | null; paths: string[] }>(
			`/api/trash/${encodeURIComponent(id)}`
		),
	restoreTrash: (id: string, targetPath?: string) =>
		req<{ id: string; kind: string; path: string; notes: string[] }>(
			`/api/trash/${encodeURIComponent(id)}/restore`,
			{
				method: 'POST',
				body: JSON.stringify({ target_path: targetPath ?? null })
			}
		),
	purgeTrash: (id: string) =>
		req<{ ok: boolean }>(`/api/trash/${encodeURIComponent(id)}`, { method: 'DELETE' }),
	emptyTrash: () =>
		req<{ ok: boolean; removed: number }>('/api/trash', { method: 'DELETE' }),
	listThreads: () => req<{ threads: ThreadSummary[] }>('/api/omnia/threads'),
	createThread: (title = 'New chat') =>
		req<OmniaThread>('/api/omnia/threads', {
			method: 'POST',
			body: JSON.stringify({ title })
		}),
	getThread: (id: string) => req<OmniaThread>(`/api/omnia/threads/${encodeURIComponent(id)}`),
	renameThread: (id: string, title: string) =>
		req<OmniaThread>(`/api/omnia/threads/${encodeURIComponent(id)}`, {
			method: 'PATCH',
			body: JSON.stringify({ title })
		}),
	deleteThread: (id: string) =>
		req<{ ok: boolean }>(`/api/omnia/threads/${encodeURIComponent(id)}`, {
			method: 'DELETE'
		}),
	approveOmniaConfirmation: (token: string, threadId: string) =>
		req<{
			result: Record<string, unknown>;
			action: OmniaAction | null;
			actions: OmniaAction[];
		}>(
			`/api/omnia/confirmations/${encodeURIComponent(token)}/approve`,
			{ method: 'POST', body: JSON.stringify({ thread_id: threadId }) }
		),
	cancelOmniaConfirmation: (token: string, threadId: string) =>
		req<{ ok: boolean }>(
			`/api/omnia/confirmations/${encodeURIComponent(token)}/cancel`,
			{ method: 'POST', body: JSON.stringify({ thread_id: threadId }) }
		),
	omniaAlerts: () =>
		req<{
			alerts: {
				key: string;
				title: string;
				starts_at?: string;
				minutes_before?: number;
				overdue?: boolean;
				body?: string;
				source?: string;
			}[];
		}>('/api/overseer/alerts'),
	proofread: (path: string) =>
		req<ProofreadReview>(`/api/proofread/${encodePath(path)}`),
	dismissSuggestion: (path: string, id: string) =>
		req<ProofreadReview>(
			`/api/proofread/${encodePath(path)}/dismiss/${encodeURIComponent(id)}`,
			{ method: 'POST' }
		),
	clearProofread: (path: string) =>
		req<ProofreadReview>(`/api/proofread/${encodePath(path)}`, { method: 'DELETE' }),
	autocorrect: (path: string, content: string) =>
		req<AutocorrectResult>(`/api/autocorrect/${encodePath(path)}`, {
			method: 'POST',
			body: JSON.stringify({ content })
		}),
	noteOrder: () => req<{ order: string[] }>('/api/settings/note-order'),
	saveNoteOrder: (order: string[]) =>
		req<{ order: string[] }>('/api/settings/note-order', {
			method: 'PUT',
			body: JSON.stringify({ order })
		}),
	folderOrder: () => req<{ order: string[] }>('/api/settings/folder-order'),
	saveFolderOrder: (order: string[]) =>
		req<{ order: string[] }>('/api/settings/folder-order', {
			method: 'PUT',
			body: JSON.stringify({ order })
		}),
	stopOmniaThread: (threadId: string) =>
		req<{ stopped: boolean }>(
			`/api/omnia/threads/${encodeURIComponent(threadId)}/stop`,
			{ method: 'POST' }
		),
	/** Drop a message and everything after it — used when regenerating a reply
	 * so the old exchange is replaced rather than duplicated. */
	truncateOmniaThread: (threadId: string, messageId: string) =>
		req<OmniaThread>(
			`/api/omnia/threads/${encodeURIComponent(threadId)}/messages/${encodeURIComponent(messageId)}`,
			{ method: 'DELETE' }
		),
	listOmniaActions: (limit = 100) =>
		req<{ actions: OmniaActionRecord[] }>(`/api/omnia/actions?limit=${limit}`),
	undoOmniaAction: (id: string, threadId: string) =>
		req<{ ok: boolean; summary: string; refresh: string[] }>(
			`/api/omnia/actions/${encodeURIComponent(id)}/undo`,
			{ method: 'POST', body: JSON.stringify({ thread_id: threadId }) }
		),
	backupStatus: () =>
		req<{
			configured: boolean;
			remote?: string;
			interval?: number;
			last_push?: string | null;
			last_error?: string | null;
		}>('/api/backup/status'),
	backupPush: () =>
		req<{ pushed: boolean; commit?: string; reason?: string; error?: string }>(
			'/api/backup/push',
			{ method: 'POST' }
		),
	rebuildIndex: () =>
		req<{ notes_indexed: number; chunks: number; semantic: boolean }>('/api/index/rebuild', {
			method: 'POST'
		}),
	getOmniaSettings: () => req<OmniaSettings>('/api/settings/omnia'),
	saveOmniaSettings: (changes: {
		housekeeping?: boolean;
		full_auto?: boolean;
		autocorrect?: boolean;
		monthly_token_budget?: number;
		background_model?: string;
		media_model?: string;
		autocorrect_model?: string;
		background_reasoning?: string;
		media_reasoning?: string;
		autocorrect_reasoning?: string;
	}) =>
		req<OmniaSettings>('/api/settings/omnia', {
			method: 'PUT',
			body: JSON.stringify(changes)
		}),
	getGeneralSettings: () => req<{ timezone: string }>('/api/settings/general'),
	saveGeneralSettings: (timezone: string) =>
		req<{ timezone: string }>('/api/settings/general', {
			method: 'PUT',
			body: JSON.stringify({ timezone })
		}),
	getPushKey: () => req<{ key: string | null }>('/api/push/public-key'),
	subscribePush: (sub: { endpoint: string; keys: { p256dh: string; auth: string } }) =>
		req<{ ok: boolean }>('/api/push/subscribe', {
			method: 'POST',
			body: JSON.stringify(sub)
		}),
	unsubscribePush: (endpoint: string) =>
		req<{ ok: boolean }>('/api/push/unsubscribe', {
			method: 'POST',
			body: JSON.stringify({ endpoint })
		}),
	testPush: () => req<{ sent: number; pruned: number }>('/api/push/test', { method: 'POST' }),
	getAppearanceSettings: () => req<AppearanceSettings>('/api/settings/appearance'),
	saveAppearanceSettings: (appearance: AppearanceSettingsInput) =>
		req<AppearanceSettings>('/api/settings/appearance', {
			method: 'PUT',
			body: JSON.stringify(appearance)
		}),
	renameNote: (path: string, newPath: string, newTitle?: string) =>
		// revision/content are absent from the offline fallback — treat as optional.
		req<{ path: string; revision?: string; content?: string }>(`/api/notes/${encodePath(path)}/rename`, {
			method: 'POST',
			// client_id: our own note_removed echo must not read as a remote
			// deletion of the note we just renamed.
			body: JSON.stringify({ new_path: newPath, new_title: newTitle, client_id: CLIENT_ID })
		}),
	renameFolder: (path: string, newPath: string) =>
		req<{ path: string; moved: number }>('/api/notes/folders/rename', {
			method: 'POST',
			body: JSON.stringify({ path, new_path: newPath, client_id: CLIENT_ID })
		}),
	deleteFolder: (path: string) =>
		req<{ ok: boolean; deleted: number }>(`/api/notes/folders/${encodePath(path)}`, {
			method: 'DELETE'
		}),
	deleteNote: (path: string) =>
		req<{ ok: boolean }>(`/api/notes/${encodePath(path)}`, { method: 'DELETE' })
};
