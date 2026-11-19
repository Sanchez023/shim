import React, {useEffect, useState} from 'react';
import {Box, Text, useInput} from 'ink';
import TextInput from 'ink-text-input';

import {useTheme} from '../theme/ThemeContext.js';

const WAIT_FRAMES = [
	'Agent is waiting for your input   ',
	'Agent is waiting for your input.  ',
	'Agent is waiting for your input.. ',
	'Agent is waiting for your input...',
];

function WaitingAnimation(): React.JSX.Element {
	const {theme} = useTheme();
	const [frame, setFrame] = useState(0);
	useEffect(() => {
		const timer = setInterval(() => setFrame((f) => (f + 1) % WAIT_FRAMES.length), 500);
		return () => clearInterval(timer);
	}, []);
	return (
		<Text color={theme.colors.accent} dimColor>
			{WAIT_FRAMES[frame]}
		</Text>
	);
}

const CUSTOM_OPTION_LABEL = 'Other (type your own answer)';

function QuestionModal({
	modal,
	modalInput,
	setModalInput,
	onSubmit,
}: {
	modal: Record<string, unknown>;
	modalInput: string;
	setModalInput: (value: string) => void;
	onSubmit: (value: string) => void;
}): React.JSX.Element {
	const {theme} = useTheme();
	const [extraLines, setExtraLines] = useState<string[]>([]);
	const options = Array.isArray(modal.options)
		? modal.options.map((option) => String(option)).filter((option) => option.length > 0)
		: [];
	const [selectedIndex, setSelectedIndex] = useState(0);
	// Options are followed by one synthetic entry that opens a free-text input,
	// so a predefined list never blocks a custom answer.
	const customIndex = options.length;
	const customActive = options.length > 0 && selectedIndex === customIndex;

	useInput((chunk, key) => {
		if (options.length > 0) {
			const itemCount = options.length + 1;
			if (key.upArrow) {
				setSelectedIndex((index) => (index - 1 + itemCount) % itemCount);
				return;
			}
			if (key.downArrow) {
				setSelectedIndex((index) => (index + 1) % itemCount);
				return;
			}
			if (key.return) {
				// The focused TextInput handles Enter while the custom option is
				// selected, so submitting here would bypass the typed answer.
				if (customActive) {
					return;
				}
				const selected = options[selectedIndex];
				if (selected !== undefined) {
					onSubmit(selected);
				}
				return;
			}
			if (key.escape) {
				onSubmit('');
				return;
			}
			if (customActive) {
				return;
			}
			const numeric = Number.parseInt(chunk, 10);
			if (Number.isInteger(numeric) && numeric >= 1 && numeric <= options.length) {
				onSubmit(options[numeric - 1] ?? '');
			}
			return;
		}
		if (key.shift && key.return) {
			setExtraLines((lines) => [...lines, modalInput]);
			setModalInput('');
		}
	});

	const handleSubmit = (value: string): void => {
		const allLines = [...extraLines, value];
		setExtraLines([]);
		onSubmit(allLines.join('\n'));
	};

	const toolName = modal.tool_name ? String(modal.tool_name) : null;
	const reason = modal.reason ? String(modal.reason) : null;
	const question = String(modal.question ?? 'Question');

	return (
		<Box flexDirection="column" marginTop={1} borderStyle="double" borderColor={theme.colors.accent} paddingX={1}>
			<WaitingAnimation />
			<Box marginTop={1}>
				<Text color={theme.colors.accent} bold>{'\u2753 '}</Text>
				<Text bold>{question}</Text>
			</Box>
			{toolName ? (
				<Text dimColor>
					{'  '}Tool: <Text color={theme.colors.primary}>{toolName}</Text>
				</Text>
			) : null}
			{reason ? (
				<Text dimColor>{'  '}Reason: {reason}</Text>
			) : null}
			{options.length > 0 ? (
				<Box flexDirection="column" marginTop={1}>
					{options.map((option, index) => {
						const selected = index === selectedIndex;
						return (
							<Box key={`${index}:${option}`}>
								<Text color={selected ? 'cyan' : undefined} bold={selected}>
									{selected ? '\u276f ' : '  '}
									{index + 1}. {option}
								</Text>
							</Box>
						);
					})}
					<Box>
						{customActive ? (
							<>
								<Text color={theme.colors.primary} bold>
									{'\u276f '}
									{options.length + 1}.{' '}
								</Text>
								<TextInput
									focus
									value={modalInput}
									onChange={setModalInput}
									onSubmit={(value) => {
										const trimmed = value.trim();
										if (trimmed) {
											onSubmit(trimmed);
										}
									}}
									placeholder="type your answer"
								/>
							</>
						) : (
							<Text dimColor>
								{'  '}
								{options.length + 1}. {CUSTOM_OPTION_LABEL}
							</Text>
						)}
					</Box>
					<Box marginTop={1}>
						<Text dimColor>
							{'\u2191\u2193'} navigate{'  '}
							{'\u23ce'} select{'  '}esc cancel
							{customActive ? '  type a custom answer' : ''}
						</Text>
					</Box>
				</Box>
			) : (
				<>
					{extraLines.length > 0 && (
						<Box flexDirection="column" marginTop={1} marginLeft={2}>
							{extraLines.map((line, i) => (
								<Text key={i} dimColor>
									{line}
								</Text>
							))}
						</Box>
					)}
					<Box marginTop={1}>
						<Text color={theme.colors.primary}>{'> '}</Text>
						<TextInput value={modalInput} onChange={setModalInput} onSubmit={handleSubmit} />
					</Box>
					<Text dimColor>{'  '}shift+enter: newline | enter: submit</Text>
				</>
			)}
		</Box>
	);
}

