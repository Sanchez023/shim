import React, {useEffect, useRef, useState} from 'react';
import {Box, Text, useInput, useStdin} from 'ink';
import chalk from 'chalk';

import {useTheme} from '../theme/ThemeContext.js';
import {Spinner} from './Spinner.js';

const noop = (): void => {};
const BACKSPACE_CONTROL_PATTERN = /^[\b\u007f]+$/;

type Grapheme = {text: string; start: number; end: number};
type SegmenterLike = {
	segment: (input: string) => Iterable<{segment: string; index: number}>;
};
type SegmenterConstructor = new (
	locales?: string,
	options?: {granularity: 'grapheme'},
) => SegmenterLike;

const Segmenter = (Intl as unknown as {Segmenter?: SegmenterConstructor}).Segmenter;
const graphemeSegmenter = Segmenter
	? new Segmenter(undefined, {granularity: 'grapheme'})
	: null;

/** Split text into user-perceived characters (emoji, CJK, combining marks). */
export function splitGraphemes(value: string): Grapheme[] {
	if (graphemeSegmenter) {
		return Array.from(graphemeSegmenter.segment(value), (part) => ({
			text: part.segment,
			start: part.index,
			end: part.index + part.segment.length,
		}));
	}
	const graphemes: Grapheme[] = [];
	let offset = 0;
	for (const char of value) {
		graphemes.push({text: char, start: offset, end: offset + char.length});
		offset += char.length;
	}
	return graphemes;
}

export function previousGraphemeOffset(value: string, offset: number): number {
	let previous = 0;
	for (const grapheme of splitGraphemes(value)) {
		if (grapheme.start >= offset) {
			break;
		}
		previous = grapheme.start;
	}
	return previous;
}

export function nextGraphemeOffset(value: string, offset: number): number {
	for (const grapheme of splitGraphemes(value)) {
		if (grapheme.end > offset) {
			return grapheme.end;
		}
	}
	return value.length;
}

export function getBackspaceDeleteCount(sequence: string): number {
	if (!sequence || !BACKSPACE_CONTROL_PATTERN.test(sequence)) {
		return 1;
	}

	return [...sequence].length;
}

function MultilineTextInput({
	value,
	onChange,
	onSubmit,
	focus = true,
	promptPrefix,
	promptColor,
	placeholder,
}: {
	value: string;
	onChange: (value: string) => void;
	onSubmit?: (value: string) => void;
	focus?: boolean;
	promptPrefix: string;
	promptColor: string;
	placeholder?: string;
}): React.JSX.Element {
	const [cursorOffset, setCursorOffset] = useState(value.length);
	const {internal_eventEmitter} = useStdin();
	const lastSequenceRef = useRef('');
	// Tracks the last value this component produced via onChange. If the
	// incoming `value` prop diverges from this, the change came from outside
	// (tab completion, history recall, programmatic clear) and we should
	// move the cursor to the end — otherwise the cursor stays wherever the
	// user had it, which puts subsequent keystrokes in the middle of the
	// newly-completed text. See HKUDS/OpenHarness#183.
	const lastInternalValueRef = useRef<string>(value);

	useEffect(() => {
		if (value === lastInternalValueRef.current) {
			// Self-authored update; cursor was already positioned by the
			// handler that called onChange.
			return;
		}
		lastInternalValueRef.current = value;
		setCursorOffset(value.length);
	}, [value]);

	const commitValue = (nextValue: string): void => {
		lastInternalValueRef.current = nextValue;
		onChange(nextValue);
	};

	useEffect(() => {
		if (!focus) {
			return;
		}

		const handleRawInput = (chunk: string | Buffer): void => {
			lastSequenceRef.current = Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
		};

		internal_eventEmitter.on('input', handleRawInput);
		return () => {
			internal_eventEmitter.removeListener('input', handleRawInput);
		};
	}, [focus, internal_eventEmitter]);

	useInput(
		(input, key) => {
			if (!focus) {
				return;
			}

			// Ctrl+<key> shortcuts are handled at the App level (interrupt,
			// history search, expand/collapse tools, copy, paste image). The
			// text input must not also insert the underlying letter.
			if (key.ctrl) {
				return;
			}

			if (
				key.upArrow ||
				key.downArrow ||
				key.tab ||
				(key.shift && key.tab) ||
				key.escape
			) {
				return;
			}

			if (key.return) {
				if (key.shift) {
					const nextValue = value.slice(0, cursorOffset) + '\n' + value.slice(cursorOffset);
					setCursorOffset(cursorOffset + 1);
					commitValue(nextValue);
					return;
				}
				onSubmit?.(value);
				return;
			}

			if (key.leftArrow) {
				setCursorOffset((previous) => previousGraphemeOffset(value, previous));
				return;
			}

			if (key.rightArrow) {
				setCursorOffset((previous) => nextGraphemeOffset(value, previous));
				return;
			}

			if (key.backspace) {
				if (cursorOffset === 0) {
					return;
				}
				const deleteCount = Math.min(cursorOffset, getBackspaceDeleteCount(lastSequenceRef.current || input));
				let start = cursorOffset;
				for (let index = 0; index < deleteCount; index += 1) {
					start = previousGraphemeOffset(value, start);
				}
				const nextValue = value.slice(0, start) + value.slice(cursorOffset);
				setCursorOffset(start);
				commitValue(nextValue);
				return;
			}

			if (key.delete) {
				// Ink reports the common DEL byte (`0x7f`) as `delete`, even though
				// many terminals emit it for the Backspace key. Use the raw sequence
				// to distinguish that case from a true forward-delete escape sequence.
				if (
					lastSequenceRef.current === '\x7f' ||
					lastSequenceRef.current === '\x1b\x7f' ||
					BACKSPACE_CONTROL_PATTERN.test(lastSequenceRef.current)
				) {
					if (cursorOffset === 0) {
						return;
					}
					const deleteCount = Math.min(cursorOffset, getBackspaceDeleteCount(lastSequenceRef.current));
					let start = cursorOffset;
					for (let index = 0; index < deleteCount; index += 1) {
						start = previousGraphemeOffset(value, start);
					}
					const nextValue = value.slice(0, start) + value.slice(cursorOffset);
					setCursorOffset(start);
					commitValue(nextValue);
					return;
				}

				if (cursorOffset >= value.length) {
					return;
				}
				const nextValue = value.slice(0, cursorOffset) + value.slice(nextGraphemeOffset(value, cursorOffset));
				commitValue(nextValue);
				return;
			}

			if (!input) {
				return;
			}

			const nextValue = value.slice(0, cursorOffset) + input + value.slice(cursorOffset);
			setCursorOffset(cursorOffset + input.length);
			commitValue(nextValue);
		},
		{isActive: focus},
	);

	let renderedValue = value;
	if (focus) {
		if (value.length === 0) {
			renderedValue = chalk.inverse(' ');
			if (placeholder) {
				renderedValue += chalk.dim(` ${placeholder}`);
			}
		} else {
			renderedValue = '';
			let cursorRendered = false;
			for (const grapheme of splitGraphemes(value)) {
				if (!cursorRendered && grapheme.start === cursorOffset) {
					if (grapheme.text === '\n') {
						// Keep the line break; the caret sits at the end of the line.
						renderedValue += `${chalk.inverse(' ')}\n`;
					} else {
						renderedValue += chalk.inverse(grapheme.text);
					}
					cursorRendered = true;
				} else {
					renderedValue += grapheme.text;
				}
			}
			if (!cursorRendered) {
				renderedValue += chalk.inverse(' ');
			}
		}
	}

	const lines = renderedValue.split('\n');
	const indent = ' '.repeat(promptPrefix.length);
	return (
		<Box flexDirection="column">
			{lines.map((line, index) => (
				<Box key={`${index}:${line}`}>
					<Text color={promptColor} bold>
						{index === 0 ? promptPrefix : indent}
					</Text>
					<Text>{line.length > 0 ? line : ' '}</Text>
				</Box>
			))}
		</Box>
	);
}

