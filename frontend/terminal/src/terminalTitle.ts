/**
 * Terminal title integration (OSC 0).
 *
 * Windows Terminal, iTerm2, VS Code, GNOME Terminal and most modern terminals
 * update the tab/window title when they receive an OSC 0 sequence. Keep the
 * title short and sanitize control characters so model/status text cannot
 * inject escape sequences.
 */

export type TerminalTitleState = {
	appName?: string;
	ready: boolean;
	busy: boolean;
	busyLabel?: string;
	waitingForInput?: boolean;
	model?: string;
	queuedCount?: number;
};

const MAX_TITLE_LENGTH = 120;

function sanitizeTitle(value: string): string {
	return value
		.replace(/[\u0000-\u001F\u007F]/g, ' ')
		.replace(/\s+/g, ' ')
		.trim();
}

export function buildTerminalTitle(state: TerminalTitleState): string {
	const appName = sanitizeTitle(state.appName ?? 'oh') || 'oh';
	const indicator = state.busy
		? '●'
		: state.waitingForInput
			? '◆'
			: state.ready
				? '○'
				: '◌';
	let status: string;
	if (!state.ready) {
		status = 'connecting…';
	} else if (state.waitingForInput) {
		status = 'waiting for input';
	} else if (state.busy) {
		status = sanitizeTitle(state.busyLabel ?? '') || 'working…';
	} else {
		status = 'idle';
	}

	const parts = [`${indicator} ${appName}`, status];
	const model = sanitizeTitle(state.model ?? '');
	if (model) {
		parts.push(model);
	}
	if ((state.queuedCount ?? 0) > 0) {
		parts.push(`+${state.queuedCount} queued`);
	}
	return sanitizeTitle(parts.join(' · ')).slice(0, MAX_TITLE_LENGTH);
}

export function terminalTitleSequence(title: string): string {
	return `\u001B]0;${sanitizeTitle(title)}\u0007`;
}

export function writeTerminalTitle(
	stdout: NodeJS.WriteStream | undefined,
	title: string,
): void {
	if (!stdout?.isTTY) {
		return;
	}
	stdout.write(terminalTitleSequence(title));
}
