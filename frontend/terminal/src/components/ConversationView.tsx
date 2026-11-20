import React from 'react';
import {Box, Text} from 'ink';
import stringWidth from 'string-width';

import {useTerminalSize} from '../hooks/useTerminalSize.js';

import {useTheme} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';
import {MarkdownText} from './MarkdownText.js';
import {
	TOOL_DIFF_PREVIEW_ROWS,
	TOOL_OUTPUT_PREVIEW_ROWS,
	ToolCallDisplay,
	buildEditPreview,
	buildToolOutputPreview,
	summarizeToolInput,
} from './ToolCallDisplay.js';
import {WelcomeBanner} from './WelcomeBanner.js';

export type ToolPair = readonly [TranscriptItem, TranscriptItem];
export type GroupedItem = TranscriptItem | ToolPair;

export function groupToolPairs(items: TranscriptItem[]): GroupedItem[] {
	const result: GroupedItem[] = [];
	let i = 0;
	while (i < items.length) {
		const cur = items[i];
		const next = items[i + 1];
		if (cur.role === 'tool' && next?.role === 'tool_result') {
			result.push([cur, next] as const);
			i += 2;
		} else {
			result.push(cur);
			i++;
		}
	}
	return result;
}

/**
 * Index where the transcript splits into printed scrollback and the live frame.
 *
 * `printedCount` is monotonic: once a finished turn's tail has been printed it
 * must never slide back into the live frame, or Ink's ``Static`` would print it
 * a second time. While a turn runs the newest `liveWindow` items stay live so
 * streaming output and tool activity remain visible.
 */
export function staticBoundary(total: number, printedCount: number, liveWindow = 8): number {
	return Math.min(total, Math.max(printedCount, total - liveWindow));
}

/**
 * Split the transcript into a committed prefix (rendered once through Ink's
 * ``Static`` so the terminal scrollback keeps all context) and a small
 * dynamic tail that can still change (in-progress tools, streaming output).
 */
export function splitTranscript(
	items: TranscriptItem[],
	dynamicWindow = 8,
): {staticItems: TranscriptItem[]; dynamicItems: TranscriptItem[]} {
	let start = Math.max(0, items.length - dynamicWindow);
	// Never split a tool/tool_result pair across the boundary, and keep a
	// trailing tool call dynamic until its result arrives.
	while (
		start > 0 &&
		start < items.length &&
		items[start - 1].role === 'tool' &&
		items[start].role === 'tool_result'
	) {
		start -= 1;
	}
	while (start > 0 && items[start - 1].role === 'tool') {
		start -= 1;
	}
	return {
		staticItems: items.slice(0, start),
		dynamicItems: items.slice(start),
	};
}

export function TranscriptRow({
	group,
	theme,
	outputStyle,
	clamp = true,
	maxTextRows,
}: {
	group: GroupedItem;
	theme: ReturnType<typeof useTheme>['theme'];
	outputStyle: string;
	/**
	 * Clamp the row to the viewport. Disable this for rows printed into Ink's
	 * `<Static>`: scrollback has no height budget, and a finished answer must
	 * not be folded into "… N more lines" just because it is tall.
	 */
	clamp?: boolean;
	/** Rows this row may occupy inside the live frame, when the caller knows. */
	maxTextRows?: number;
}): React.JSX.Element {
	if (Array.isArray(group)) {
		const [toolItem, resultItem] = group as ToolPair;
		return (
			<ToolCallDisplay
				item={toolItem}
				resultItem={resultItem}
				outputStyle={outputStyle}
				maxDiffRows={
					maxTextRows
						? Math.min(TOOL_DIFF_PREVIEW_ROWS, Math.max(2, maxTextRows - 2))
						: TOOL_DIFF_PREVIEW_ROWS
				}
				maxOutputRows={
					maxTextRows
						? Math.min(TOOL_OUTPUT_PREVIEW_ROWS, Math.max(2, maxTextRows - 2))
						: TOOL_OUTPUT_PREVIEW_ROWS
				}
			/>
		);
	}
	return (
		<MessageRow
			item={group as TranscriptItem}
			theme={theme}
			outputStyle={outputStyle}
			clamp={clamp}
			maxTextRows={maxTextRows}
		/>
	);
}

function wrappedRows(text: string, width: number): number {
	if (!text) {
		return 0;
	}
	let rows = 0;
	for (const line of text.split('\n')) {
		const lineWidth = stringWidth(line);
		rows += lineWidth === 0 ? 1 : Math.ceil(lineWidth / width);
	}
	return rows;
}