export function PromptInput({
	busy,
	input,
	setInput,
	onSubmit,
	toolName,
	suppressSubmit,
	statusLabel,
	imageAttachmentLabels = [],
	clipboardStatus,
	queuedMessages = [],
	readOnly = false,
	todoPanelHidden = false,
}: {
	busy: boolean;
	input: string;
	setInput: (value: string) => void;
	onSubmit: (value: string) => void;
	toolName?: string;
	suppressSubmit?: boolean;
	statusLabel?: string;
	imageAttachmentLabels?: string[];
	clipboardStatus?: string | null;
	queuedMessages?: string[];
	readOnly?: boolean;
	todoPanelHidden?: boolean;
}): React.JSX.Element {
	const {theme} = useTheme();
	// Keep the familiar prompt while a turn runs. The spinner above is enough of
	// a busy signal; a "… " prompt made the input look disabled and hid the fact
	// that typing queues a follow-up message.
	const promptPrefix = '> ';
	const placeholder = busy
		? 'type to queue a message (sent at the next tool boundary)'
		: undefined;
	// Only the bash tool can be detached (Ctrl+B -> background task).
	const canBackground = busy && (toolName ?? '').toLowerCase() === 'bash';

	return (
		<Box flexDirection="column">
			{busy ? (
				<Box flexDirection="column" marginBottom={0}>
					<Box>
						<Spinner label={statusLabel ?? (toolName ? `Running ${toolName}...` : 'Running...')} />
					</Box>
					{canBackground ? (
						<Box>
							<Text dimColor>{'  ctrl+b: move this command to the background'}</Text>
						</Box>
					) : null}
				</Box>
			) : null}
			{imageAttachmentLabels.length > 0 ? (
				<Box>
					<Text color={theme.colors.accent}>
						{imageAttachmentLabels.map((label, index) => `[image ${index + 1}: ${label}]`).join(' ')}
					</Text>
				</Box>
			) : null}
			{clipboardStatus ? (
				<Box>
					<Text color={theme.colors.muted}>{clipboardStatus}</Text>
				</Box>
			) : null}
			{todoPanelHidden && !busy ? (
				<Box>
					<Text dimColor>{'ctrl+t: show the todo panel'}</Text>
				</Box>
			) : null}
			{queuedMessages.length > 0 ? (
				<Box flexDirection="column">
					{queuedMessages.map((message, index) => (
						<Box key={`${index}:${message}`}>
							<Text color={theme.colors.accent}>{'↳ '}</Text>
							<Text dimColor>{message}</Text>
						</Box>
					))}
					<Box marginBottom={0}>
						<Text dimColor>
							{'  '}
							{queuedMessages.length === 1 ? '↑ edit queued message' : '↑ edits the latest queued message'}
							{' · ctrl+x ctrl+s sends now'}
						</Text>
					</Box>
				</Box>
			) : null}
			{readOnly ? (
				<Box>
					<Text dimColor>viewer open — ↑/↓ scroll · esc to close</Text>
				</Box>
			) : (
				<MultilineTextInput
					value={input}
					onChange={setInput}
					onSubmit={suppressSubmit ? noop : onSubmit}
					focus
					promptPrefix={promptPrefix}
					promptColor={theme.colors.primary}
					placeholder={placeholder}
				/>
			)}
		</Box>
	);
}
