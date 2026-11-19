import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';

const EXPAND_HINT = ' (ctrl+o to expand)';

function toolGlyph(toolName: string): string {
	switch (toolName.toLowerCase()) {
		case 'bash':
			return '$';
		case 'read_file':
		case 'read':
		case 'fileread':
			return '▸';
		case 'edit_file':
		case 'write_file':
		case 'multi_edit':
		case 'notebook_edit':
		case 'edit':
			return '✎';
		case 'grep':
			return '⌕';
		case 'glob':
			return '✱';
		case 'agent':
		case 'task':
		case 'task_output':
			return '◆';
		case 'web_fetch':
		case 'web_search':
			return '↗';
		default:
			return '⏵';
	}
}

function formatToolDuration(durationMs?: number): string | null {
	if (durationMs == null || durationMs < 1000) {
		return null;
	}
	const seconds = durationMs / 1000;
	if (seconds < 60) {
		return `${seconds.toFixed(1)}s`;
	}
	const minutes = Math.floor(seconds / 60);
	return `${minutes}m${String(Math.round(seconds % 60)).padStart(2, '0')}s`;
}

function countLines(value: unknown): number {
	return typeof value === 'string' && value.length > 0 ? value.split('\n').length : 0;
}

function editDiffStat(
	toolName: string,
	toolInput?: Record<string, unknown>,
): {added: number; removed: number} | null {
	if (!toolInput) {
		return null;
	}
	const lower = toolName.toLowerCase();
	if (lower === 'edit_file') {
		const oldText = toolInput.old_str ?? toolInput.old_string;
		const newText = toolInput.new_str ?? toolInput.new_string;
		if (typeof oldText !== 'string' || typeof newText !== 'string') {
			return null;
		}
		return {added: countLines(newText), removed: countLines(oldText)};
	}
	if (lower === 'write_file') {
		const content = toolInput.content;
		if (typeof content !== 'string') {
			return null;
		}
		return {added: countLines(content), removed: 0};
	}
	return null;
}

