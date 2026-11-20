import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';

const EXPAND_HINT = ' (ctrl+o to expand)';

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

export type ToolDiffRow = {kind: 'add' | 'del' | 'context'; text: string};

export type ToolDiffPreview = {
	rows: ToolDiffRow[];
	hidden: number;
	added: number;
	removed: number;
};

/** Line-level preview of an edit, shown under the tool row. */
export const TOOL_DIFF_PREVIEW_ROWS = 8;

function splitTextLines(text: string): string[] {
	const normalized = text.replace(/\r\n/g, '\n');
	const lines = normalized.split('\n');
	// A trailing newline is not an extra line in the diff.
	if (lines.length > 0 && lines[lines.length - 1] === '') {
		lines.pop();
	}
	return lines;
}

/**
 * Line diff for the old/new strings an edit tool received.
 *
 * The snippets are usually small, but a large replacement is capped to a
 * delete-all/add-all shape rather than allocating a giant LCS table.
 */
function diffLines(oldText: string, newText: string): ToolDiffRow[] {
	const before = splitTextLines(oldText);
	const after = splitTextLines(newText);
	if (before.length * after.length > 200_000) {
		return [
			...before.map((text): ToolDiffRow => ({kind: 'del', text})),
			...after.map((text): ToolDiffRow => ({kind: 'add', text})),
		];
	}
	const n = before.length;
	const m = after.length;
	const table: Uint32Array[] = Array.from(
		{length: n + 1},
		() => new Uint32Array(m + 1),
	);
	for (let i = n - 1; i >= 0; i -= 1) {
		for (let j = m - 1; j >= 0; j -= 1) {
			table[i][j] =
				before[i] === after[j]
					? table[i + 1][j + 1] + 1
					: Math.max(table[i + 1][j], table[i][j + 1]);
		}
	}
	const rows: ToolDiffRow[] = [];
	let i = 0;
	let j = 0;
	while (i < n && j < m) {
		if (before[i] === after[j]) {
			rows.push({kind: 'context', text: before[i]});
			i += 1;
			j += 1;
		} else if (table[i + 1][j] >= table[i][j + 1]) {
			rows.push({kind: 'del', text: before[i]});
			i += 1;
		} else {
			rows.push({kind: 'add', text: after[j]});
			j += 1;
		}
	}
	while (i < n) {
		rows.push({kind: 'del', text: before[i]});
		i += 1;
	}
	while (j < m) {
		rows.push({kind: 'add', text: after[j]});
		j += 1;
	}
	return rows;
}

function compactDiffRows(
	rows: ToolDiffRow[],
	maxRows: number,
): {rows: ToolDiffRow[]; hidden: number} {
	if (rows.length <= maxRows) {
		return {rows, hidden: 0};
	}
	// Keep the head and the tail: a replacement starts with deletions and ends
	// with additions, so taking only the first N rows would hide the new text.
	const head = rows.slice(0, Math.ceil(maxRows / 2));
	const tail = rows.slice(rows.length - Math.floor(maxRows / 2));
	return {rows: [...head, ...tail], hidden: rows.length - maxRows};
}

/**
 * Build the inline diff shown after a file edit.
 *
 * `edit_file` carries the exact old/new snippets, so a real line diff is
 * possible.  `write_file` only carries the new content (the overwritten text
 * is not in the tool input), so its lines are shown as additions.
 */
export function buildToolDiffPreview(
	toolName: string,
	toolInput?: Record<string, unknown>,
	maxRows = TOOL_DIFF_PREVIEW_ROWS,
): ToolDiffPreview | null {
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
		const rows = diffLines(oldText, newText);
		const added = rows.filter((row) => row.kind === 'add').length;
		const removed = rows.filter((row) => row.kind === 'del').length;
		if (added === 0 && removed === 0) {
			return null;
		}
		return {...compactDiffRows(rows, Math.max(2, maxRows)), added, removed};
	}
	if (lower === 'write_file') {
		const content = toolInput.content;
		if (typeof content !== 'string' || content.length === 0) {
			return null;
		}
		const lines = splitTextLines(content);
		// A written file can be arbitrarily large; only build the rows shown.
		const limit = Math.max(2, maxRows);
		const shown = lines.slice(0, limit);
		return {
			rows: shown.map((text): ToolDiffRow => ({kind: 'add', text})),
			hidden: Math.max(0, lines.length - shown.length),
			added: lines.length,
			removed: 0,
		};
	}
	return null;
}

/** Lines of tool output shown under a completed row. */
export const TOOL_OUTPUT_PREVIEW_ROWS = 5;

