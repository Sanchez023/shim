import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {Box, Static, Text, render} from 'ink';

import {ThemeProvider, useTheme} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';
import {
	ConversationView,
	TranscriptRow,
	groupToolPairs,
	selectVisibleGroups,
	splitTranscript,
	staticBoundary,
} from './ConversationView.js';

const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');
const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

type InkTestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

function createTestStdout(): InkTestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns: 120,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

async function waitForOutputToStabilize(getOutput: () => string): Promise<string> {
	let previous = '';
	let sawOutput = false;

	for (let i = 0; i < 50; i++) {
		await nextLoopTurn();
		const current = getOutput();
		sawOutput ||= current.length > 0;
		if (sawOutput && current === previous) {
			return current;
		}
		previous = current;
	}

	throw new Error(`Ink output did not stabilize: ${JSON.stringify(previous)}`);
}

async function renderOutput(node: React.JSX.Element): Promise<string> {
	const stdout = createTestStdout();
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(
		<ThemeProvider initialTheme="default">{node}</ThemeProvider>,
		{stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false},
	);

	const exitPromise = instance.waitUntilExit();
	const stableOutput = await waitForOutputToStabilize(() => output);
	instance.unmount();
	await exitPromise;
	await waitForOutputToStabilize(() => output);
	instance.cleanup();
	return stripAnsi(stableOutput);
}

const firstTool: TranscriptItem = {
	role: 'tool',
	text: 'bash',
	tool_name: 'bash',
	tool_input: {command: 'ls'},
};
const firstResult: TranscriptItem = {role: 'tool_result', text: 'first-result', is_error: false};
const secondTool: TranscriptItem = {
	role: 'tool',
	text: 'bash',
	tool_name: 'bash',
	tool_input: {command: 'pwd'},
};
const secondResult: TranscriptItem = {role: 'tool_result', text: 'second-result', is_error: false};

function view(): React.JSX.Element {
	return (
		<ConversationView
			items={[firstTool, firstResult, secondTool, secondResult]}
			assistantBuffer=""
			showWelcome={false}
			outputStyle="default"
		/>
	);
}

test('completed bash calls preview their output inline', async () => {
	const output = await renderOutput(view());

	assert.match(output, /Bash\(ls\)/);
	assert.match(output, /Bash\(pwd\)/);
	assert.match(output, /first-result/);
	assert.match(output, /second-result/);
	assert.doesNotMatch(output, /click to expand/);
	assert.doesNotMatch(output, /ctrl\+o to expand/);
});

test('live thinking streams in a dim block above the answer', async () => {
	const output = await renderOutput(
		<ConversationView
			items={[]}
			assistantBuffer=""
			thinkingBuffer="weighing the seams"
			showWelcome={false}
			outputStyle="default"
		/>,
	);

	assert.match(output, /✻ Thinking…/);
	assert.match(output, /weighing the seams/);
});

test('completed thinking collapses to a Claude-style summary row', async () => {
	const output = await renderOutput(
		<ConversationView
			items={[{role: 'thinking', text: 'hidden reasoning', duration_ms: 3200}]}
			assistantBuffer=""
			showWelcome={false}
			outputStyle="default"
		/>,
	);

	assert.match(output, /✻ Thought for 3s/);
	assert.match(output, /ctrl\+o full transcript/);
	assert.doesNotMatch(output, /hidden reasoning/);
});

test('oversized command output is clamped and points at ctrl+o', async () => {
	const text = Array.from({length: 200}, (_, index) => `system-line-${index}`).join('\n');
	const output = await renderOutput(
		<ConversationView
			items={[{role: 'system', text}]}
			assistantBuffer=""
			showWelcome={false}
			outputStyle="default"
		/>,
	);

	assert.match(output, /system-line-0/);
	assert.doesNotMatch(output, /system-line-199/);
	assert.match(output, /more lines · ctrl\+o full transcript/);
});

test('splitTranscript commits older items and keeps a small dynamic tail', () => {
	const items: TranscriptItem[] = Array.from({length: 20}, (_, index) => ({
		role: 'assistant',
		text: `message-${index}`,
	}));

	const {staticItems, dynamicItems} = splitTranscript(items);

	assert.equal(staticItems.length, 12);
	assert.equal(dynamicItems.length, 8);
	assert.equal(staticItems[0].text, 'message-0');
	assert.equal(dynamicItems[dynamicItems.length - 1].text, 'message-19');
});

test('splitTranscript keeps tool/result pairs on the same side', () => {
	const items: TranscriptItem[] = [];
	for (let index = 0; index < 3; index += 1) {
		items.push({role: 'tool', text: 'tool', tool_name: 'bash'});
		items.push({role: 'tool_result', text: `result-${index}`});
	}

	const {staticItems, dynamicItems} = splitTranscript(items, 1);

	assert.equal(staticItems.length, 4);
	assert.equal(dynamicItems.length, 2);
	assert.equal(dynamicItems[0].role, 'tool');
	assert.equal(dynamicItems[1].role, 'tool_result');
});

function StaticHistoryView({items}: {items: TranscriptItem[]}): React.JSX.Element {
	const {theme} = useTheme();
	return (
		<Static items={groupToolPairs(items)}>
			{(group, index) => (
				<TranscriptRow
					key={index}
					group={group}
					theme={theme}
					outputStyle="default"
					clamp={false}
				/>
			)}
		</Static>
	);
}

