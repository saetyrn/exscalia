export interface DialogRequest {
	id: number;
	kind: 'confirm' | 'prompt';
	title: string;
	message: string;
	confirmLabel: string;
	cancelLabel: string;
	value: string;
	placeholder: string;
	danger: boolean;
	resolve: (value: boolean | string | null) => void;
}

export interface Toast {
	id: number;
	message: string;
	tone: 'default' | 'success' | 'error';
	actionLabel?: string;
	action?: () => void;
	/** Pending auto-dismiss; null while paused (hover/focus) or sticky. */
	timer: number | null;
	/** Lifetime left in ms, Infinity for a sticky toast. */
	remaining: number;
	startedAt: number;
}

// Plain notices go quickly; a toast that offers an action or reports an
// error stays long enough to be read and reached.
const TOAST_MS = 4200;
const LASTING_TOAST_MS = 12_000;
const RESUME_FLOOR_MS = 1500;

let nextId = 1;

export const ui = $state({
	dialog: null as DialogRequest | null,
	toasts: [] as Toast[],
	paletteOpen: false
});

export function confirmDialog(
	message: string,
	options: {
		title?: string;
		confirmLabel?: string;
		cancelLabel?: string;
		danger?: boolean;
	} = {}
): Promise<boolean> {
	return new Promise((resolve) => {
		ui.dialog = {
			id: nextId++,
			kind: 'confirm',
			title: options.title ?? 'Confirm',
			message,
			confirmLabel: options.confirmLabel ?? 'Confirm',
			cancelLabel: options.cancelLabel ?? 'Cancel',
			value: '',
			placeholder: '',
			danger: options.danger ?? false,
			resolve: (value) => resolve(value === true)
		};
	});
}

export function promptDialog(
	message: string,
	options: {
		title?: string;
		value?: string;
		placeholder?: string;
		confirmLabel?: string;
	} = {}
): Promise<string | null> {
	return new Promise((resolve) => {
		ui.dialog = {
			id: nextId++,
			kind: 'prompt',
			title: options.title ?? 'Enter a value',
			message,
			confirmLabel: options.confirmLabel ?? 'Save',
			cancelLabel: 'Cancel',
			value: options.value ?? '',
			placeholder: options.placeholder ?? '',
			danger: false,
			resolve: (value) => resolve(typeof value === 'string' ? value : null)
		};
	});
}

export function closeDialog(value: boolean | string | null): void {
	const dialog = ui.dialog;
	if (!dialog) return;
	ui.dialog = null;
	dialog.resolve(value);
}

export function notify(
	message: string,
	options: {
		tone?: Toast['tone'];
		actionLabel?: string;
		action?: () => void;
		/** Milliseconds before auto-dismiss; 0 keeps the toast until dismissed. */
		timeout?: number;
	} = {}
): void {
	const lifetime =
		options.timeout ?? (options.action || options.tone === 'error' ? LASTING_TOAST_MS : TOAST_MS);
	const toast: Toast = {
		id: nextId++,
		message,
		tone: options.tone ?? 'default',
		actionLabel: options.actionLabel,
		action: options.action,
		timer: null,
		remaining: lifetime > 0 ? lifetime : Infinity,
		startedAt: 0
	};
	startToastTimer(toast);
	ui.toasts.push(toast);
}

function startToastTimer(toast: Toast): void {
	if (toast.remaining === Infinity) return;
	toast.startedAt = Date.now();
	toast.timer = window.setTimeout(() => dismissToast(toast.id), toast.remaining);
}

export function pauseToast(id: number): void {
	const toast = ui.toasts.find((entry) => entry.id === id);
	if (!toast || toast.timer === null) return;
	window.clearTimeout(toast.timer);
	toast.timer = null;
	toast.remaining -= Date.now() - toast.startedAt;
}

export function resumeToast(id: number): void {
	const toast = ui.toasts.find((entry) => entry.id === id);
	if (!toast || toast.timer !== null || toast.remaining === Infinity) return;
	toast.remaining = Math.max(toast.remaining, RESUME_FLOOR_MS);
	startToastTimer(toast);
}

export function dismissToast(id: number): void {
	const index = ui.toasts.findIndex((toast) => toast.id === id);
	if (index < 0) return;
	const timer = ui.toasts[index].timer;
	if (timer !== null) window.clearTimeout(timer);
	ui.toasts.splice(index, 1);
}

export function openPalette(): void {
	ui.paletteOpen = true;
}

export function closePalette(): void {
	ui.paletteOpen = false;
}