function truncateToWidth(text: string, width: number): string {
	if (width <= 0) {
		return '…';
	}
	if (stringWidth(text) <= width) {
		return text;
	}
	let used = 0;
	let out = '';
	for (const char of text) {
		const charWidth = stringWidth(char);
		if (used + charWidth > width - 1) {
			break;
		}
		out += char;
		used += charWidth;
	}
	return `${out}…`;
}

/**
 * Bound a message to the rows it can actually occupy after wrapping.
 *
 * Splitting on newlines alone is not enough: a single 100k-character line
 * still wraps to thousands of terminal rows and pushes the prompt off screen.
 */
function clampWrappedText(
	text: string,
	maxRows: number,
	width: number,
): {text: string; hidden: boolean} {
	const lines = text.split('\n');
	const kept: string[] = [];
	let used = 0;
	for (const line of lines) {
		const rows = Math.max(1, Math.ceil(stringWidth(line) / width));
		if (used + rows > maxRows) {
			const remaining = Math.max(0, maxRows - used);
			if (remaining > 0) {
				kept.push(truncateToWidth(line, remaining * width));
			}
			return {text: kept.join('\n'), hidden: true};
		}
		kept.push(line);
		used += rows;
	}
	return {text, hidden: false};
}

function truncateTailToWidth(text: string, width: number): string {
	if (width <= 0) {
		return '…';
	}
	if (stringWidth(text) <= width) {
		return text;
	}
	let used = 0;
	let out = '';
	const chars = [...text];
	for (let index = chars.length - 1; index >= 0; index -= 1) {
		const charWidth = stringWidth(chars[index]);
		if (used + charWidth > width - 1) {
			break;
		}
		out = chars[index] + out;
		used += charWidth;
	}
	return `…${out}`;
}

/**
 * Bound a *live* answer to the rows it can occupy, keeping its tail.
 *
 * `clampWrappedText` keeps the head, which is right for user/system/log rows
 * (they are read from the top).  An assistant answer is different: while it
 * streams, and when it has just finished, the newest text is the part the user
 * is waiting for, so trimming the end hides the conclusion behind a
 * "… N more lines" hint.  Scrollback still keeps the full text.
 */
function clampWrappedTail(
	text: string,
	maxRows: number,
	width: number,
): {text: string; hidden: boolean; dropped: number} {
	const lines = text.split('\n');
	const kept: string[] = [];
	let used = 0;
	for (let index = lines.length - 1; index >= 0; index -= 1) {
		const rows = Math.max(1, Math.ceil(stringWidth(lines[index]) / width));
		if (used + rows > maxRows) {
			const remaining = Math.max(0, maxRows - used);
			if (remaining > 0) {
				kept.unshift(truncateTailToWidth(lines[index], remaining * width));
			}
			return {text: kept.join('\n'), hidden: true, dropped: index + 1};
		}
		kept.unshift(lines[index]);
		used += rows;
	}
	return {text, hidden: false, dropped: 0};
}

