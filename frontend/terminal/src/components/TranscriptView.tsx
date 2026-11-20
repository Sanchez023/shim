import React from 'react';
import {Box, Text} from 'ink';
import stringWidth from 'string-width';

import {
	CLAUDE_DIFF,
	highlightClaudeToLines,
	languageForPath,
} from '../highlight.js';
import {useTerminalSize} from '../hooks/useTerminalSize.js';
import {useTheme} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';
import {groupToolPairs} from './ConversationView.js';
import {
	buildEditPreview,
	buildToolDiffPreview,
	summarizeToolInput,
	toolVerb,
} from './ToolCallDisplay.js';

export type TranscriptLineKind =
	| 'user'
	| 'assistant'
	| 'thinking'
	| 'tool'
	| 'result'
	| 'error'
	| 'diff_add'
	| 'diff_del'
	| 'diff_context'
	| 'system'
	| 'status'
	| 'log';

export type TranscriptLine = {
	text: string;
	kind: TranscriptLineKind;
	/** Language used to syntax-highlight a diff line. */
	language?: string;
};

const MAX_RESULT_LINES = 2000;
const MAX_TOTAL_LINES = 4000;

function pushText(lines: TranscriptLine[], text: string, kind: TranscriptLineKind): void {
	for (const line of text.split('\n')) {
		lines.push({text: line, kind});
	}
}

/**
 * Flatten the conversation into full-text lines for the transcript view.
 * Unlike the inline conversation view, tool results are not preview-truncated:
 * `ctrl+o` in Claude Code opens a full transcript, so this is where complete
 * stdout belongs.
 */
function isToolPair(group: TranscriptItem | readonly [TranscriptItem, TranscriptItem]): group is readonly [TranscriptItem, TranscriptItem] {
	return Array.isArray(group);
}

function pushToolEntry(
	lines: TranscriptLine[],
	item: TranscriptItem,
	result?: TranscriptItem,
): void {
	const toolName = item.tool_name ?? 'tool';
	const summary = summarizeToolInput(toolName, item.tool_input, item.text)
		.replace(/\s+/g, ' ')
		.trim();
	const language = languageForPath(
		typeof item.tool_input?.path === 'string'
			? item.tool_input.path
			: typeof item.tool_input?.file_path === 'string'
				? item.tool_input.file_path
				: undefined,
	);
	pushText(lines, `⏺ ${toolVerb(toolName)}${summary ? `(${summary})` : ''}`, 'tool');
	// A failed edit did not apply its diff; the error result follows instead.
	if (result?.is_error) {
		return;
	}
	const preview = result
		? buildEditPreview(toolName, item.tool_input, result, 1_000)
		: buildToolDiffPreview(toolName, item.tool_input, 1_000);
	if (!preview) {
		return;
	}
	for (const row of preview.rows) {
		const prefix = row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' ';
		lines.push({
			text: `${prefix}${row.text}`,
			kind:
				row.kind === 'add'
					? 'diff_add'
					: row.kind === 'del'
						? 'diff_del'
						: 'diff_context',
			language,
		});
	}
	if (preview.hidden > 0) {
		lines.push({text: `… ${preview.hidden} more diff lines`, kind: 'log'});
	}
}

function pushToolResult(lines: TranscriptLine[], result: TranscriptItem): void {
	const resultLines = result.text.split('\n').slice(0, MAX_RESULT_LINES);
	pushText(lines, resultLines.join('\n'), result.is_error ? 'error' : 'result');
}

export function buildTranscriptLines(
	items: TranscriptItem[],
	assistantBuffer = '',
): TranscriptLine[] {
	const lines: TranscriptLine[] = [];

	// Group first so a result that arrived out of order still prints under its
	// own tool call, and the diff can use its metadata.
	for (const group of groupToolPairs(items)) {
		if (isToolPair(group)) {
			const [tool, result] = group;
			pushToolEntry(lines, tool, result);
			pushToolResult(lines, result);
			continue;
		}
		const item = group;
		switch (item.role) {
			case 'user':
				pushText(lines, `> ${item.text}`, 'user');
				break;
			case 'assistant':
				pushText(lines, item.text, 'assistant');
				break;
			case 'thinking':
				pushText(lines, `✻ ${item.text}`, 'thinking');
				break;
			case 'tool':
				pushToolEntry(lines, item);
				break;
			case 'tool_result':
				pushToolResult(lines, item);
				break;
			case 'system':
				pushText(lines, `[system] ${item.text}`, 'system');
				break;
			case 'status':
				pushText(lines, item.text, 'status');
				break;
			case 'log':
				pushText(lines, item.text, 'log');
				break;
		}
	}

	if (assistantBuffer) {
		pushText(lines, assistantBuffer, 'assistant');
	}

	if (lines.length > MAX_TOTAL_LINES) {
		const dropped = lines.length - MAX_TOTAL_LINES;
		return [
			{text: `... (${dropped} earlier lines truncated)`, kind: 'log'},
			...lines.slice(-MAX_TOTAL_LINES),
		];
	}
	return lines;
}

