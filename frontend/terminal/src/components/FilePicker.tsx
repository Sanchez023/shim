import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';

export function FilePicker({
	query,
	matches,
	selectedIndex,
}: {
	query: string;
	matches: string[];
	selectedIndex: number;
}): React.JSX.Element | null {
	const {theme} = useTheme();
	if (matches.length === 0) {
		if (!query) {
			return null;
		}
		return (
			<Box flexDirection="column" borderStyle="round" borderColor={theme.colors.muted} paddingX={1}>
				<Text dimColor>Files</Text>
				<Text dimColor>No files match @{query}</Text>
			</Box>
		);
	}
	return (
		<Box
			flexDirection="column"
			borderStyle="round"
			borderColor={theme.colors.accent}
			paddingX={1}
			marginBottom={0}
		>
			<Text dimColor bold>
				{' '}
				Files{query ? ` matching @${query}` : ''}
			</Text>
			{matches.slice(0, 10).map((path, index) => {
				const selected = index === selectedIndex;
				return (
					<Box key={path}>
						<Text color={selected ? theme.colors.accent : undefined} bold={selected}>
							{selected ? '❯ ' : '  '}
							{path}
						</Text>
						{selected ? <Text dimColor> [enter]</Text> : null}
					</Box>
				);
			})}
			<Text dimColor>
				{' '}
				↑↓ navigate  ⏎ insert  esc dismiss
			</Text>
		</Box>
	);
}
