import React, {useEffect, useState} from 'react';
import {Box, Text} from 'ink';
import stringWidth from 'string-width';

import {useTerminalSize} from '../hooks/useTerminalSize.js';
import {useTheme} from '../theme/ThemeContext.js';
import type {TaskSnapshot} from '../types.js';

// Tool names are lower-case in the backend (`bash`, `edit_file`, ...).
const WRITE_TOOLS = new Set([
	'write_file',
	'edit_file',
	'multi_edit',
	'notebook_edit',
	'bash',
	'computer',
	'str_replace_editor',
]);

function isPlanMode(mode: string): boolean {
	const normalized = mode.toLowerCase();
	return normalized === 'plan' || normalized === 'plan mode';
}

function PlanModeIndicator({
	mode,
}: {
	mode: string;
}): React.JSX.Element | null {
	const {theme} = useTheme();
	const plan = isPlanMode(mode);
	const [prevPlan, setPrevPlan] = useState(plan);
	const [flash, setFlash] = useState(false);

	useEffect(() => {
		if (prevPlan && !plan) {
			setFlash(true);
			const timer = setTimeout(() => setFlash(false), 800);
			setPrevPlan(false);
			return () => clearTimeout(timer);
		}
		setPrevPlan(plan);
	}, [plan]);

	if (!plan) {
		return flash ? (
			<Text color={theme.colors.success} bold>
				{' PLAN MODE OFF '}
			</Text>
		) : null;
	}

	return null;
}

type StatusSegment = {
	key: string;
	text: string;
	node: React.ReactNode;
};