type DiffLineKind = 'add' | 'del' | 'hunk' | 'context';

type ParsedDiffLine = {
	kind: DiffLineKind;
	content: string;
};

function parseDiffLines(diffText: string): ParsedDiffLine[] {
	return diffText
		.split('\n')
		.flatMap((raw): ParsedDiffLine[] => {
			if (!raw || raw.startsWith('+++') || raw.startsWith('---')) {
				return [];
			}
			if (raw.startsWith('@@')) {
				return [{kind: 'hunk', content: raw}];
			}
			if (raw.startsWith('+')) {
				return [{kind: 'add', content: raw.slice(1)}];
			}
			if (raw.startsWith('-')) {
				return [{kind: 'del', content: raw.slice(1)}];
			}
			return [{kind: 'context', content: raw.startsWith(' ') ? raw.slice(1) : raw}];
		});
}

function EditDiffModal({modal}: {modal: Record<string, unknown>}): React.JSX.Element {
	const {theme} = useTheme();
	const path = String(modal.path ?? '');
	const added = Number(modal.added ?? 0);
	const removed = Number(modal.removed ?? 0);
	const lines = parseDiffLines(String(modal.diff ?? ''));
	const pageSize = 20;
	const maxOffset = Math.max(0, lines.length - pageSize);
	const [scrollOffset, setScrollOffset] = useState(0);
	const hunkCount = lines.filter((line) => line.kind === 'hunk').length;
	const lineNumberWidth = String(lines.length).length;

	useInput(
		(_chunk, key) => {
			if (key.upArrow) {
				setScrollOffset((offset) => Math.max(0, offset - 1));
				return;
			}
			if (key.downArrow) {
				setScrollOffset((offset) => Math.min(maxOffset, offset + 1));
				return;
			}
			if (key.pageUp) {
				setScrollOffset((offset) => Math.max(0, offset - pageSize));
				return;
			}
			if (key.pageDown) {
				setScrollOffset((offset) => Math.min(maxOffset, offset + pageSize));
				return;
			}
		},
		{isActive: Boolean(process.stdin.isTTY)},
	);

	const visibleLines = lines.slice(scrollOffset, scrollOffset + pageSize);
	const firstLine = lines.length === 0 ? 0 : scrollOffset + 1;
	const lastLine = Math.min(lines.length, scrollOffset + pageSize);

	return (
		<Box flexDirection="column" marginTop={1}>
			<Text>
				<Text color={theme.colors.warning} bold>{'\u250C '}</Text>
				<Text bold>Edit </Text>
				<Text color={theme.colors.primary} bold>{path}</Text>
				<Text>{'  '}</Text>
				<Text color={theme.colors.success}>{`+${added}`}</Text>
				<Text>{' '}</Text>
				<Text color={theme.colors.error}>{`-${removed}`}</Text>
				<Text dimColor>{hunkCount ? `  ${hunkCount} hunk${hunkCount === 1 ? '' : 's'}` : ''}</Text>
				<Text dimColor>
					{'  '}
					{firstLine}-{lastLine}/{lines.length}
				</Text>
			</Text>
			{visibleLines.map((line, index) => {
				const absoluteIndex = scrollOffset + index + 1;
				const gutter = String(absoluteIndex).padStart(lineNumberWidth, ' ');
				if (line.kind === 'hunk') {
					return (
						<Text key={absoluteIndex}>
							<Text color={theme.colors.warning}>{'\u2502 '}</Text>
							<Text dimColor>{gutter} </Text>
							<Text color={theme.colors.primary} dimColor>{line.content}</Text>
						</Text>
					);
				}
				if (line.kind === 'add') {
					return (
						<Text key={absoluteIndex}>
							<Text color={theme.colors.warning}>{'\u2502 '}</Text>
							<Text dimColor>{gutter} </Text>
							<Text color={theme.colors.success}>+</Text>
							<Text color={theme.colors.success}>{line.content}</Text>
						</Text>
					);
				}
				if (line.kind === 'del') {
					return (
						<Text key={absoluteIndex}>
							<Text color={theme.colors.warning}>{'\u2502 '}</Text>
							<Text dimColor>{gutter} </Text>
							<Text color={theme.colors.error}>-</Text>
							<Text color={theme.colors.error}>{line.content}</Text>
						</Text>
					);
				}
				return (
					<Text key={absoluteIndex}>
						<Text color={theme.colors.warning}>{'\u2502 '}</Text>
						<Text dimColor>{gutter} </Text>
						<Text dimColor>{' '}{line.content}</Text>
					</Text>
				);
			})}
			{lines.length > pageSize ? (
				<Text dimColor>
					{'  '}
					{scrollOffset > 0 ? '\u2191 more above' : ''}
					{scrollOffset > 0 && scrollOffset + pageSize < lines.length ? ' \u00B7 ' : ''}
					{scrollOffset + pageSize < lines.length ? '\u2193 more below' : ''}
				</Text>
			) : null}
			<Text>
				<Text color={theme.colors.warning}>{'\u2514 '}</Text>
				<Text color={theme.colors.success}>[y] Once</Text>
				<Text>{'  '}</Text>
				<Text color={theme.colors.success}>[a] Always</Text>
				<Text>{'  '}</Text>
				<Text color={theme.colors.error}>[n] Deny</Text>
				<Text dimColor>{'  '}[\u2191\u2193/pgup/pgdn] scroll</Text>
			</Text>
		</Box>
	);
}

