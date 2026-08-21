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
}

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
		timeout?: number;
	} = {}
): void {
	const toast: Toast = {
		id: nextId++,
		message,
		tone: options.tone ?? 'default',
		actionLabel: options.actionLabel,
		action: options.action
	};
	ui.toasts.push(toast);
	window.setTimeout(() => dismissToast(toast.id), options.timeout ?? 4200);
}

export function dismissToast(id: number): void {
	const index = ui.toasts.findIndex((toast) => toast.id === id);
	if (index >= 0) ui.toasts.splice(index, 1);
}

export function openPalette(): void {
	ui.paletteOpen = true;
}

export function closePalette(): void {
	ui.paletteOpen = false;
}
