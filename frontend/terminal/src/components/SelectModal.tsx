import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';

export type SelectOption = {
	value: string;
	label: string;
	description?: string;
	active?: boolean;
};

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
						return (
							<Box key={opt.value} flexDirection="row">
								<Text color={isSelected ? theme.colors.primary : undefined} bold={isSelected}>
									{isSelected ? '\u276F ' : '  '}
									<Text color={isSelected ? theme.colors.primary : undefined}>
										{opt.label}
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