function estimateGroupRows(
	group: GroupedItem,
	width: number,
	maxTextRows: number,
): number {
	if (Array.isArray(group)) {
		const [tool, result] = group as ToolPair;
		const toolName = tool.tool_name ?? 'tool';
		const summary = summarizeToolInput(
			toolName,
			tool.tool_input,
			tool.text,
		)
			.replace(/\s+/g, ' ')
			.trim();
		let rows = wrappedRows(`${toolName} ${summary}`, width);
		if (result?.is_error) {
			rows += Math.min(
				5,
				Math.max(1, result.text.split('\n').filter((line) => line.trim()).length),
			);
		} else if (result) {
			// Count the same preview block the row renders, so the live-frame
			// budget does not include a group that then overflows.
			const previewLimit = Math.min(
				TOOL_DIFF_PREVIEW_ROWS,
				Math.max(2, maxTextRows - 2),
			);
			const edit = buildEditPreview(toolName, tool.tool_input, result, previewLimit);
			if (edit) {
				rows += 1; // "Added N lines, removed M lines"
				const numbers = edit.rows
					.flatMap((row) => [row.oldLine, row.newLine])
					.filter((line): line is number => typeof line === 'number');
				const numberWidth = Math.max(3, ...numbers.map((line) => String(line).length));
				for (const row of edit.rows) {
					const marker = row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' ';
					const lineNo = row.kind === 'del' ? row.oldLine : row.newLine;
					const gutter = `     ${lineNo != null ? `${String(lineNo).padStart(numberWidth)} ` : ''}${marker}`;
					rows += wrappedRows(`${gutter}${row.text}`, width);
				}
				if (edit.hidden > 0) {
					rows += 1;
				}
			} else if (toolName.toLowerCase() === 'bash') {
				const outputLimit = Math.min(
					TOOL_OUTPUT_PREVIEW_ROWS,
					Math.max(2, maxTextRows - 2),
				);
				const output = buildToolOutputPreview(toolName, result, outputLimit);
				if (output) {
					for (const line of output.lines) {
						rows += wrappedRows(`     ${line}`, width);
					}
					if (output.hidden > 0) {
						rows += 1;
					}
				}
			} else {
				rows += 1; // one-line result summary
			}
		}
		return rows;
	}
	const item = group as TranscriptItem;
	const lines = wrappedRows(item.text, width);
	switch (item.role) {
		case 'user':
			return Math.min(maxTextRows, lines) + 2;
		case 'assistant':
			// Markdown adds code fences, headings and table rules on top of the
			// plain wrapped line count; keep a small safety margin so the tail
			// still fits after rendering.
			return Math.min(maxTextRows, Math.ceil(lines * 1.15)) + 2;
		case 'system':
			return Math.min(maxTextRows, lines) + 1;
		case 'log':
			return Math.min(8, lines) + 1;
		case 'tool':
		case 'tool_result':
			return Math.max(1, Math.min(8, lines)) + 1;
		case 'thinking':
		case 'status':
			return 1;
		default:
			return Math.min(maxTextRows, lines) + 1;
	}
}

/**
 * Keep the newest conversation rows that fit in the live frame.
 *
 * Completed rows are committed to Ink's scrollback, so the live view only
 * needs the tail. Rendering every dynamic row let a long turn grow the live
 * frame past the terminal height and pushed the prompt input off screen.
 */
export function selectVisibleGroups(
	groups: GroupedItem[],
	budget: number,
	width: number,
	maxTextRows: number,
): GroupedItem[] {
	if (groups.length === 0) {
		return groups;
	}
	const visible: GroupedItem[] = [];
	let used = 0;
	for (let index = groups.length - 1; index >= 0; index -= 1) {
		const rows = estimateGroupRows(groups[index], width, maxTextRows);
		if (visible.length > 0 && used + rows > budget) {
			break;
		}
		visible.push(groups[index]);
		used += rows;
		if (used >= budget) {
			break;
		}
	}
	return visible.reverse();
}

