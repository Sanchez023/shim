import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {ThemeProvider} from '../theme/ThemeContext.js';
import {
	ToolCallDisplay,
	buildLiveOutputPreview,
	summarizeToolInput,
} from './ToolCallDisplay.js';

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

test('a completed bash call previews the output block', async () => {
	const output = await renderOutput(
		<ToolCallDisplay item={toolItem} resultItem={resultItem} outputStyle="default" />,
	);

	assert.match(output, /Bash\(ls\)/);
	assert.match(output, /alpha-result/);
	assert.match(output, /epsilon-result/);
	assert.doesNotMatch(output, /\(ctrl\+o to expand\)/);
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

test('shows an accurate edit diffstat, the line diff and the duration', async () => {
	const editItem = {
		role: 'tool' as const,
		tool_name: 'edit_file',
		tool_input: {path: 'src/app.ts', old_str: 'alpha\nbeta', new_str: 'alpha\ngamma'},
		text: 'edit_file',
		duration_ms: 2400,
	};

	const output = await renderOutput(
		<ToolCallDisplay
			item={editItem}
			resultItem={{...resultItem, text: 'Updated src/app.ts'}}
			outputStyle="default"
		/>,
	);

	// The diff counts only the changed lines; counting every line of old/new
	// overstates a one-line replacement.
	assert.match(output, /Added 1 line, removed 1 line/);
	assert.match(output, /2\.4s/);
	assert.match(output, /Update\(src\/app\.ts\)/);
	assert.match(output, /-beta/);
	assert.match(output, /\+gamma/);
});

test('write_file previews its new lines as additions', async () => {
	const writeItem = {
		role: 'tool' as const,
		tool_name: 'write_file',
		tool_input: {path: 'src/new.ts', content: 'first\nsecond'},
		text: 'write_file',
	};

	const output = await renderOutput(
		<ToolCallDisplay
			item={writeItem}
			resultItem={{...resultItem, text: 'Wrote src/new.ts'}}
			outputStyle="default"
		/>,
	);

	assert.match(output, /Added 2 lines, removed 0 lines/);
	assert.match(output, /\+first/);
	assert.match(output, /\+second/);
	assert.match(output, /Write\(src\/new\.ts\)/);
});

test('renders the unified diff with real file line numbers and context', async () => {
	const editItem = {
		role: 'tool' as const,
		tool_name: 'edit_file',
		tool_input: {path: 'src/app.ts', old_str: 'removed line', new_str: 'added line\nextra line'},
		text: 'edit_file',
	};
	const diff = [
		'@@ -176,2 +176,3 @@',
		' context line',
		'-removed line',
		'+added line',
		'+extra line',
	].join('\n');

	const output = await renderOutput(
		<ToolCallDisplay
			item={editItem}
			resultItem={{
				...resultItem,
				text: 'Updated src/app.ts',
				metadata: {diff, diff_added: 2, diff_removed: 1},
			}}
			outputStyle="default"
		/>,
	);

	assert.match(output, /Added 2 lines, removed 1 line/);
	assert.match(output, /177 -removed line/);
	assert.match(output, /177 \+added line/);
	assert.match(output, /178 \+extra line/);
	assert.match(output, /176  context line/);
	assert.match(output, /Update\(src\/app\.ts\)/);
});

test('bash previews the tail of a long output with an earlier-lines hint', async () => {
	const lines = Array.from({length: 8}, (_, index) => `out-${index + 1}`).join('\n');
	const output = await renderOutput(
		<ToolCallDisplay
			item={{role: 'tool', tool_name: 'bash', tool_input: {command: 'make'}, text: 'bash'}}
			resultItem={{...resultItem, text: lines}}
			outputStyle="default"
		/>,
	);

	assert.match(output, /out-4/);
	assert.match(output, /out-8/);
	assert.doesNotMatch(output, /out-1\b/);
	assert.match(output, /3 earlier lines · ctrl\+o full transcript/);
});

test('a running command previews the tail of its partial output', async () => {
	const running = {
		role: 'tool' as const,
		tool_name: 'bash',
		tool_input: {command: 'make'},
		text: 'bash',
		output: 'one\ntwo\nthree\n',
	};

	const output = await renderOutput(
		<ToolCallDisplay item={running} outputStyle="default" maxOutputRows={2} />,
	);

	assert.match(output, /Bash\(make\)/);
	assert.match(output, /three/);
	assert.doesNotMatch(output, /one\b/);
	assert.match(output, /1 earlier line/);
});

test('live preview strips ANSI and collapses carriage-return redraws', () => {
	const preview = buildLiveOutputPreview(
		'\u001b[31mred\u001b[0m\n10%\r50%\r100%\nlast',
		2,
	);
	assert.deepEqual(preview?.lines, ['100%', 'last']);
	assert.equal(preview?.hidden, 1);
});

test('summarizes the result of read, grep and bash calls', async () => {
	const readOutput = await renderOutput(
		<ToolCallDisplay
			item={{role: 'tool', tool_name: 'read_file', tool_input: {path: 'a.ts'}, text: 'read_file'}}
			resultItem={{...resultItem, text: 'line1\nline2\nline3'}}
			outputStyle="default"
		/>,
	);
	assert.match(readOutput, /Read 3 lines/);

	const grepOutput = await renderOutput(
		<ToolCallDisplay
			item={{role: 'tool', tool_name: 'grep', tool_input: {pattern: 'ctx'}, text: 'grep'}}
			resultItem={{...resultItem, text: 'a.ts:1:ctx\nb.ts:2:ctx'}}
			outputStyle="default"
		/>,
	);
	assert.match(grepOutput, /2 matches/);
});

test('a failed edit does not preview an unapplied diff', async () => {
	const editItem = {
		role: 'tool' as const,
		tool_name: 'edit_file',
		tool_input: {path: 'src/app.ts', old_str: 'alpha\nbeta', new_str: 'alpha\ngamma'},
		text: 'edit_file',
	};

	const output = await renderOutput(
		<ToolCallDisplay
			item={editItem}
			resultItem={{...resultItem, text: 'old_str was not found in the file', is_error: true}}
			outputStyle="default"
		/>,
	);

	assert.match(output, /old_str was not found/);
	assert.doesNotMatch(output, /\+gamma/);
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
