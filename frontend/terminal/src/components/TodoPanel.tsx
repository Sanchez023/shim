import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';

export type TodoItem = {
	text: string;
	checked: boolean;
};

function parseTodoItems(markdown: string): TodoItem[] {
	const lines = markdown.split('\n');
	const items: TodoItem[] = [];
	for (const line of lines) {
		const m = line.match(/^\s*-\s+\[([ xX])\]\s+(.+)/);
		if (m) {
			items.push({checked: m[1].toLowerCase() === 'x', text: m[2].trim()});
		}
	}
	return items;
}

function TodoPanelInner({
	markdown,
	display = 'full',
	variant = 'inline',
	title = 'Todo List',
	width = 32,
	maxItems,
}: {
	markdown: string;
	/** full renders the list, compact keeps one summary line, hidden renders nothing. */
	display?: 'full' | 'compact' | 'hidden';
	variant?: 'inline' | 'sidebar';
	title?: string;
	width?: number;
	/**
	 * Items to render before collapsing the rest into "… N more". The panel
	 * lives inside the live frame, which has a fixed height, so an unbounded
	 * checklist would squeeze the conversation out of view.
	 */
	maxItems?: number;
}): React.JSX.Element | null {
	const {theme} = useTheme();
	const items = parseTodoItems(markdown);

	if (display === 'hidden' || items.length === 0) {
		return null;
	}

	const compact = display === 'compact';
	const done = items.filter((i) => i.checked).length;
	const total = items.length;
	const limit = maxItems === undefined ? items.length : Math.max(1, maxItems);
	const visible = items.slice(0, limit);
	const hiddenCount = items.length - visible.length;

	if (variant === 'sidebar') {
		const barWidth = Math.max(6, Math.min(20, width - 12));
		const filled = total > 0 ? Math.round((done / total) * barWidth) : 0;
		const bar = `${'█'.repeat(filled)}${'░'.repeat(Math.max(0, barWidth - filled))}`;
		const currentIndex = items.findIndex((item) => !item.checked);
		return (
			<Box flexDirection="column" borderStyle="round" borderColor={theme.colors.warning} paddingX={1}>
				<Text bold color={theme.colors.warning}>
					{title}
				</Text>
				<Text dimColor>
					{done}/{total} done
				</Text>
				<Text color={done === total ? theme.colors.success : theme.colors.warning}>{bar}</Text>
				<Text dimColor>{compact ? '[ctrl+t hide]' : '[ctrl+t compact]'}</Text>
				{compact ? null : (
					<Box flexDirection="column" marginTop={1}>
						{visible.map((item, index) => {
							const isCurrent = index === currentIndex;
							return (
								<Text
									key={`${index}:${item.text}`}
									wrap="truncate"
									color={item.checked ? theme.colors.success : isCurrent ? theme.colors.primary : undefined}
									dimColor={item.checked}
								>
									{item.checked ? '☑ ' : isCurrent ? '▶ ' : '☐ '}
									{item.text}
								</Text>
							);
						})}
						{hiddenCount > 0 ? (
							<Text dimColor>{`… ${hiddenCount} more`}</Text>
						) : null}
					</Box>
				)}
			</Box>
		);
	}

	if (compact) {
		return (
			<Box>
				<Text color={theme.colors.warning} bold>
					{'☑ '}
				</Text>
				<Text dimColor>
					Todos: {done}/{total} done
				</Text>
				<Text dimColor> [ctrl+t hide]</Text>
			</Box>
		);
	}

	return (
		<Box flexDirection="column" borderStyle="round" borderColor={theme.colors.warning} paddingX={1} marginTop={1}>
			<Box>
				<Text color={theme.colors.warning} bold>
					{'☑ '}
				</Text>
				<Text bold>
					{title}{' '}
				</Text>
				<Text dimColor>
					({done}/{total})
				</Text>
				<Text dimColor> [ctrl+t compact]</Text>
			</Box>
			{visible.map((item, i) => (
				<Box key={i}>
					<Text color={item.checked ? theme.colors.success : theme.colors.foreground}>
						{item.checked ? '  ☑ ' : '  ☐ '}
					</Text>
					<Text
						color={item.checked ? 'green' : undefined}
						dimColor={item.checked}
					>
						{item.text}
					</Text>
				</Box>
			))}
			{hiddenCount > 0 ? (
				<Box>
					<Text dimColor>{`  … ${hiddenCount} more`}</Text>
				</Box>
			) : null}
		</Box>
	);
}

export const TodoPanel = React.memo(TodoPanelInner);

export {parseTodoItems};
