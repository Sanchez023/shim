import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';

function CommandPickerInner({
	hints,
	selectedIndex,
	descriptions = {},
	usage = {},
	query,
}: {
	hints: string[];
	selectedIndex: number;
	descriptions?: Record<string, string>;
	usage?: Record<string, string>;
	query?: string;
}): React.JSX.Element | null {
	const {theme} = useTheme();
	if (hints.length === 0) {
		if (!query) {
			return null;
		}
		return (
			<Box flexDirection="column" borderStyle="round" borderColor={theme.colors.muted} paddingX={1}>
				<Text dimColor> Commands</Text>
				<Text dimColor>No commands match /{query}</Text>
			</Box>
		);
	}
	const selected = hints[selectedIndex];
	const description = selected ? descriptions[selected] : undefined;
	const selectedUsage = selected ? usage[selected] : undefined;

	return (
		<Box flexDirection="column" borderStyle="round" borderColor={theme.colors.primary} paddingX={1} marginBottom={0}>
			<Text dimColor bold> Commands</Text>
			{hints.map((hint, i) => {
				const isSelected = i === selectedIndex;
				return (
					<Box key={hint}>
						<Text color={isSelected ? theme.colors.primary : undefined} bold={isSelected}>
							{isSelected ? '\u276F ' : '  '}
							{hint}
						</Text>
						{isSelected ? <Text dimColor> [enter]</Text> : null}
					</Box>
				);
			})}
			{description ? (
				<Text dimColor>
					{'  '}
					{description}
				</Text>
			) : null}
			{selectedUsage ? (
				<Text color={theme.colors.accent} dimColor>
					{'  '}
					{selectedUsage}
				</Text>
			) : null}
			<Text dimColor> {'\u2191\u2193'} navigate{'  '}{'\u23CE'} select{'  '}esc dismiss</Text>
		</Box>
	);
}

export const CommandPicker = React.memo(CommandPickerInner);