test('Static keeps the full transcript prefix in terminal scrollback', async () => {
	const items: TranscriptItem[] = Array.from({length: 50}, (_, index) => ({
		role: 'assistant',
		text: `scrollback-message-${index}`,
	}));

	const output = await renderOutput(<StaticHistoryView items={items} />);

	assert.match(output, /scrollback-message-0/);
	assert.match(output, /scrollback-message-49/);
});

test('a finished answer is printed in full instead of being folded', async () => {
	// The final assistant message is the product of the turn: folding it into
	// "… N more lines" forced a ctrl+o round-trip just to read the reply.
	const items: TranscriptItem[] = [
		{role: 'assistant', text: Array.from({length: 60}, (_, index) => `answer-line-${index}`).join('\n')},
	];

	const output = await renderOutput(<StaticHistoryView items={items} />);

	assert.match(output, /answer-line-59/);
	assert.doesNotMatch(output, /more lines/);
});

test('the live frame keeps the tail of a tall answer', async () => {
	// The conclusion is what the user just waited for.  A head clamp hid it
	// behind "… N more lines" and made the final round look partially folded.
	const items: TranscriptItem[] = [
		{role: 'assistant', text: Array.from({length: 60}, (_, index) => `live-line-${index}`).join('\n')},
	];

	const output = await renderOutput(
		<Box flexDirection="column" height={20}>
			<ConversationView
				items={items}
				assistantBuffer=""
				showWelcome={false}
				outputStyle="default"
				maxRows={12}
			/>
		</Box>,
	);

	assert.match(output, /live-line-59/);
	assert.doesNotMatch(output, /live-line-0\b/);
	assert.match(output, /earlier lines · ctrl\+o full transcript/);
});

test('staticBoundary flushes the tail once a turn finishes', () => {
	// Mid-turn: the newest items stay live so streaming stays visible.
	assert.equal(staticBoundary(20, 0), 12);
	// Idle after a turn: everything is printed, so the reply is never folded.
	assert.equal(staticBoundary(20, 20), 20);
	// Monotonic: a new turn never pulls printed rows back into the live frame.
	assert.equal(staticBoundary(21, 20), 20);
	assert.equal(staticBoundary(0, 0), 0);
});

test('results pair with their tool when completions arrive out of order', () => {
	const items: TranscriptItem[] = [
		{role: 'tool', tool_name: 'agent', tool_use_id: 'a', text: 'agent', tool_input: {description: 'A'}},
		{role: 'tool', tool_name: 'agent', tool_use_id: 'b', text: 'agent', tool_input: {description: 'B'}},
		{role: 'tool', tool_name: 'bash', tool_use_id: 'c', text: 'bash', tool_input: {command: 'ls'}},
		{role: 'tool_result', tool_use_id: 'c', text: 'bash done', is_error: false},
		{role: 'tool_result', tool_use_id: 'a', text: 'agent A done', is_error: false},
		{role: 'tool_result', tool_use_id: 'b', text: 'agent B done', is_error: false},
	];

	const grouped = groupToolPairs(items);
	assert.equal(grouped.length, 3);
	const pairs = grouped as Array<readonly [TranscriptItem, TranscriptItem]>;
	assert.deepEqual(
		pairs.map(([tool, result]) => [tool.tool_use_id, result.tool_use_id]),
		[
			['a', 'a'],
			['b', 'b'],
			['c', 'c'],
		],
	);
});

test('selectVisibleGroups keeps the newest rows that fit the budget', () => {	const groups = Array.from({length: 10}, (_, index) => ({
		role: 'assistant' as const,
		text: `message-${index}\n${'detail '.repeat(20)}`,
	}));

	const visible = selectVisibleGroups(groups, 10, 114, 28);
	const texts = visible.map((group) => (group as TranscriptItem).text);

	assert.ok(texts.length > 0);
	assert.ok(texts.length < groups.length);
	assert.match(texts[texts.length - 1], /message-9/);
	assert.doesNotMatch(texts.join('\n'), /message-0/);
});

test('selectVisibleGroups always keeps the newest item', () => {
	const groups: TranscriptItem[] = [{role: 'assistant', text: 'only-message'}];
	assert.deepEqual(selectVisibleGroups(groups, 1, 80, 20), groups);
});

test('many tall messages keep the prompt row visible', async () => {
	const items: TranscriptItem[] = Array.from({length: 12}, (_, index) => ({
		role: 'assistant',
		text: `message-${index}\n${'detail '.repeat(20)}`,
	}));

	const output = await renderOutput(
		<Box flexDirection="column" height={14}>
			<Box flexDirection="column" flexGrow={1} flexBasis={0} overflowY="hidden">
				<ConversationView
					items={items}
					assistantBuffer=""
					showWelcome={false}
					outputStyle="default"
					maxRows={10}
				/>
			</Box>
			<Text>PROMPT_INPUT_VISIBLE</Text>
		</Box>,
	);

	assert.match(output, /message-11/);
	assert.doesNotMatch(output, /message-0\n/);
	assert.match(output, /PROMPT_INPUT_VISIBLE/);
});
