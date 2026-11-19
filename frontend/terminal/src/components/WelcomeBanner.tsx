import React from 'react';
import {Box, Text} from 'ink';

import {useTerminalSize} from '../hooks/useTerminalSize.js';

import {useTheme} from '../theme/ThemeContext.js';

// Five-row block letters. Keep every row the same width so narrow terminals
// never see a ragged right edge.
const WORDMARKS: Record<string, string[]> = {
	shim: [
		'███████ ██   ██ ██ ███    ███',
		'██      ██   ██ ██ ████  ████',
		'███████ ███████ ██ ██ ████ ██',
		'     ██ ██   ██ ██ ██  ██  ██',
		'███████ ██   ██ ██ ██      ██',
	],
	ohmo: [
		' ██████  ██   ██ ███    ███  ██████ ',
		'██    ██ ██   ██ ████  ████ ██    ██',
		'██    ██ ███████ ██ ████ ██ ██    ██',
		'██    ██ ██   ██ ██  ██  ██ ██    ██',
		' ██████  ██   ██ ██      ██  ██████ ',
	],
};

const HINTS: Array<[string, string]> = [
	['/help', 'commands'],
	['/model', 'switch model'],
	['ctrl+c', 'exit'],
];

export function WelcomeBanner({
	appName = 'shim',
	version,
}: {
	appName?: string;
	version?: string;
}): React.JSX.Element {
	const {theme} = useTheme();
	const {columns} = useTerminalSize();
	const name = appName.toLowerCase();
	const wordmark = WORDMARKS[name];
	const versionLabel = version ? `v${version}` : '';
	const setLine = (
		<Text>
			{HINTS.map(([key, description], index) => (
				<React.Fragment key={key}>
					{index > 0 ? <Text dimColor>{'  ·  '}</Text> : null}
					<Text color={theme.colors.primary}>{key}</Text>
					<Text dimColor> {description}</Text>
				</React.Fragment>
			))}
		</Text>
	);

	if (wordmark && columns >= 40) {
		return (
			<Box flexDirection="column" marginBottom={1}>
				{wordmark.map((line, index) => (
					<Text key={index} color={theme.colors.primary} bold>
						{line}
					</Text>
				))}
				<Text> </Text>
				<Text>
					<Text dimColor> An AI-powered coding assistant</Text>
					{versionLabel ? <Text dimColor>{'  '}{versionLabel}</Text> : null}
				</Text>
				<Text> </Text>
				{setLine}
			</Box>
		);
	}

	return (
		<Box flexDirection="column" marginBottom={1}>
			<Text>
				<Text color={theme.colors.primary} bold>
					◆ {name.toUpperCase()}
				</Text>
				{versionLabel ? <Text dimColor>{'  '}{versionLabel}</Text> : null}
			</Text>
			<Text dimColor>An AI-powered coding assistant</Text>
			{setLine}
		</Box>
	);
}
