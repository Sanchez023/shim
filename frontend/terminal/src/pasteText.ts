/**
 * Claude Code-style paste collapsing.
 *
 * Terminals hand pasted text to the TUI as one large input chunk. Inserting it
 * verbatim floods the composer (and the scrollback) and makes the pending
 * message impossible to edit. Instead we keep the text in a side table and
 * insert a short placeholder, expanding it again right before submit.
 */

/** A paste at least this long becomes a placeholder. */
export const PASTE_COLLAPSE_MIN_CHARS = 400;
/** A paste with at least this many lines becomes a placeholder. */
export const PASTE_COLLAPSE_MIN_LINES = 4;

/** Regex source for one placeholder token: `[Pasted text #1 +12 lines]`. */
export const PASTE_PLACEHOLDER_SOURCE =
	'\\[Pasted text #\\d+ (?:\\+\\d+ lines?|\\(\\d+ chars\\))\\]';

export function shouldCollapsePaste(text: string): boolean {
	if (text.length >= PASTE_COLLAPSE_MIN_CHARS) {
		return true;
	}
	return text.split('\n').length >= PASTE_COLLAPSE_MIN_LINES;
}

export function formatPastePlaceholder(index: number, text: string): string {
	const lines = text.split('\n').length;
	if (lines > 1) {
		return `[Pasted text #${index} +${lines - 1} lines]`;
	}
	return `[Pasted text #${index} (${text.length} chars)]`;
}

/** Replace known placeholders with their original text; unknown ones stay. */
export function expandPastePlaceholders(
	value: string,
	blocks: ReadonlyMap<string, string>,
): string {
	if (blocks.size === 0 || !value.includes('[Pasted text #')) {
		return value;
	}
	return value.replace(new RegExp(PASTE_PLACEHOLDER_SOURCE, 'g'), (match) =>
		blocks.get(match) ?? match,
	);
}

/** Placeholder that ends exactly at `offset` (backspace target). */
export function placeholderEndingAt(
	value: string,
	offset: number,
): {start: number; token: string} | null {
	const pattern = new RegExp(PASTE_PLACEHOLDER_SOURCE, 'g');
	let match = pattern.exec(value);
	while (match) {
		const start = match.index;
		const end = start + match[0].length;
		if (end === offset) {
			return {start, token: match[0]};
		}
		if (end > offset) {
			return null;
		}
		match = pattern.exec(value);
	}
	return null;
}

/** Placeholder that starts exactly at `offset` (forward delete target). */
export function placeholderStartingAt(
	value: string,
	offset: number,
): {end: number; token: string} | null {
	const pattern = new RegExp(PASTE_PLACEHOLDER_SOURCE, 'g');
	let match = pattern.exec(value);
	while (match) {
		const start = match.index;
		if (start === offset) {
			return {end: start + match[0].length, token: match[0]};
		}
		if (start > offset) {
			return null;
		}
		match = pattern.exec(value);
	}
	return null;
}
