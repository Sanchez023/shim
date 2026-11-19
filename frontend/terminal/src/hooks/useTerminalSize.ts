import {useEffect, useState} from 'react';
import {useStdout} from 'ink';

export type TerminalSize = {
	columns: number;
	rows: number;
};

const FALLBACK: TerminalSize = {columns: 80, rows: 40};
const RESIZE_DEBOUNCE_MS = 50;

type ResizeSubscriber = () => void;

type ResizeEntry = {
	subscribers: Set<ResizeSubscriber>;
	timer: ReturnType<typeof setTimeout> | null;
	handler: () => void;
};

// One `resize` listener per stream, shared by every hook instance. Components
// such as MarkdownText mount one hook per message block, so subscribing each
// instance to stdout directly trips Node's default 10-listener warning.
const streams = new Map<NodeJS.WriteStream, ResizeEntry>();

function acquireEntry(stdout: NodeJS.WriteStream): ResizeEntry {
	const existing = streams.get(stdout);
	if (existing) {
		return existing;
	}

	const entry: ResizeEntry = {
		subscribers: new Set(),
		timer: null,
		handler: () => {
			if (entry.timer !== null) {
				clearTimeout(entry.timer);
			}
			entry.timer = setTimeout(() => {
				entry.timer = null;
				for (const notify of entry.subscribers) {
					notify();
				}
			}, RESIZE_DEBOUNCE_MS);
		},
	};
	stdout.on('resize', entry.handler);
	streams.set(stdout, entry);
	return entry;
}

function releaseEntry(stdout: NodeJS.WriteStream, entry: ResizeEntry): void {
	if (entry.subscribers.size > 0) {
		return;
	}
	if (entry.timer !== null) {
		clearTimeout(entry.timer);
		entry.timer = null;
	}
	stdout.off('resize', entry.handler);
	streams.delete(stdout);
}

function readSize(stdout: NodeJS.WriteStream | undefined): TerminalSize {
	return {
		columns: stdout?.columns || FALLBACK.columns,
		rows: stdout?.rows || FALLBACK.rows,
	};
}

/**
 * Track the live terminal size.
 *
 * Ink recalculates Yoga layout on `stdout.resize`, but that does not re-run
 * React components, so anything reading `stdout.columns` / `stdout.rows` in
 * the render body keeps the size from the previous frame. That left the fixed
 * frame height, transcript row budget, sidebar width, and table wrapping stale
 * after a window resize — the layout appeared distorted until the next message.
 * Subscribing here forces a re-render with the new dimensions (debounced so
 * dragging the window edge does not re-render on every intermediate size).
 *
 * All hook instances share a single stream listener; the listener is removed
 * once the last subscriber unmounts.
 */
export function useTerminalSize(): TerminalSize {
	const {stdout} = useStdout();
	const [size, setSize] = useState<TerminalSize>(() => readSize(stdout));

	useEffect(() => {
		if (!stdout) {
			return;
		}

		const entry = acquireEntry(stdout);

		const publish = (): void => {
			const next = readSize(stdout);
			setSize((previous) =>
				previous.columns === next.columns && previous.rows === next.rows
					? previous
					: next
			);
		};

		// Catch a size change that happened between the initial render and the
		// effect subscription (for example while the backend was connecting).
		entry.subscribers.add(publish);
		publish();

		return () => {
			entry.subscribers.delete(publish);
			releaseEntry(stdout, entry);
		};
	}, [stdout]);

	return size;
}
