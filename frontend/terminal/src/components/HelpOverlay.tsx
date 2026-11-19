import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';

const SHORTCUTS: Array<[string, string]> = [
	['enter', 'send message'],
	['shift+enter', 'newline'],
	['↑ / ↓', 'history when input is empty'],
	['type while running', 'queue a message; ↑ edits the latest one'],
	['ctrl+x ctrl+s', 'send queued input now (interrupts the running turn)'],
	['ctrl+r', 'reverse history search'],
	['tab', 'permission mode picker'],
	['/', 'command picker'],
	['@', 'file mention / fuzzy completion'],
	['!', 'run a shell command directly'],
	['ctrl+o', 'toggle full transcript (↑/↓/j/k scroll, q/esc close)'],
	['ctrl+y', 'copy last assistant message'],
	['ctrl+v', 'attach clipboard image'],
	['esc', 'interrupt turn; double-esc clears input'],
	['ctrl+c', 'stop while busy; exit when idle'],
	['?', 'toggle this help'],
];

export function HelpOverlay(): React.JSX.Element {
	const {theme} = useTheme();
	return (
		<Box
			flexDirection="column"
			borderStyle="round"
			borderColor={theme.colors.primary}
			paddingX={1}
			marginBottom={1}
		>
			<Text bold color={theme.colors.primary}>
				Keyboard shortcuts
			</Text>
			{SHORTCUTS.map(([key, description]) => (
				<Box key={key}>
					<Box width={14}>
						<Text color={theme.colors.accent}>{key}</Text>
					</Box>
					<Text dimColor>{description}</Text>
				</Box>
			))}
			<Text dimColor>esc or ? to close</Text>
		</Box>
	);
}
