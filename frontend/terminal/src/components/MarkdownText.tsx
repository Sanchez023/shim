import React from 'react';
import {Box, Text} from 'ink';

import {useTerminalSize} from '../hooks/useTerminalSize.js';
import {lexer, type Token, type Tokens} from 'marked';
import stringWidth from 'string-width';

import {highlightToLines} from '../highlight.js';
import {useTheme} from '../theme/ThemeContext.js';
import type {ThemeConfig} from '../theme/builtinThemes.js';

function getInlineFallbackText(token: Token): string {
	if ('text' in token && typeof token.text === 'string') {
		return token.text;
	}

	return token.raw;
}

function getInlineDisplayText(tokens: Token[] | undefined): string {
	if (!tokens || tokens.length === 0) {
		return '';
	}

	return tokens.map((token) => {
		switch (token.type) {
			case 'text': {
				const t = token as Tokens.Text;
				return t.tokens && t.tokens.length > 0 ? getInlineDisplayText(t.tokens) : t.text;
			}
			case 'strong':
			case 'em':
			case 'del':
				return getInlineDisplayText((token as Tokens.Strong | Tokens.Em | Tokens.Del).tokens);
			case 'codespan':
				return (token as Tokens.Codespan).text;
			case 'link': {
				const l = token as Tokens.Link;
				return l.text || l.href;
			}
			case 'image': {
				const image = token as Tokens.Image;
				return image.text || image.href;
			}
			case 'br':
				return '\n';
			case 'escape':
				return (token as Tokens.Escape).text;
			default:
				return getInlineFallbackText(token);
		}
	}).join('');
}

function getTableCellDisplayText(cell: Tokens.TableCell): string {
	const displayText = getInlineDisplayText(cell.tokens);
	return displayText.length > 0 ? displayText : cell.text;
}

// Inline token renderer — returns an array of <Text> elements.
function renderInline(tokens: Token[] | undefined, theme: ThemeConfig): React.ReactNode {
	if (!tokens || tokens.length === 0) {
		return null;
	}
	return tokens.map((token, i) => {
		switch (token.type) {
			case 'text': {
				const t = token as Tokens.Text;
				// Text tokens can themselves contain inline children, such as list items.
				if (t.tokens && t.tokens.length > 0) {
					return <React.Fragment key={i}>{renderInline(t.tokens, theme)}</React.Fragment>;
				}
				return <Text key={i}>{t.text}</Text>;
			}
			case 'strong': {
				const s = token as Tokens.Strong;
				return (
					<Text key={i} bold>
						{renderInline(s.tokens, theme)}
					</Text>
				);
			}
			case 'em': {
				const e = token as Tokens.Em;
				return (
					<Text key={i} italic>
						{renderInline(e.tokens, theme)}
					</Text>
				);
			}
			case 'del': {
				const d = token as Tokens.Del;
				return (
					<Text key={i} strikethrough>
						{renderInline(d.tokens, theme)}
					</Text>
				);
			}
			case 'codespan': {
				const c = token as Tokens.Codespan;
				return (
					<Text key={i} color={theme.colors.accent}>
						{c.text}
					</Text>
				);
			}
			case 'link': {
				const l = token as Tokens.Link;
				const label = l.text || l.href;
				return (
					<Text key={i} color={theme.colors.info}>
						{label}
					</Text>
				);
			}
			case 'image': {
				const image = token as Tokens.Image;
				return <Text key={i}>{image.text || image.href}</Text>;
			}
			case 'br':
				return <Text key={i}>{'\n'}</Text>;
			case 'escape': {
				const es = token as Tokens.Escape;
				return <Text key={i}>{es.text}</Text>;
			}
			default:
				return <Text key={i}>{getInlineFallbackText(token)}</Text>;
		}
	});
}

function renderBlocks(
	tokens: Token[] | undefined,
	theme: ThemeConfig,
	maxWidth: number,
): React.ReactNode {
	if (!tokens || tokens.length === 0) {
		return null;
	}

	return tokens.map((token, i) => (
		<MarkdownBlock key={i} token={token} theme={theme} maxWidth={maxWidth} />
	));
}

const MIN_COLUMN_WIDTH = 4;

function tableBorder(widths: number[], left: string, join: string, right: string): string {
	return left + widths.map((width) => '─'.repeat(width + 2)).join(join) + right;
}