function StatusBarInner({
	status,
	tasks,
	activeToolName,
	queuedCount = 0,
	busy = false,
	elapsedSeconds = 0,
	lastTurnSeconds = 0,
}: {
	status: Record<string, unknown>;
	tasks: TaskSnapshot[];
	activeToolName?: string;
	queuedCount?: number;
	busy?: boolean;
	elapsedSeconds?: number;
	lastTurnSeconds?: number;
}): React.JSX.Element {
	const {theme} = useTheme();
	const {columns} = useTerminalSize();
	const model = String(status.model ?? 'unknown');
	const mode = String(status.permission_mode ?? 'default');
	const plan = isPlanMode(mode);
	const taskCount = tasks.length;
	const mcpCount = Number(status.mcp_connected ?? 0);
	const inputTokens = Number(status.input_tokens ?? 0);
	const outputTokens = Number(status.output_tokens ?? 0);
	const cacheReadTokens = Number(status.cache_read_input_tokens ?? 0);
	const cacheHitRate =
		typeof status.cache_hit_rate === 'number' ? status.cache_hit_rate : null;
	const contextTokens = Number(status.context_tokens ?? 0);
	const contextWindow = Number(status.context_window_tokens ?? 0);
	const contextPercentRaw =
		contextWindow > 0 ? Math.min(100, (contextTokens / contextWindow) * 100) : 0;
	const contextPercentLabel =
		contextPercentRaw > 0 && contextPercentRaw < 1
			? '<1%'
			: `${Math.round(contextPercentRaw)}%`;

	const candidates: StatusSegment[] = [];

	candidates.push({
		key: 'model',
		text: `model: ${model}`,
		node: (
			<Text key="model" color={theme.colors.primary} dimColor>
				model: {model}
			</Text>
		),
	});

	if (contextWindow > 0) {
		const gaugeWidth = 8;
		const filled = Math.min(
			gaugeWidth,
			Math.max(0, Math.round((contextPercentRaw / 100) * gaugeWidth)),
		);
		const gauge = `${'█'.repeat(filled)}${'░'.repeat(gaugeWidth - filled)}`;
		const gaugeColor =
			contextPercentRaw >= 85
				? theme.colors.error
				: contextPercentRaw >= 60
					? theme.colors.warning
					: theme.colors.success;
		candidates.push({
			key: 'ctx',
			text: `ctx ${gauge} ${contextPercentLabel}`,
			node: (
				<Text key="ctx">
					<Text dimColor>ctx </Text>
					<Text color={gaugeColor}>{gauge}</Text>
					<Text dimColor> {contextPercentLabel}</Text>
				</Text>
			),
		});
	}

	if (plan && activeToolName && WRITE_TOOLS.has(activeToolName.toLowerCase())) {
		candidates.push({
			key: 'blocked',
			text: `⛔ ${activeToolName}`,
			node: (
				<Text key="blocked" color={theme.colors.error}>
					{' '}
					{'⛔'} {activeToolName}
				</Text>
			),
		});
	}

	candidates.push({
		key: 'mode',
		text: `mode: ${mode}`,
		node: (
			<Text key="mode" color={plan ? theme.colors.warning : undefined} bold={plan} dimColor={!plan}>
				mode: {mode}
			</Text>
		),
	});

	if (inputTokens > 0 || outputTokens > 0) {
		candidates.push({
			key: 'tok',
			text: `tok: ${formatNum(inputTokens)}↓ ${formatNum(outputTokens)}↑`,
			node: (
				<Text key="tok" dimColor>
					tok: {formatNum(inputTokens)}↓ {formatNum(outputTokens)}↑
				</Text>
			),
		});
	}

	if (cacheReadTokens > 0 || cacheHitRate !== null) {
		const rateLabel =
			cacheHitRate !== null ? ` (${(cacheHitRate * 100).toFixed(1)}%)` : '';
		candidates.push({
			key: 'cache',
			text: `cache: ${formatNum(cacheReadTokens)}${rateLabel}`,
			node: (
				<Text key="cache" dimColor>
					cache: {formatNum(cacheReadTokens)}
					{rateLabel}
				</Text>
			),
		});
	}

	if (taskCount > 0) {
		candidates.push({
			key: 'tasks',
			text: `tasks: ${taskCount}`,
			node: (
				<Text key="tasks" dimColor>
					tasks: {taskCount}
				</Text>
			),
		});
	}

	if (mcpCount > 0) {
		candidates.push({
			key: 'mcp',
			text: `mcp: ${mcpCount}`,
			node: (
				<Text key="mcp" dimColor>
					mcp: {mcpCount}
				</Text>
			),
		});
	}

	if (queuedCount > 0) {
		candidates.push({
			key: 'queued',
			text: `queued: ${queuedCount}`,
			node: (
				<Text key="queued" color={theme.colors.accent} dimColor>
					queued: {queuedCount}
				</Text>
			),
		});
	}

	const durationSeconds =
		busy && elapsedSeconds > 0
			? elapsedSeconds
			: !busy && lastTurnSeconds > 0
				? lastTurnSeconds
				: null;
	if (durationSeconds !== null) {
		candidates.push({
			key: 'duration',
			text: formatDuration(durationSeconds),
			node: (
				<Text key="duration" dimColor>
					{formatDuration(durationSeconds)}
				</Text>
			),
		});
	}

	// Drop low-priority segments instead of letting Ink truncate the tail in
	// the middle of a word (or of a wide CJK model name).
	const maxWidth = Math.max(10, columns - 2);
	const chosen: StatusSegment[] = [];
	if (candidates.length > 0) {
		chosen.push(candidates[0]!);
	}
	const usedWidth = (): number =>
		chosen.reduce((sum, segment) => sum + stringWidth(segment.text), 0) +
		Math.max(0, chosen.length - 1) * 3;
	for (const segment of candidates.slice(1)) {
		const projected =
			usedWidth() + 3 + stringWidth(segment.text);
		if (projected > maxWidth) {
			continue;
		}
		chosen.push(segment);
	}

	const line: React.ReactNode[] = [];
	chosen.forEach((segment, index) => {
		if (index > 0) {
			line.push(
				<Text key={`sep-${segment.key}`} dimColor>
					{' │ '}
				</Text>,
			);
		}
		line.push(segment.node);
	});

	return (
		<Box flexDirection="row" alignItems="center">
			<Text wrap="truncate">{line}</Text>
			<PlanModeIndicator mode={mode} />
		</Box>
	);
}

export const StatusBar = React.memo(StatusBarInner);

function formatNum(n: number): string {
	const scaled = (value: number, unit: string): string =>
		`${Number.isInteger(value) ? value : value.toFixed(1)}${unit}`;
	if (n >= 1_000_000) {
		return scaled(n / 1_000_000, 'M');
	}
	if (n >= 1000) {
		return scaled(n / 1000, 'k');
	}
	return String(n);
}

function formatDuration(seconds: number): string {
	const minutes = Math.floor(seconds / 60);
	const remainder = seconds % 60;
	return `${minutes}:${String(remainder).padStart(2, '0')}`;
}
