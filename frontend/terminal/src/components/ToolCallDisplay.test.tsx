import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {ThemeProvider} from '../theme/ThemeContext.js';
import {ToolCallDisplay, summarizeToolInput} from './ToolCallDisplay.js';

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

const toolItem = {
	role: 'tool' as const,
	tool_name: 'bash',
	tool_input: {command: 'ls'},
	text: 'bash',
};

const resultItem = {
	role: 'tool_result' as const,
	text: 'alpha-result\nbeta-result\ngamma-result\ndelta-result\nepsilon-result',
	is_error: false,
};

test('collapsed tool output points at ctrl+o and hides result lines', async () => {
	const output = await renderOutput(
		<ToolCallDisplay item={toolItem} resultItem={resultItem} outputStyle="default" />,
	);

	assert.match(output, /\(ctrl\+o to expand\)/);
	assert.doesNotMatch(output, /click to expand/);
	assert.doesNotMatch(output, /alpha-result/);
});

test('truncated error results advertise the transcript viewer', async () => {
	const errorItem = {
		role: 'tool_result' as const,
		text: Array.from({length: 9}, (_, index) => `error line ${index + 1}`).join('\n'),
		is_error: true,
	};

	const output = await renderOutput(
		<ToolCallDisplay item={errorItem} outputStyle="default" />,
	);

	assert.match(output, /\.\.\. \(4 more lines\)/);
	assert.match(output, /\(ctrl\+o to expand\)/);
});


test('summarizes file tools with the actual input keys', () => {
	assert.equal(summarizeToolInput('read_file', {path: 'src/app.ts'}), 'src/app.ts');
	assert.equal(summarizeToolInput('write_file', {path: 'src/app.ts', content: 'x'}), 'src/app.ts');
	assert.equal(
		summarizeToolInput('edit_file', {path: 'src/app.ts', old_str: 'a', new_str: 'b'}),
		'src/app.ts',
	);
});

test('shows edit diffstat and tool duration', async () => {
	const editItem = {
		role: 'tool' as const,
		tool_name: 'edit_file',
		tool_input: {path: 'src/app.ts', old_str: 'a\nb', new_str: 'a\nb\nc'},
		text: 'edit_file',
		duration_ms: 2400,
	};

	const output = await renderOutput(
		<ToolCallDisplay
			item={editItem}
			resultItem={{...resultItem, text: ''}}
			outputStyle="default"
		/>,
	);

	assert.match(output, /\+3/);
	assert.match(output, /-2/);
	assert.match(output, /2\.4s/);
});


test('reads the tool duration from the result row', async () => {
	const output = await renderOutput(
		<ToolCallDisplay
			item={toolItem}
			resultItem={{...resultItem, duration_ms: 1800}}
			outputStyle="default"
		/>,
	);

	assert.match(output, /1\.8s/);
});