/** Split a cell value into display lines no wider than ``width`` columns. */
function wrapCellText(text: string, width: number): string[] {
	const limit = Math.max(1, width);
	const lines: string[] = [];

	for (const paragraph of text.split('\n')) {
		let line = '';
		let lineWidth = 0;
		const flush = (): void => {
			lines.push(line);
			line = '';
			lineWidth = 0;
		};

		for (const word of paragraph.split(/(\s+)/)) {
			if (word.length === 0 || /^\s+$/.test(word)) {
				continue;
			}
			const wordWidth = stringWidth(word);
			const separator = lineWidth > 0 ? 1 : 0;
			if (lineWidth + separator + wordWidth <= limit) {
				if (separator > 0) {
					line += ' ';
					lineWidth += 1;
				}
				line += word;
				lineWidth += wordWidth;
				continue;
			}
			if (lineWidth > 0) {
				flush();
			}
			if (wordWidth <= limit) {
				line = word;
				lineWidth = wordWidth;
				continue;
			}
			// The word itself is wider than the column (URLs, CJK runs, ...).
			for (const char of [...word]) {
				const charWidth = stringWidth(char);
				if (lineWidth + charWidth > limit && lineWidth > 0) {
					flush();
				}
				line += char;
				lineWidth += charWidth;
			}
		}
		flush();
	}

	return lines.length > 0 ? lines : [''];
}

function StackedTable({
	headerCells,
	rows,
	theme,
}: {
	headerCells: string[];
	rows: string[][];
	theme: ThemeConfig;
}): React.JSX.Element {
	return (
		<Box flexDirection="column" marginTop={1} marginLeft={1}>
			{rows.map((row, rowIndex) => (
				<Box
					key={rowIndex}
					flexDirection="column"
					marginBottom={rowIndex === rows.length - 1 ? 0 : 1}
				>
					{row.map((cell, cellIndex) => (
						<Text key={cellIndex}>
							<Text color={theme.colors.primary} bold>
								{headerCells[cellIndex]?.trim() || `Column ${cellIndex + 1}`}:{' '}
							</Text>
							<Text>{cell}</Text>
						</Text>
					))}
				</Box>
			))}
		</Box>
	);
}