function ConversationViewInner({
	items,
	assistantBuffer,
	thinkingBuffer,
	showWelcome,
	outputStyle,
	appName,
	version,
	maxRows,
}: {
	items: TranscriptItem[];
	assistantBuffer: string;
	thinkingBuffer?: string;
	showWelcome: boolean;
	outputStyle: string;
	appName?: string;
	version?: string;
	maxRows?: number;
}): React.JSX.Element {
	const {theme} = useTheme();
	const {rows: terminalRows, columns: terminalColumns} = useTerminalSize();
	const isCodexStyle = outputStyle === 'codex';
	const grouped = groupToolPairs(items);
	const budget = Math.max(1, maxRows ?? terminalRows - 10);
	// One row for the "⏺" header, one for the margin above it, one spare. Ink
	// clips whatever does not fit at the *bottom* of this box, so an item that
	// overflows would silently lose its last lines instead of showing "… more".
	const maxTextRows = Math.max(3, budget - 3);
	const wrapWidth = Math.max(20, terminalColumns - 6);
	// The live answer and the conversation share one fixed-height frame. The
	// answer is what the user is watching, so it gets the bulk of the budget, but
	// letting it take all of it squeezed the transcript down to the single row
	// `Math.max(1, ...)` below guarantees — which reads as "my earlier
	// conversation disappeared while the agent was working". Reserve the rows the
	// newest two items actually need, capped at half the frame.
	const historyReserve = (() => {
		if (grouped.length === 0) {
			return 0;
		}
		let rows = 0;
		for (let index = grouped.length - 1; index >= 0; index -= 1) {
			rows += estimateGroupRows(grouped[index], wrapWidth, maxTextRows);
			if (grouped.length - index >= 2) {
				break;
			}
		}
		return Math.min(rows, Math.max(3, Math.floor(budget / 2)));
	})();
	// Both live blocks stream, so both are trimmed from the top and keep their
	// newest rows.
	const liveThinking = thinkingBuffer
		? clampWrappedTail(
				thinkingBuffer,
				Math.max(3, Math.min(12, Math.floor(budget / 3))),
				wrapWidth,
			)
		: null;
	const thinkingRows = liveThinking
		? wrappedRows(liveThinking.text, wrapWidth) + 2
		: 0;
	// "⏺" header, the margin above it, and one spare row are part of the live
	// block; whatever is left after the history reserve is the answer's text.
	const liveTextRows = Math.max(
		3,
		budget - historyReserve - thinkingRows - 3,
	);
	const clampedBuffer = assistantBuffer
		? clampWrappedTail(assistantBuffer, liveTextRows, wrapWidth)
		: null;
	const assistantRows = clampedBuffer
		? wrappedRows(clampedBuffer.text, wrapWidth) + 2
		: 0;
	const available = Math.max(1, budget - assistantRows - thinkingRows);
	const visibleGroups = selectVisibleGroups(
		grouped,
		available,
		wrapWidth,
		maxTextRows,
	);

	return (
		<Box flexDirection="column" flexGrow={1} flexBasis={0} overflowY="hidden">
			{showWelcome && items.length === 0 ? (
				<WelcomeBanner appName={appName} version={version} />
			) : null}

			{visibleGroups.map((group, index) => (
				<TranscriptRow
					key={index}
					group={group}
					theme={theme}
					outputStyle={outputStyle}
					maxTextRows={maxTextRows}
				/>
			))}

			{liveThinking ? (
				<Box marginTop={0} flexDirection="column">
					<Text dimColor>✻ Thinking…</Text>
					<Box marginLeft={2} flexDirection="column">
						<Text dimColor>{liveThinking.text}</Text>
					</Box>
					<TruncatedHint hidden={liveThinking.dropped} earlier />
				</Box>
			) : null}

			{clampedBuffer ? (
				isCodexStyle ? (
					<Box flexDirection="column" marginTop={0}>
						<Text>{clampedBuffer.text}</Text>
						<TruncatedHint hidden={clampedBuffer.dropped} earlier />
					</Box>
				) : (
					<Box marginTop={1} marginBottom={0} flexDirection="column">
						<Text>
							<Text color={theme.colors.success} bold>{theme.icons.assistant}</Text>
						</Text>
						<Box marginLeft={2} flexDirection="column">
							<MarkdownText content={clampedBuffer.text} />
						</Box>
						<TruncatedHint hidden={clampedBuffer.dropped} earlier />
					</Box>
				)
			) : null}
		</Box>
	);
}

export const ConversationView = React.memo(ConversationViewInner);

function TruncatedHint({
	hidden = 0,
	truncated = false,
	earlier = false,
}: {
	hidden?: number;
	truncated?: boolean;
	earlier?: boolean;
}): React.JSX.Element | null {
	if (!truncated && hidden <= 0) {
		return null;
	}
	const direction = earlier ? 'earlier' : 'more';
	const label = truncated
		? `${direction} lines`
		: `${hidden} ${direction} line${hidden === 1 ? '' : 's'}`;
	return (
		<Text dimColor>
			{'  '}… {label} · ctrl+o full transcript
		</Text>
	);
}

