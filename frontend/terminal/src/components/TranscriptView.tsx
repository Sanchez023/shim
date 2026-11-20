import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';
import {buildEditPreview, buildToolDiffPreview, summarizeToolInput} from './ToolCallDisplay.js';

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
export function buildTranscriptLines(
	items: TranscriptItem[],
	assistantBuffer = '',
): TranscriptLine[] {
	const lines: TranscriptLine[] = [];

	for (let index = 0; index < items.length; index += 1) {
		const item = items[index];
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
			case 'tool': {
				const toolName = item.tool_name ?? 'tool';
				const summary = summarizeToolInput(toolName, item.tool_input, item.text)
					.replace(/\s+/g, ' ')
					.trim();
				pushText(lines, `⏺ ${toolName}${summary ? ` ${summary}` : ''}`, 'tool');
				// The full transcript is where the complete edit diff belongs.
				// Skip it when the following result reports a failure, since the
				// diff was not applied.
				const next = items[index + 1];
				const result = next?.role === 'tool_result' ? next : undefined;
				if (!result || !result.is_error) {
					const preview = result
						? buildEditPreview(toolName, item.tool_input, result, 1_000)
						: buildToolDiffPreview(toolName, item.tool_input, 1_000);
					if (preview) {
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
							});
						}
						if (preview.hidden > 0) {
							lines.push({
								text: `… ${preview.hidden} more diff lines`,
								kind: 'log',
							});
						}
					}
				}
				break;
			}
			case 'tool_result': {
				const resultLines = item.text.split('\n').slice(0, MAX_RESULT_LINES);
				pushText(lines, resultLines.join('\n'), item.is_error ? 'error' : 'result');
				break;
			}
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
}: {
	lines: TranscriptLine[];
	height: number;
	offset: number;
	title?: string;
}): React.JSX.Element {
	const {theme} = useTheme();
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
			{visible.map((line, index) => (
				<Text
					key={start + index}
					color={theme.colors[KIND_COLORS[line.kind]]}
					dimColor={
						line.kind === 'result' ||
						line.kind === 'log' ||
						line.kind === 'thinking' ||
						line.kind === 'diff_context'
					}
					wrap="truncate"
				>
					{line.text || ' '}
				</Text>
			))}
			{Array.from({length: padding}, (_, index) => (
				<Text key={`pad-${index}`}> </Text>
			))}
		</Box>
	);
}