function ModalHostInner({
	modal,
	modalInput,
	setModalInput,
	onSubmit,
}: {
	modal: Record<string, unknown> | null;
	modalInput: string;
	setModalInput: (value: string) => void;
	onSubmit: (value: string) => void;
}): React.JSX.Element | null {
	const {theme} = useTheme();
	if (modal?.kind === 'permission') {
		return (
			<Box flexDirection="column" marginTop={1}>
				<Text>
					<Text color={theme.colors.warning} bold>{'\u250C '}</Text>
					<Text bold>Allow </Text>
					<Text color={theme.colors.primary} bold>{String(modal.tool_name ?? 'tool')}</Text>
					<Text bold>?</Text>
				</Text>
				{modal.reason ? (
					<Text>
						<Text color={theme.colors.warning}>{'\u2502 '}</Text>
						<Text dimColor>{String(modal.reason)}</Text>
					</Text>
				) : null}
				<Text>
					<Text color={theme.colors.warning}>{'\u2514 '}</Text>
					<Text color={theme.colors.success}>[y] Allow</Text>
					<Text>{'  '}</Text>
					<Text color={theme.colors.error}>[n] Deny</Text>
				</Text>
			</Box>
		);
	}
	if (modal?.kind === 'edit_diff') {
		return <EditDiffModal modal={modal} />;
	}
	if (modal?.kind === 'question') {
		return (
			<QuestionModal
				modal={modal}
				modalInput={modalInput}
				setModalInput={setModalInput}
				onSubmit={onSubmit}
			/>
		);
	}
	if (modal?.kind === 'mcp_auth') {
		return (
			<Box flexDirection="column" marginTop={1}>
				<Text>
					<Text color={theme.colors.warning} bold>{'\u{1F511} '}</Text>
					<Text bold>MCP Authentication</Text>
				</Text>
				<Text dimColor>{String(modal.prompt ?? 'Provide auth details')}</Text>
				<Box>
					<Text color={theme.colors.primary}>{'> '}</Text>
					<TextInput value={modalInput} onChange={setModalInput} onSubmit={onSubmit} />
				</Box>
			</Box>
		);
	}
	return null;
}

export const ModalHost = React.memo(ModalHostInner);