function MessageRow({
	item,
	theme,
	outputStyle,
	clamp = true,
	maxTextRows,
}: {
	item: TranscriptItem;
	theme: ReturnType<typeof useTheme>['theme'];
	outputStyle: string;
	clamp?: boolean;
	maxTextRows?: number;
}): React.JSX.Element {
	const isCodexStyle = outputStyle === 'codex';
	const {rows, columns} = useTerminalSize();
	// Leave room for the status bar and prompt so the committed row itself is
	// always shorter than the viewport. Inside the live frame the caller knows
	// how much room is really left (the checklist and prompt eat into it).
	const rowBudget = maxTextRows ?? Math.max(8, rows - 12);
	const wrapWidth = Math.max(20, columns - 6);
	// A hard cap only applies to the live frame. Rows printed into scrollback
	// carry no height budget, so a finished answer is shown in full instead of
	// collapsing into "… N more lines" that has to be reopened with ctrl+o.
	const limitRows = (text: string): {text: string; truncated: boolean} => {
		if (!clamp) {
			return {text, truncated: false};
		}
		const clipped = clampWrappedText(text, rowBudget, wrapWidth);
		return {text: clipped.text, truncated: clipped.hidden};
	};
	// Assistant answers keep their tail in the live frame instead of their head:
	// the conclusion is what the user just waited for, and a head clamp made it
	// look as if the end of the final answer had been folded away.
	const limitRowsTail = (text: string): {text: string; truncated: boolean} => {
		if (!clamp) {
			return {text, truncated: false};
		}
		const clipped = clampWrappedTail(text, rowBudget, wrapWidth);
		return {text: clipped.text, truncated: clipped.hidden};
	};

	switch (item.role) {
		case 'user': {
			const clamped = limitRows(item.text);
			if (isCodexStyle) {
				return (
					<Box marginTop={0} flexDirection="column">
						<Text>
							<Text dimColor>{'> '}</Text>
							<Text>{clamped.text}</Text>
						</Text>
						<TruncatedHint truncated={clamped.truncated} />
					</Box>
				);
			}
			return (
				<Box marginTop={1} marginBottom={0} flexDirection="column">
					<Text>
						<Text color={theme.colors.secondary} bold>{theme.icons.user}</Text>
						<Text>{clamped.text}</Text>
					</Text>
					<TruncatedHint truncated={clamped.truncated} />
				</Box>
			);
		}

		case 'assistant': {
			const clamped = limitRowsTail(item.text);
			if (isCodexStyle) {
				return (
					<Box marginTop={0} marginBottom={0} flexDirection="column">
						<Text>{clamped.text}</Text>
						<TruncatedHint truncated={clamped.truncated} earlier />
					</Box>
				);
			}
			return (
				<Box marginTop={1} marginBottom={0} flexDirection="column">
					<Text>
						<Text color={theme.colors.success} bold>{theme.icons.assistant}</Text>
					</Text>
					<Box marginLeft={2} flexDirection="column">
						<MarkdownText content={clamped.text} />
					</Box>
					<TruncatedHint truncated={clamped.truncated} earlier />
				</Box>
			);
		}

		case 'thinking': {
			// Collapsed by default, like Claude Code: the reasoning streams in
			// the live frame, then leaves one summary row. The full text remains
			// available in the ctrl+o transcript view.
			const seconds =
				item.duration_ms != null && item.duration_ms >= 1000
					? Math.round(item.duration_ms / 1000)
					: null;
			return (
				<Box marginTop={0}>
					<Text dimColor>
						{seconds ? `✻ Thought for ${seconds}s` : '✻ Thought'}
						{'  ·  ctrl+o full transcript'}
					</Text>
				</Box>
			);
		}

		case 'tool':
		case 'tool_result':
			return (
				<ToolCallDisplay
					item={item}
					outputStyle={outputStyle}
					maxDiffRows={Math.min(TOOL_DIFF_PREVIEW_ROWS, Math.max(2, rowBudget - 2))}
					maxOutputRows={Math.min(TOOL_OUTPUT_PREVIEW_ROWS, Math.max(2, rowBudget - 2))}
				/>
			);

		case 'system': {
			// Command output keeps its clamp even in scrollback: oversized output
			// is routed to the scrollable output view instead.
			const clamped = clampWrappedText(item.text, rowBudget, wrapWidth);
			if (isCodexStyle) {
				return (
					<Box marginTop={0} flexDirection="column">
						<Text>
							<Text color={theme.colors.warning}>[system]</Text>
							<Text> {clamped.text}</Text>
						</Text>
						<TruncatedHint truncated={clamped.hidden} />
					</Box>
				);
			}
			return (
				<Box marginTop={0} flexDirection="column">
					<Text>
						<Text color={theme.colors.warning}>{theme.icons.system}</Text>
						<Text color={theme.colors.warning}>{clamped.text}</Text>
					</Text>
					<TruncatedHint truncated={clamped.hidden} />
				</Box>
			);
		}

		case 'status':
			return (
				<Box marginTop={0}>
					<Text color={theme.colors.info}>{item.text}</Text>
				</Box>
			);

		case 'log': {
			const clamped = clampWrappedText(item.text, rowBudget, wrapWidth);
			return (
				<Box flexDirection="column">
					<Text dimColor>{clamped.text}</Text>
					<TruncatedHint truncated={clamped.hidden} />
				</Box>
			);
		}

		default:
			return (
				<Box>
					<Text>{item.text}</Text>
				</Box>
			);
	}
}