const KIND_COLORS: Record<TranscriptLineKind, 'primary' | 'secondary' | 'accent' | 'success' | 'warning' | 'error' | 'info'> = {
	user: 'secondary',
	assistant: 'success',
	thinking: 'secondary',
	tool: 'accent',
	result: 'info',
	error: 'error',
	diff_add: 'success',
	diff_del: 'error',
	diff_context: 'info',
	system: 'warning',
	status: 'info',
	log: 'info',
};

export function TranscriptView({
	lines,
	height,
	offset,
	title = 'Transcript',
	maxWidth,
}: {
	lines: TranscriptLine[];
	height: number;
	offset: number;
	title?: string;
	/** Width of the conversation pane when a sidebar narrows it. */
	maxWidth?: number;
}): React.JSX.Element {
	const {theme} = useTheme();
	const {columns} = useTerminalSize();
	const targetWidth = Math.max(10, (maxWidth ?? columns) - 2);
	const maxOffset = Math.max(0, lines.length - height);
	const clampedOffset = Math.min(Math.max(0, offset), maxOffset);
	const start = Math.max(0, lines.length - height - clampedOffset);
	const visible = lines.slice(start, start + height);
	const padding = Math.max(0, height - visible.length);

	return (
		<Box flexDirection="column" height={height + 1} overflowY="hidden" paddingX={1}>
			<Text color={theme.colors.primary} bold>
				{title}
				<Text dimColor>
					{'  '}esc / q / ctrl+o close{'  '}↑/↓ scroll{'  '}pgup/pgdn page{'  '}
				</Text>
			</Text>
			{visible.map((line, index) => {
				const isDiff =
					line.kind === 'diff_add' ||
					line.kind === 'diff_del' ||
					line.kind === 'diff_context';
				const isDiffChange = line.kind === 'diff_add' || line.kind === 'diff_del';
				const background = isDiffChange
					? line.kind === 'diff_add'
						? CLAUDE_DIFF.addedBackground
						: CLAUDE_DIFF.removedBackground
					: null;
				const padding = background
					? Math.max(0, targetWidth - stringWidth(line.text || ' '))
					: 0;
				const marker = isDiff ? line.text.slice(0, 1) : '';
				const body = isDiff ? line.text.slice(1) : line.text;
				const spans =
					isDiff && body
						? (highlightClaudeToLines(body, line.language)[0] ?? [])
						: [];
				return (
					<Text
						key={start + index}
						color={
							isDiff
								? CLAUDE_DIFF.text
								: theme.colors[KIND_COLORS[line.kind]]
						}
						dimColor={
							line.kind === 'result' ||
							line.kind === 'log' ||
							line.kind === 'thinking' ||
							line.kind === 'diff_context'
						}
						backgroundColor={background ?? undefined}
						wrap="truncate"
					>
						{isDiff ? (
							<>
								<Text
									color={
										line.kind === 'diff_add'
											? CLAUDE_DIFF.addedWord
											: line.kind === 'diff_del'
												? CLAUDE_DIFF.removedWord
												: undefined
									}
									bold={isDiffChange}
									dimColor={!isDiffChange}
								>
									{marker}
								</Text>
								{spans.length > 0
									? spans.map((span, spanIndex) => (
											<Text
												key={spanIndex}
												color={span.color}
												bold={span.bold}
												italic={span.italic}
												dimColor={span.dim}
												underline={span.underline}
											>
												{span.text}
											</Text>
										))
									: body}
							</>
						) : (
							line.text || ' '
						)}
						{padding > 0 ? ' '.repeat(padding) : ''}
					</Text>
				);
			})}
			{Array.from({length: padding}, (_, index) => (
				<Text key={`pad-${index}`}> </Text>
			))}
		</Box>
	);
}
