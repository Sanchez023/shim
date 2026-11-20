import React from 'react';
import {Box, Text, useStdout} from 'ink';
import stringWidth from 'string-width';

import {useTheme} from '../theme/ThemeContext.js';

export type SelectOption = {
	value: string;
	label: string;
	description?: string;
	active?: boolean;
};

/** Clamp one rendered row to the terminal width (CJK-safe). */
function clampToWidth(text: string, width: number): string {
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

export function SelectModal({
	title,
	options,
	selectedIndex,
	searchable = false,
	query = '',
	totalCount,
}: {
	title: string;
	options: SelectOption[];
	selectedIndex: number;
	searchable?: boolean;
	query?: string;
	totalCount?: number;
}): React.JSX.Element {
	const {theme} = useTheme();
	const {stdout} = useStdout();
	const columns = stdout?.columns && stdout.columns > 0 ? stdout.columns : 80;
	// Round border (2) + paddingX (2) + the "❯ " cursor prefix (2).
	const maxRowWidth = Math.max(24, columns - 6);
	const showingMatches = searchable && query.trim().length > 0;
	const countLabel = showingMatches
		? `${options.length}/${totalCount ?? options.length} matches`
		: `${options.length} items`;
	// Long session lists would push the prompt off-screen; keep a window around
	// the cursor instead.
	const maxVisible = 12;
	const windowStart = Math.max(
		0,
		Math.min(selectedIndex - Math.floor(maxVisible / 2), options.length - maxVisible),
	);
	const visibleOptions = options.slice(windowStart, windowStart + maxVisible);
	const hiddenAbove = windowStart;
	const hiddenBelow = options.length - windowStart - visibleOptions.length;
	return (
		<Box flexDirection="column" borderStyle="round" borderColor={theme.colors.primary} paddingX={1} marginTop={1}>
			<Box flexDirection="row">
				<Text bold color={theme.colors.primary}>{title}</Text>
				<Text dimColor>{`  ${countLabel}`}</Text>
			</Box>
			{searchable ? (
				<Box flexDirection="row" marginTop={1}>
					<Text color={theme.colors.primary} bold>{'search '}</Text>
					<Text>{query}</Text>
					<Text inverse> </Text>
				</Box>
			) : null}
			<Text> </Text>
			{options.length === 0 ? (
				<Text dimColor>{'  no matches'}</Text>
			) : (
				<>
					{hiddenAbove > 0 ? (
						<Text dimColor>{`  \u22EE ${hiddenAbove} earlier`}</Text>
					) : null}
					{visibleOptions.map((opt, offset) => {
						const i = windowStart + offset;
						const isSelected = i === selectedIndex;
						const isCurrent = opt.active;
						const currentSuffix = isCurrent ? ' (current)' : '';
						const descriptionSuffix = opt.description ? `  ${opt.description}` : '';
						const label = clampToWidth(
							opt.label,
							maxRowWidth - stringWidth(currentSuffix) - stringWidth(descriptionSuffix),
						);
						return (
							<Box key={opt.value} flexDirection="row">
								<Text color={isSelected ? theme.colors.primary : undefined} bold={isSelected}>
									{isSelected ? '\u276F ' : '  '}
									<Text color={isSelected ? theme.colors.primary : undefined}>
										{label}
									</Text>
								</Text>
								{isCurrent ? <Text color={theme.colors.success}> (current)</Text> : null}
								{opt.description ? <Text dimColor>  {opt.description}</Text> : null}
							</Box>
						);
					})}
					{hiddenBelow > 0 ? (
						<Text dimColor>{`  \u22EE ${hiddenBelow} more`}</Text>
					) : null}
				</>
			)}
			<Text> </Text>
			<Text dimColor>
				{searchable ? 'type to search  ' : ''}
				{'\u2191\u2193'} navigate{'  '}{'\u23CE'} select{'  '}esc cancel
			</Text>
		</Box>
	);
}
