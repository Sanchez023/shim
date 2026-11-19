import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';

export function HistorySearch({
	query,
	matches,
	selectedIndex,
}: {
	query: string;
	matches: string[];
	selectedIndex: number;
}): React.JSX.Element {
	const {theme} = useTheme();
	return (
		<Box flexDirection="column" marginBottom={1}>
			<Text dimColor>
				history search · ctrl+r next · enter select · esc cancel
			</Text>
			<Box>
				<Text color={theme.colors.primary} bold>
					{'? '}
				</Text>
				<Text>{query}</Text>
				<Text inverse> </Text>
			</Box>
			{matches.length === 0 ? (
				<Text dimColor>{'  no matches'}</Text>
			) : (
				matches.slice(0, 8).map((item, index) => (
					<Box key={`${index}:${item}`}>
						<Text
							color={index === selectedIndex ? theme.colors.primary : undefined}
							dimColor={index !== selectedIndex}
						>
							{index === selectedIndex ? '❯ ' : '  '}
							{item.replace(/\n/g, ' ⏎ ').slice(0, 120)}
						</Text>
					</Box>
				))
			)}
		</Box>
	);
}