function MarkdownTable({
	table,
	theme,
	maxWidth,
}: {
	table: Tokens.Table;
	theme: ThemeConfig;
	maxWidth: number;
}): React.JSX.Element {
	const headerTexts = table.header.map(getTableCellDisplayText);
	const rowTexts = table.rows.map((row) => row.map(getTableCellDisplayText));
	// Use stringWidth for correct CJK and wide-char column widths.
	const colCount = Math.max(1, table.header.length, ...table.rows.map((row) => row.length));
	const headerCells = Array.from({length: colCount}, (_, c) => headerTexts[c] ?? '');
	const rows = rowTexts.map((row) => Array.from({length: colCount}, (_, c) => row[c] ?? ''));
	const naturalWidths = headerCells.map((cellText, c) =>
		rows.reduce((width, row) => Math.max(width, stringWidth(row[c] ?? '')), stringWidth(cellText)),
	);
	const borderOverhead = 3 * colCount + 1;
	const naturalTotal = naturalWidths.reduce((sum, width) => sum + width, 0) + borderOverhead;

	if (naturalTotal <= maxWidth) {
		const colWidths = naturalWidths;
		const trailing = (cellText: string, c: number): string =>
			' '.repeat(Math.max(0, (colWidths[c] ?? 0) - stringWidth(cellText)));
		return (
			<Box flexDirection="column" marginTop={1} marginLeft={1}>
				<Text color={theme.colors.muted}>{tableBorder(colWidths, '┌', '┬', '┐')}</Text>
				<Text>
					<Text color={theme.colors.muted}>{'│'}</Text>
					{table.header.map((cell, c) => (
						<React.Fragment key={c}>
							<Text color={theme.colors.primary} bold>
								{' '}{renderInline(cell.tokens, theme)}{trailing(headerTexts[c] ?? '', c)}{' '}
							</Text>
							<Text color={theme.colors.muted}>{'│'}</Text>
						</React.Fragment>
					))}
				</Text>
				<Text color={theme.colors.muted}>{tableBorder(colWidths, '├', '┼', '┤')}</Text>
				{table.rows.map((row, i) => (
					<Text key={i}>
						<Text color={theme.colors.muted}>{'│'}</Text>
						{row.map((cell, c) => (
							<React.Fragment key={c}>
								<Text>
									{' '}{renderInline(cell.tokens, theme)}{trailing(rowTexts[i]?.[c] ?? '', c)}{' '}
								</Text>
								<Text color={theme.colors.muted}>{'│'}</Text>
							</React.Fragment>
						))}
					</Text>
				))}
				<Text color={theme.colors.muted}>{tableBorder(colWidths, '└', '┴', '┘')}</Text>
			</Box>
		);
	}

	// The table is wider than the viewport. Ink would wrap the row <Text> at the
	// terminal edge, splitting borders across lines; instead cap every column so
	// the box fits and wrap cell contents inside it.
	const minimumTotal =
		naturalWidths.reduce((sum, width) => sum + Math.min(width, MIN_COLUMN_WIDTH), 0) +
		borderOverhead;
	if (minimumTotal > maxWidth) {
		return <StackedTable headerCells={headerCells} rows={rows} theme={theme} />;
	}

	let low = MIN_COLUMN_WIDTH;
	let high = Math.max(MIN_COLUMN_WIDTH, ...naturalWidths);
	while (low < high) {
		const mid = Math.ceil((low + high) / 2);
		const total =
			naturalWidths.reduce((sum, width) => sum + Math.min(width, mid), 0) + borderOverhead;
		if (total <= maxWidth) {
			low = mid;
		} else {
			high = mid - 1;
		}
	}
	const widths = naturalWidths.map((width) => Math.min(width, low));
	const headerLines = headerCells.map((cell, c) => wrapCellText(cell, widths[c] ?? MIN_COLUMN_WIDTH));
	const rowLines = rows.map((row) =>
		row.map((cell, c) => wrapCellText(cell, widths[c] ?? MIN_COLUMN_WIDTH)),
	);

	const renderRow = (
		cellLines: string[][],
		keyPrefix: string,
		tone: 'header' | 'body',
	): React.JSX.Element[] => {
		const height = Math.max(1, ...cellLines.map((lines) => lines.length));
		return Array.from({length: height}, (_, lineIndex) => (
			<Text key={`${keyPrefix}-${lineIndex}`}>
				<Text color={theme.colors.muted}>{'│'}</Text>
				{cellLines.map((lines, c) => {
					const content = lines[lineIndex] ?? '';
					const padding = ' '.repeat(Math.max(0, (widths[c] ?? 0) - stringWidth(content)));
					return (
						<React.Fragment key={c}>
							<Text
								color={tone === 'header' ? theme.colors.primary : undefined}
								bold={tone === 'header'}
							>
								{' '}{content}{padding}{' '}
							</Text>
							<Text color={theme.colors.muted}>{'│'}</Text>
						</React.Fragment>
					);
				})}
			</Text>
		));
	};

	return (
		<Box flexDirection="column" marginTop={1} marginLeft={1}>
			<Text color={theme.colors.muted}>{tableBorder(widths, '┌', '┬', '┐')}</Text>
			{renderRow(headerLines, 'h', 'header')}
			<Text color={theme.colors.muted}>{tableBorder(widths, '├', '┼', '┤')}</Text>
			{rowLines.map((cellLines, i) => (
				<React.Fragment key={i}>{renderRow(cellLines, `r${i}`, 'body')}</React.Fragment>
			))}
			<Text color={theme.colors.muted}>{tableBorder(widths, '└', '┴', '┘')}</Text>
		</Box>
	);
}

const MAX_CODE_LINES = 40;