export function ToolCallDisplay({
	item,
	resultItem,
	outputStyle,
}: {
	item: TranscriptItem;
	resultItem?: TranscriptItem;
	outputStyle?: string;
}): React.JSX.Element {
	const {theme} = useTheme();
	const isCodexStyle = outputStyle === 'codex';

	if (item.role === 'tool') {
		const toolName = item.tool_name ?? 'tool';
		const summary = summarizeToolInput(toolName, item.tool_input, item.text)
			.replace(/\s+/g, ' ')
			.trim();
		const duration = formatToolDuration(item.duration_ms ?? resultItem?.duration_ms);
		const diffStat = editDiffStat(toolName, item.tool_input);

		let statusNode: React.ReactNode = null;
		let errorLines: string[] | null = null;

		if (resultItem) {
			if (resultItem.is_error) {
				statusNode = isCodexStyle
					? <Text color={theme.colors.error}> error</Text>
					: <Text color={theme.colors.error}> {theme.icons.error.trim()}</Text>;
				const lines = resultItem.text.split('\n').filter((l) => l.trim());
				const maxErrLines = isCodexStyle ? 8 : 5;
				errorLines = lines.length > maxErrLines
					? [...lines.slice(0, maxErrLines), `... (${lines.length - maxErrLines} more lines)`]
					: lines;
			} else if (!isCodexStyle) {
				const lineCount = resultItem.text.split('\n').filter((l) => l.trim()).length;
				const resultLabel = lineCount > 0 ? `${lineCount}L` : theme.icons.success.trim();
				statusNode = <Text dimColor> → {resultLabel}</Text>;
			} else {
				const lineCount = resultItem.text.split('\n').filter((l) => l.trim()).length;
				statusNode = <Text dimColor>{lineCount > 0 ? ` ${lineCount}L` : ''}</Text>;
			}
		}

		const durationNode = duration ? <Text dimColor> · {duration}</Text> : null;
		const diffNode = diffStat ? (
			<Text>
				{' '}
				<Text color={theme.colors.success}>+{diffStat.added}</Text>
				<Text dimColor> </Text>
				<Text color={theme.colors.error}>-{diffStat.removed}</Text>
			</Text>
		) : null;

		const toggleHint = resultItem ? EXPAND_HINT : '';

		if (isCodexStyle) {
			return (
				<Box marginLeft={0} flexDirection="column">
					<Text dimColor>
						{`• Ran ${toolName}${summary ? ` ${summary}` : ''}`}
						{statusNode}
						{durationNode}
						{diffNode}
						{toggleHint}
					</Text>
					{errorLines?.map((line, i) => {
						const prefix = i === errorLines.length - 1 ? '└ ' : '│ ';
						return (
							<Text key={i} color={theme.colors.error}>
								{prefix}
								{line}
							</Text>
						);
					})}
				</Box>
			);
		}

		return (
			<Box marginLeft={2} flexDirection="column">
				<Text>
					<Text color={theme.colors.accent} bold>{`  ${toolGlyph(toolName)} `}</Text>
					<Text color={theme.colors.accent} bold>{toolName}</Text>
					<Text dimColor> {summary}</Text>
					{statusNode}
					{durationNode}
					{diffNode}
					<Text dimColor>{toggleHint}</Text>
				</Text>
				{errorLines?.map((line, i) => (
					<Box key={i} marginLeft={4}>
						<Text color={theme.colors.error}>{line}</Text>
					</Box>
				))}
			</Box>
		);
	}

	if (item.role === 'tool_result') {
		if (!item.is_error) {
			return <></>;
		}
		const lines = item.text.length > 0
			? item.text.split('\n').filter((l) => l.trim())
			: [''];
		const maxLines = isCodexStyle ? 8 : 5;
		const truncated = lines.length > maxLines;
		const display = truncated
			? [...lines.slice(0, maxLines), `... (${lines.length - maxLines} more lines)`]
			: lines;
		if (isCodexStyle) {
			return (
				<Box marginLeft={0} flexDirection="column">
					{display.map((line, i) => {
						const prefix = i === display.length - 1 ? '└ ' : '│ ';
						return (
							<Text key={i} color={theme.colors.error}>
								{prefix}
								{line}
							</Text>
						);
					})}
					{truncated ? <Text dimColor>{EXPAND_HINT}</Text> : null}
				</Box>
			);
		}
		return (
			<Box marginLeft={4} flexDirection="column">
				{display.map((line, i) => (
					<Text key={i} color={theme.colors.error}>{line}</Text>
				))}
				{truncated ? <Text dimColor>{EXPAND_HINT}</Text> : null}
			</Box>
		);
	}

	return <Text>{item.text}</Text>;
}

export function summarizeToolInput(toolName: string, toolInput?: Record<string, unknown>, fallback?: string): string {
	if (!toolInput) {
		return fallback?.slice(0, 80) ?? '';
	}
	const lower = toolName.toLowerCase();
	const path = toolInput.path ?? toolInput.file_path;
	if (lower === 'bash' && toolInput.command) {
		return String(toolInput.command).slice(0, 120);
	}
	if (
		lower === 'read_file' ||
		lower === 'read' ||
		lower === 'fileread' ||
		lower === 'write_file' ||
		lower === 'write' ||
		lower === 'filewrite' ||
		lower === 'edit_file' ||
		lower === 'edit' ||
		lower === 'fileedit' ||
		lower === 'notebook_edit'
	) {
		if (path != null) {
			return String(path);
		}
	}
	if (lower === 'grep' && toolInput.pattern) {
		return `/${String(toolInput.pattern)}/`;
	}
	if (lower === 'glob' && toolInput.pattern) {
		return String(toolInput.pattern);
	}
	if (lower === 'agent' && toolInput.description) {
		return String(toolInput.description);
	}
	const entries = Object.entries(toolInput);
	if (entries.length > 0) {
		const [key, val] = entries[0];
		return `${key}=${String(val).slice(0, 60)}`;
	}
	const text = fallback ?? '';
	return text.length > 80 ? `${text.slice(0, 79)}…` : text;
}
