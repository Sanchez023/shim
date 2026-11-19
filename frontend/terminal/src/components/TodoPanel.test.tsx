import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {TodoPanel} from './TodoPanel.js';

const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');
const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));

type TestStdout = PassThrough & {
	isTTY: boolean;
	columns: number;
	rows: number;
	cursorTo: () => boolean;
	clearLine: () => boolean;
	moveCursor: () => boolean;
};

type TestStdin = PassThrough & {
	isTTY: boolean;
	setRawMode: (_mode: boolean) => void;
	resume: () => TestStdin;
	pause: () => TestStdin;
	ref: () => TestStdin;
	unref: () => TestStdin;
};

function createStdout(): TestStdout {
	return Object.assign(new PassThrough(), {
		isTTY: true,
		columns: 120,
		rows: 40,
		cursorTo: () => true,
		clearLine: () => true,
		moveCursor: () => true,
	});
}

function createStdin(): TestStdin {
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

test('sidebar variant renders plan progress, checkboxes and the current item', async () => {
	const stdin = createStdin();
	const stdout = createStdout();
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(
		<TodoPanel
			markdown={'- [x] inspect repository\n- [ ] write fix\n- [ ] run tests'}
			variant="sidebar"
			title="Plan"
			width={32}
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
	for (let i = 0; i < 30; i += 1) {
		await nextLoopTurn();
	}
	const frame = stripAnsi(output);
	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();

	assert.match(frame, /Plan/);
	assert.match(frame, /1\/3 done/);
	assert.match(frame, /inspect repository/);
	assert.match(frame, /write fix/);
	assert.match(frame, /run tests/);
	assert.match(frame, /▶ write fix/);
	assert.match(frame, /[█░]+/);


async function renderFrame(element: React.JSX.Element): Promise<string> {
	const stdin = createStdin();
	const stdout = createStdout();
	let output = '';
	stdout.on('data', (chunk) => {
		output += chunk.toString();
	});

	const instance = render(element, {
		stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
		stdout: stdout as unknown as NodeJS.WriteStream,
		exitOnCtrlC: false,
		debug: true,
		patchConsole: false,
	});

	const exitPromise = instance.waitUntilExit();
	for (let i = 0; i < 30; i += 1) {
		await nextLoopTurn();
	}
	const frame = stripAnsi(output);
	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();
	return frame;
}

test('compact display keeps the progress line but drops the item list', async () => {
	const frame = await renderFrame(
		<TodoPanel
			markdown={'- [x] inspect repository\n- [ ] write fix'}
			display="compact"
			variant="sidebar"
			title="Plan"
			width={32}
		/>,
	);

	assert.match(frame, /Plan/);
	assert.match(frame, /1\/2 done/);
	assert.match(frame, /\[ctrl\+t hide\]/);
	assert.doesNotMatch(frame, /write fix/);
});

test('hidden display renders nothing at all', async () => {
	const frame = await renderFrame(
		<TodoPanel markdown={'- [ ] write fix'} display="hidden" variant="sidebar" title="Plan" width={32} />,
	);

	assert.doesNotMatch(frame, /Plan/);
	assert.doesNotMatch(frame, /write fix/);
});

test('inline compact display advertises the next ctrl+t step', async () => {
	const frame = await renderFrame(
		<TodoPanel markdown={'- [x] one\n- [ ] two'} display="compact" />,
	);

	assert.match(frame, /Todos: 1\/2 done/);
	assert.match(frame, /\[ctrl\+t hide\]/);
	assert.doesNotMatch(frame, /☐ two/);
});

test('maxItems folds the overflow into a "… N more" row (inline)', async () => {
	const frame = await renderFrame(
		<TodoPanel
			markdown={'- [ ] one\n- [ ] two\n- [ ] three\n- [ ] four'}
			maxItems={2}
		/>,
	);

	assert.match(frame, /one/);
	assert.match(frame, /two/);
	assert.doesNotMatch(frame, /☐ three/);
	assert.match(frame, /… 2 more/);
});

test('maxItems folds the overflow in the sidebar variant too', async () => {
	const frame = await renderFrame(
		<TodoPanel
			markdown={'- [ ] one\n- [ ] two\n- [ ] three\n- [ ] four'}
			variant="sidebar"
			title="Plan"
			width={32}
			maxItems={1}
		/>,
	);

	assert.match(frame, /Plan/);
	assert.match(frame, /one/);
	assert.doesNotMatch(frame, /☐ two/);
	assert.match(frame, /… 3 more/);
});
});