export type ToolOutputPreview = {
	lines: string[];
	hidden: number;
	side: 'head' | 'tail';
};

const ANSI_ESCAPE = /\u001B\[[0-9;?]*[ -/]*[@-~]/g;
const NO_OUTPUT_PREVIEW_TOOLS = new Set([
	'edit_file',
	'write_file',
	'multi_edit',
	'notebook_edit',
]);

/**
 * A short block of the tool's actual output.
 *
 * Shell commands put their result at the end (`pytest` finishes with
 * "5 passed in 12.3s"), so bash previews keep the tail; file/search tools are
 * read from the top.  The full output stays in `ctrl+o`.
 */
export function buildToolOutputPreview(
	toolName: string,
	resultItem?: TranscriptItem,
	maxRows = TOOL_OUTPUT_PREVIEW_ROWS,
): ToolOutputPreview | null {
	if (!resultItem || resultItem.is_error) {
		return null;
	}
	const lower = toolName.toLowerCase();
	if (NO_OUTPUT_PREVIEW_TOOLS.has(lower)) {
		return null;
	}
	const text = resultItem.text.replace(ANSI_ESCAPE, '').replace(/\r\n/g, '\n');
	const all = text
		.split('\n')
		.map((line) => line.replace(/\r$/, ''))
		.filter((line) => line.trim() !== '');
	if (all.length === 0) {
		return null;
	}
	const limit = Math.max(1, maxRows);
	if (lower === 'bash') {
		const start = Math.max(0, all.length - limit);
		return {lines: all.slice(start), hidden: start, side: 'tail'};
	}
	return {
		lines: all.slice(0, limit),
		hidden: Math.max(0, all.length - limit),
		side: 'head',
	};
}

type InlineDiffLine = {
	kind: 'add' | 'del' | 'context';
	text: string;
	oldLine?: number;
	newLine?: number;
};

export type InlineDiffPreview = {
	rows: InlineDiffLine[];
	hidden: number;
	added: number;
	removed: number;
};

/** Split a unified diff into hunks while tracking the real file line numbers. */
function parseDiffHunks(diffText: string): InlineDiffLine[][] {
	const hunks: InlineDiffLine[][] = [];
	let current: InlineDiffLine[] | null = null;
	let oldLine = 0;
	let newLine = 0;
	for (const raw of diffText.split('\n')) {
		if (!raw || raw.startsWith('+++') || raw.startsWith('---') || raw.startsWith('\\')) {
			continue;
		}
		if (raw.startsWith('@@')) {
			const match = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
			if (match) {
				oldLine = Number(match[1]);
				newLine = Number(match[2]);
			}
			current = [];
			hunks.push(current);
			continue;
		}
		if (!current) {
			current = [];
			hunks.push(current);
		}
		if (raw.startsWith('+')) {
			current.push({kind: 'add', text: raw.slice(1), newLine});
			newLine += 1;
		} else if (raw.startsWith('-')) {
			current.push({kind: 'del', text: raw.slice(1), oldLine});
			oldLine += 1;
		} else {
			current.push({
				kind: 'context',
				text: raw.startsWith(' ') ? raw.slice(1) : raw,
				oldLine,
				newLine,
			});
			oldLine += 1;
			newLine += 1;
		}
	}
	return hunks.filter((hunk) => hunk.length > 0);
}

/**
 * Diff shown under an edit row.
 *
 * Prefer the unified diff the tool attached to its result: it carries the real
 * file line numbers and context lines.  Backends that do not send metadata fall
 * back to a snippet-only diff of the tool input.
 */
export function buildEditPreview(
	toolName: string,
	toolInput: Record<string, unknown> | undefined,
	resultItem: TranscriptItem | undefined,
	maxRows: number,
): InlineDiffPreview | null {
	if (!resultItem || resultItem.is_error) {
		return null;
	}
	const metadata = resultItem.metadata ?? {};
	const diffText = typeof metadata.diff === 'string' ? metadata.diff : '';
	if (diffText.trim()) {
		const hunks = parseDiffHunks(diffText);
		const all = hunks.flat();
		if (all.length === 0) {
			return null;
		}
		const added =
			typeof metadata.diff_added === 'number'
				? metadata.diff_added
				: all.filter((row) => row.kind === 'add').length;
		const removed =
			typeof metadata.diff_removed === 'number'
				? metadata.diff_removed
				: all.filter((row) => row.kind === 'del').length;
		const limit = Math.max(2, maxRows);
		const firstHunk = hunks[0] ?? all;
		if (all.length <= limit) {
			return {rows: all, hidden: 0, added, removed};
		}
		if (firstHunk.length <= limit) {
			return {
				rows: firstHunk,
				hidden: all.length - firstHunk.length,
				added,
				removed,
			};
		}
		const head = firstHunk.slice(0, Math.ceil(limit / 2));
		const tail = firstHunk.slice(firstHunk.length - Math.floor(limit / 2));
		return {
			rows: [...head, ...tail],
			hidden: all.length - limit,
			added,
			removed,
		};
	}
	const fallback = buildToolDiffPreview(toolName, toolInput, maxRows);
	if (!fallback) {
		return null;
	}
	return {
		rows: fallback.rows.map((row) => ({kind: row.kind, text: row.text})),
		hidden: fallback.hidden,
		added: fallback.added,
		removed: fallback.removed,
	};
}

const TOOL_VERBS: Record<string, string> = {
	bash: 'Bash',
	read_file: 'Read',
	read: 'Read',
	fileread: 'Read',
	edit_file: 'Update',
	write_file: 'Write',
	multi_edit: 'Update',
	notebook_edit: 'Update',
	grep: 'Grep',
	glob: 'Glob',
	agent: 'Agent',
	task: 'Task',
	web_fetch: 'WebFetch',
	web_search: 'WebSearch',
};

function toolVerb(toolName: string): string {
	const lower = toolName.toLowerCase();
	const known = TOOL_VERBS[lower];
	if (known) {
		return known;
	}
	return toolName.length > 0 ? toolName[0].toUpperCase() + toolName.slice(1) : 'Tool';
}

function pluralLines(count: number): string {
	return `${count} line${count === 1 ? '' : 's'}`;
}

function toolResultOverview(toolName: string, resultItem?: TranscriptItem): string | null {
	if (!resultItem || resultItem.is_error) {
		return null;
	}
	const lower = toolName.toLowerCase();
	const text = resultItem.text.trim();
	const lineCount = text === '' ? 0 : text.split('\n').filter((line) => line.trim()).length;
	switch (lower) {
		case 'read_file':
		case 'read':
		case 'fileread':
			return lineCount > 0 ? `Read ${lineCount} line${lineCount === 1 ? '' : 's'}` : 'Read';
		case 'grep':
			return text === '(no matches)' ? 'No matches' : `${lineCount} match${lineCount === 1 ? '' : 'es'}`;
		case 'glob':
			return `${lineCount} file${lineCount === 1 ? '' : 's'}`;
		case 'bash':
			return lineCount > 0 ? `${lineCount} line${lineCount === 1 ? '' : 's'}` : 'Done';
		case 'edit_file':
		case 'multi_edit':
		case 'notebook_edit':
			return 'Updated';
		case 'write_file':
			return 'Wrote';
		default:
			return lineCount > 0 ? `${lineCount} line${lineCount === 1 ? '' : 's'}` : 'Done';
	}
}

function errorDisplayLines(resultItem: TranscriptItem, isCodexStyle: boolean): string[] {
	const lines = resultItem.text.split('\n').filter((line) => line.trim());
	const maxLines = isCodexStyle ? 8 : 5;
	return lines.length > maxLines
		? [...lines.slice(0, maxLines), `... (${lines.length - maxLines} more lines)`]
		: lines;
}

/** Render a Claude-style diff block: line numbers, marker, colored content. */
function renderEditDiffRows(
	rows: InlineDiffLine[],
	theme: ReturnType<typeof useTheme>['theme'],
): React.JSX.Element {
	const numbers = rows
		.flatMap((row) => [row.oldLine, row.newLine])
		.filter((line): line is number => typeof line === 'number');
	const width = Math.max(3, ...numbers.map((line) => String(line).length));
	return (
		<Box flexDirection="column">
			{rows.map((row, index) => {
				const marker = row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' ';
				const lineNo = row.kind === 'del' ? row.oldLine : row.newLine;
				return (
					<Text
						key={index}
						color={
							row.kind === 'add'
								? theme.colors.success
								: row.kind === 'del'
									? theme.colors.error
									: undefined
						}
						dimColor={row.kind === 'context'}
					>
						{'     '}
						{lineNo != null ? `${String(lineNo).padStart(width)} ` : ''}
						{marker}
						{row.text}
					</Text>
				);
			})}
		</Box>
	);
}

export function ToolCallDisplay({
	item,
	resultItem,
	outputStyle,
	maxDiffRows = TOOL_DIFF_PREVIEW_ROWS,
	maxOutputRows = TOOL_OUTPUT_PREVIEW_ROWS,
}: {
	item: TranscriptItem;
	resultItem?: TranscriptItem;
	outputStyle?: string;
	/** Rows the inline edit diff may occupy in the live frame. */
	maxDiffRows?: number;
	/** Rows the inline shell-output preview may occupy in the live frame. */
	maxOutputRows?: number;
}): React.JSX.Element {
	const {theme} = useTheme();
	const isCodexStyle = outputStyle === 'codex';

	if (item.role === 'tool') {
		const toolName = item.tool_name ?? 'tool';
		const summary = summarizeToolInput(toolName, item.tool_input, item.text)
			.replace(/\s+/g, ' ')
			.trim();
		const duration = formatToolDuration(item.duration_ms ?? resultItem?.duration_ms);
		const editPreview = buildEditPreview(
			toolName,
			item.tool_input,
			resultItem,
			maxDiffRows,
		);
		// Claude renders shell output inline; file/search tools keep a summary
		// because their content is already represented by the count.
		const outputPreview =
			!editPreview && toolName.toLowerCase() === 'bash'
				? buildToolOutputPreview(toolName, resultItem, maxOutputRows)
				: null;
		const overview = toolResultOverview(toolName, resultItem);
		const durationNode = duration ? <Text dimColor> · {duration}</Text> : null;
		const verb = toolVerb(toolName);

		if (isCodexStyle) {
			return (
				<Box marginLeft={0} flexDirection="column">
					<Text dimColor>
						{`• Ran ${toolName}${summary ? ` ${summary}` : ''}`}
						{overview ? ` · ${overview}` : ''}
						{duration ? ` · ${duration}` : ''}
						{resultItem ? EXPAND_HINT : ''}
					</Text>
					{editPreview ? (
						<Box marginLeft={2} flexDirection="column">
							{editPreview.rows.map((row, index) => (
								<Text
									key={index}
									color={
										row.kind === 'add'
											? theme.colors.success
											: row.kind === 'del'
												? theme.colors.error
												: undefined
									}
									dimColor={row.kind === 'context'}
								>
									{row.kind === 'add' ? '+' : row.kind === 'del' ? '-' : ' '}
									{row.text}
								</Text>
							))}
							{editPreview.hidden > 0 ? (
								<Text dimColor>
									… {editPreview.hidden} more diff lines · ctrl+o full transcript
								</Text>
							) : null}
						</Box>
					) : null}
					{outputPreview ? (
						<Box marginLeft={2} flexDirection="column">
							{outputPreview.lines.map((line, index) => (
								<Text key={index} dimColor>
									{line}
								</Text>
							))}
							{outputPreview.hidden > 0 ? (
								<Text dimColor>
									… {outputPreview.hidden} earlier lines · ctrl+o full transcript
								</Text>
							) : null}
						</Box>
					) : null}
					{resultItem?.is_error
						? errorDisplayLines(resultItem, true).map((line, index, all) => (
								<Text key={index} color={theme.colors.error}>
									{index === all.length - 1 ? '└ ' : '│ '}
									{line}
								</Text>
							))
						: null}
				</Box>
			);
		}

		return (
			<Box marginLeft={2} flexDirection="column">
				<Text>
					<Text color={theme.colors.accent} bold>{theme.icons.assistant}</Text>
					<Text color={theme.colors.accent} bold>{verb}</Text>
					{summary ? <Text dimColor>({summary})</Text> : null}
					{durationNode}
				</Text>
				{editPreview ? (
					<Box flexDirection="column">
						<Text dimColor>
							{'  ⎿  '}
							Added {pluralLines(editPreview.added)}, removed{' '}
							{pluralLines(editPreview.removed)}
						</Text>
						{renderEditDiffRows(editPreview.rows, theme)}
						{editPreview.hidden > 0 ? (
							<Text dimColor>
								{'     '}… {editPreview.hidden} more diff lines · ctrl+o full transcript
							</Text>
						) : null}
					</Box>
				) : outputPreview ? (
					<Box flexDirection="column">
						{outputPreview.lines.map((line, index) => (
							<Text key={index} dimColor>
								{index === 0 ? '  ⎿  ' : '     '}
								{line}
							</Text>
						))}
						{outputPreview.hidden > 0 ? (
							<Text dimColor>
								{'     '}… {outputPreview.hidden} earlier lines · ctrl+o full transcript
							</Text>
						) : null}
					</Box>
				) : resultItem?.is_error ? null : overview ? (
					<Text dimColor>
						{'  ⎿  '}
						{overview}
					</Text>
				) : null}
				{resultItem?.is_error
					? errorDisplayLines(resultItem, false).map((line, index) => (
							<Text key={index} color={theme.colors.error}>
								{index === 0 ? '  ⎿  ' : '     '}
								{line}
							</Text>
						))
					: null}
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
