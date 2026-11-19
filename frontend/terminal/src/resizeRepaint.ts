import ansiEscapes from 'ansi-escapes';
import stringWidth from 'string-width';

// Ink does not export its instance registry from the package root, but the
// resize repair has to read the frame Ink last printed (`lastOutput`) and take
// over its `resize` listener, so it can only work against the instance itself.
// The path resolves to the same module Ink's own imports use, which keeps the
// WeakMap shared after bundling.
// eslint-disable-next-line import/no-unresolved
import instances from '../node_modules/ink/build/instances.js';

/** The slice of Ink's instance this module relies on. */
type InkInstance = {
	lastOutput?: string;
	resized?: () => void;
};

/**
 * Number of terminal rows *text* occupies at *columns* once the terminal wraps
 * it. Mirrors how a terminal counts rows: every `\n` ends a logical line, and
 * each logical line wraps at the column boundary.
 */
export function physicalRows(text: string, columns: number): number {
	if (!text) {
		return 0;
	}
	const width = Math.max(1, columns);
	let rows = 0;
	for (const line of text.split('\n')) {
		const lineWidth = stringWidth(line);
		rows += lineWidth === 0 ? 1 : Math.ceil(lineWidth / width);
	}
	return rows;
}

/**
 * Erase sequence needed before Ink repaints after a resize, or `''`.
 *
 * Ink tracks its frame height as the number of logical lines it wrote
 * (`log-update`), and erases that many rows on the next paint. Narrowing the
 * window makes the terminal re-wrap those already printed lines into more rows,
 * so Ink's count is short by `physicalRows(new) - physicalRows(old)` rows and
 * the leftovers stay on screen, mixed into the transcript.
 *
 * The returned sequence erases exactly `delta` rows above the cursor and moves
 * the cursor up by `delta`, so Ink's own (stale) erase then lands on the first
 * row of the frame and erases the rest. `delta + 1` rows are touched because
 * `eraseLines` starts with the row the cursor is already on.
 */
export function reflowEraseSequence(
	frame: string,
	previousColumns: number,
	columns: number,
): string {
	if (!frame || previousColumns <= 0 || columns <= 0 || columns >= previousColumns) {
		return '';
	}
	const delta = physicalRows(frame, columns) - physicalRows(frame, previousColumns);
	return delta > 0 ? ansiEscapes.eraseLines(delta + 1) : '';
}

/**
 * Keep Ink's frame intact when the terminal is resized.
 *
 * Returns a cleanup function. No-op when the stream is not a TTY (Ink does not
 * listen for `resize` there) or when the Ink instance cannot be located.
 */
export function installResizeRepair(stdout: NodeJS.WriteStream): () => void {
	const noop = (): void => undefined;
	if (!stdout.isTTY) {
		return noop;
	}
	const ink = instances.get(stdout) as unknown as InkInstance | undefined;
	const inkResized = ink?.resized;
	if (!ink || typeof inkResized !== 'function') {
		return noop;
	}

	let previousColumns = stdout.columns || 0;
	const handleResize = (): void => {
		const columns = stdout.columns || 0;
		const erase = reflowEraseSequence(ink.lastOutput ?? '', previousColumns, columns);
		if (erase) {
			stdout.write(erase);
		}
		previousColumns = columns;
	};

	// Ink registered its own listener when it was constructed, and listeners run
	// in registration order: ours has to go first, so re-register Ink's after it.
	stdout.off('resize', inkResized);
	stdout.on('resize', handleResize);
	stdout.on('resize', inkResized);

	return () => {
		stdout.off('resize', handleResize);
		stdout.off('resize', inkResized);
		stdout.on('resize', inkResized);
	};
}
