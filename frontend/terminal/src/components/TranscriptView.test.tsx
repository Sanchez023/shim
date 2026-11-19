import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {ThemeProvider} from '../theme/ThemeContext.js';
import type {TranscriptItem} from '../types.js';
import {TranscriptView, buildTranscriptLines, type TranscriptLine} from './TranscriptView.js';

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

test('transcript lines keep full tool results and the streaming buffer', () => {
	const resultText = Array.from({length: 12}, (_, index) => `result-${index + 1}`).join('\n');
	const items: TranscriptItem[] = [
		{role: 'user', text: 'run it'},
		{role: 'tool', text: 'bash', tool_name: 'bash', tool_input: {command: 'ls'}},
		{role: 'tool_result', text: resultText, is_error: false},
		{role: 'assistant', text: 'done'},
	];

	const lines = buildTranscriptLines(items, 'still streaming');
	const joined = lines.map((line) => line.text).join('\n');

	assert.match(joined, /^> run it$/m);
	assert.match(joined, /result-12/);
	assert.match(joined, /still streaming/);
	assert.equal(lines.find((line) => line.text === 'result-1')?.kind, 'result');
});

test('viewport anchors to the bottom by default and can scroll to the top', async () => {
	const lines: TranscriptLine[] = Array.from({length: 20}, (_, index) => ({
		text: `line-${index + 1}`,
		kind: 'log',
	}));

	const bottom = await renderOutput(<TranscriptView lines={lines} height={5} offset={0} />);
	assert.match(bottom, /line-20/);
	assert.doesNotMatch(bottom, /line-14/);

	const top = await renderOutput(<TranscriptView lines={lines} height={5} offset={9999} />);
	assert.match(top, /line-1/);
	assert.doesNotMatch(top, /line-20/);
});
