import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {ModalHost} from './ModalHost.js';

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

type InkTestStdin = PassThrough & {
	isTTY: boolean;
	setRawMode: (_mode: boolean) => void;
	resume: () => InkTestStdin;
	pause: () => InkTestStdin;
	ref: () => InkTestStdin;
	unref: () => InkTestStdin;
};

function createTestStdin(): InkTestStdin {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		setRawMode: () => undefined,
		resume() {
			return this;
		},
		pause() {
			return this;
		},
		ref() {
			return this;
		},
		unref() {
			return this;
		},
	});
}

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

	for (let i = 0; i < 50; i += 1) {
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

test('renders edit diff preview with stats and always shortcut', async () => {
	const stdout = createTestStdout();
	let output = '';

	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(
		<ModalHost
			modal={{
				kind: 'edit_diff',
				path: 'src/demo.txt',
				diff: '@@ -1 +1 @@\n-old line\n+new line',
				added: 1,
				removed: 1,
			}}
			modalInput=""
			setModalInput={() => undefined}
			onSubmit={() => undefined}
		/>,
		{stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false},
	);

	const exitPromise = instance.waitUntilExit();
	const stableOutput = await waitForOutputToStabilize(() => output);
	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdout.destroy();

	const rendered = stripAnsi(stableOutput);
	assert.match(rendered, /Edit src\/demo\.txt/);
	assert.match(rendered, /\+1/);
	assert.match(rendered, /-1/);
	assert.match(rendered, /\+new line/);
	assert.match(rendered, /-old line/);
	assert.match(rendered, /\[a\] Always/);
});

test('question modal offers a custom answer input as the last option', async () => {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});
	let submitted: string | null = null;

	function Harness(): React.JSX.Element {
		const [value, setValue] = React.useState('');
		return (
			<ModalHost
				modal={{kind: 'question', question: 'Pick a color', options: ['red', 'green']}}
				modalInput={value}
				setModalInput={setValue}
				onSubmit={(answer) => {
					submitted = answer;
				}}
			/>
		);
	}

	const instance = render(<Harness />, {
		stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
		stdout: stdout as unknown as NodeJS.WriteStream,
		exitOnCtrlC: false,
		debug: true,
		patchConsole: false,
	});
	const exitPromise = instance.waitUntilExit();
	const frame = stripAnsi(await waitForOutputToStabilize(() => output));
	assert.match(frame, /Other \(type your own answer\)/);

	const press = async (input: string): Promise<void> => {
		stdin.write(input);
		for (let i = 0; i < 10; i += 1) {
			await nextLoopTurn();
		}
	};

	await press('\u001B[B'); // red -> green
	await press('\u001B[B'); // green -> custom
	await press('blue');
	await press('\r');

	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();

	assert.equal(submitted, 'blue');
});

test('question modal navigates options with arrow keys and submits the selection', async () => {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});
	let submitted: string | null = null;

	const instance = render(
		<ModalHost
			modal={{
				kind: 'question',
				question: 'Pick a color',
				options: ['red', 'green', 'blue'],
			}}
			modalInput=""
			setModalInput={() => undefined}
			onSubmit={(value) => {
				submitted = value;
			}}
		/>,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);

	const exitPromise = instance.waitUntilExit();
	const frame = stripAnsi(await waitForOutputToStabilize(() => output));
	assert.match(frame, /Pick a color/);
	assert.match(frame, /red/);
	assert.match(frame, /green/);
	assert.match(frame, /blue/);

	stdin.write('\u001B[B');
	await new Promise((resolve) => setTimeout(resolve, 30));
	for (let i = 0; i < 10; i += 1) {
		await nextLoopTurn();
	}
	stdin.write('\r');
	for (let i = 0; i < 10; i += 1) {
		await nextLoopTurn();
	}

	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();

	assert.equal(submitted, 'green');
});