function MarkdownBlock({
	token,
	theme,
	maxWidth,
}: {
	token: Token;
	theme: ThemeConfig;
	maxWidth: number;
}): React.JSX.Element | null {
	const highlighted = React.useMemo(() => {
		if (token.type !== 'code') {
			return [];
		}
		const code = token as Tokens.Code;
		return highlightToLines(code.text, code.lang, theme);
	}, [token, theme]);

	switch (token.type) {
		case 'heading': {
			const h = token as Tokens.Heading;
			const headingColors: string[] = [
				theme.colors.primary,
				theme.colors.secondary,
				theme.colors.accent,
				theme.colors.info,
				theme.colors.muted,
				theme.colors.muted,
			];
			const color = headingColors[h.depth - 1] ?? theme.colors.primary;
			const isMajor = h.depth <= 2;
			return (
				<Box marginTop={1} flexDirection="column">
					<Text color={color} bold={isMajor} underline={h.depth === 1}>
						{renderInline(h.tokens, theme)}
					</Text>
					{h.depth === 1 ? <Text color={color} dimColor>{'━'.repeat(32)}</Text> : null}
				</Box>
			);
		}

		case 'paragraph': {
			const p = token as Tokens.Paragraph;
			return (
				<Box marginTop={0} flexWrap="wrap">
					<Text>{renderInline(p.tokens, theme)}</Text>
				</Box>
			);
		}

		case 'code': {
			const c = token as Tokens.Code;
			const visible = highlighted.slice(0, MAX_CODE_LINES);
			const hidden = Math.max(0, highlighted.length - visible.length);
			const lineLabel = `${highlighted.length} line${highlighted.length === 1 ? '' : 's'}`;
			return (
				<Box flexDirection="column" marginTop={1} marginLeft={2} borderStyle="round" paddingX={1} borderColor={theme.colors.muted}>
					<Text dimColor>
						{c.lang ? `${c.lang}  ` : ''}
						{lineLabel}
					</Text>
					{visible.map((spans, i) => (
						<Text key={i}>
							{spans.length === 0
								? ' '
								: spans.map((span, j) => (
										<Text key={j} color={span.color} bold={span.bold} italic={span.italic}>
											{span.text}
										</Text>
									))}
						</Text>
					))}
					{hidden > 0 ? (
						<Text dimColor>
							{'… '}
							{hidden} more line{hidden === 1 ? '' : 's'} · ctrl+o full transcript
						</Text>
					) : null}
				</Box>
			);
		}

		case 'blockquote': {
			const bq = token as Tokens.Blockquote;
			return (
				<Box flexDirection="column" marginTop={0} marginLeft={0}>
					{bq.tokens.map((t, i) => (
						<Box key={i} flexDirection="row">
							<Text color={theme.colors.muted}>{'│ '}</Text>
							<Box flexDirection="column" flexGrow={1}>
								{renderBlocks([t], theme, Math.max(16, maxWidth - 2))}
							</Box>
						</Box>
					))}
				</Box>
			);
		}

		case 'list': {
			const l = token as Tokens.List;
			return (
				<Box flexDirection="column" marginTop={0} marginLeft={2}>
					{l.items.map((item, i) => {
						// For tight lists, item.tokens = [{type:'text', tokens:[...inline]}]
						// For loose lists, item.tokens = [{type:'paragraph', tokens:[...inline]}]
						const inlineTokens: Token[] = item.tokens.flatMap((t) =>
							'tokens' in t && t.tokens ? (t.tokens as Token[]) : [],
						);
						const bullet = l.ordered ? `${(Number(l.start) || 1) + i}. ` : '• ';
						return (
							<Box key={i} flexDirection="row">
								<Text color={theme.colors.primary}>{bullet}</Text>
								<Box flexGrow={1}>
									<Text>
										{inlineTokens.length > 0
											? renderInline(inlineTokens, theme)
											: item.text}
									</Text>
								</Box>
							</Box>
						);
					})}
				</Box>
			);
		}

		case 'hr':
			return (
				<Box marginTop={1}>
					<Text dimColor>{'─'.repeat(48)}</Text>
				</Box>
			);

		case 'space':
			return null;

		case 'table':
			return <MarkdownTable table={token as Tokens.Table} theme={theme} maxWidth={maxWidth} />;

		default:
			if ((token as Token).raw) {
				return <Text>{(token as Token).raw}</Text>;
			}
			return null;
	}
}

export const MarkdownText = React.memo(function MarkdownText({content}: {content: string}): React.JSX.Element {
	const {theme} = useTheme();
	const {columns} = useTerminalSize();
	const tokens = React.useMemo(() => lexer(content), [content]);
	// The conversation adds padding plus a couple of indents around this block;
	// keep tables inside what is actually visible.
	const maxWidth = Math.max(20, columns - 6);
	return (
		<Box flexDirection="column">
			{renderBlocks(tokens, theme, maxWidth)}
		</Box>
	);
});
