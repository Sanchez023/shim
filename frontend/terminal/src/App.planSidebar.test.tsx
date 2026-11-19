import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {App} from './App.js';

const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
const stripAnsi = (value: string): string =>
	value
		.replace(/\u001B\][^\u0007]*\u0007/g, '')
		.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');

function latestFrame(writes: string[]): string {
	for (let index = writes.length - 1; index >= 0; index--) {
		const frame = stripAnsi(writes[index] ?? '');
		if (frame.trim()) {
			return frame;
		}
	}
	return '';
}

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

const backendScript = [
	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model', permission_mode: 'Plan Mode'}, commands: [], command_descriptions: {}}));",
	"console.log('OHJSON:' + JSON.stringify({type: 'todo_update', todo_markdown: '- [x] inspect repository\\n- [ ] write plan'}));",
	'process.stdin.resume();',
].join('\n');

test('plan mode renders the todo sidebar with progress', async () => {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', backendScript]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();

	const deadline = Date.now() + 3000;
	while (Date.now() < deadline) {
		const frame = stripAnsi(writes[writes.length - 1] ?? '');
		if (frame.includes('write plan')) {
			break;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
		await nextLoopTurn();
	}

	const frame = stripAnsi(writes[writes.length - 1] ?? '');
	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();

	assert.match(frame, /Plan/);
	assert.match(frame, /1\/2 done/);
	assert.match(frame, /inspect repository/);
	assert.match(frame, /▶ write plan/);
});

const todoInAutoScript = [
	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model', permission_mode: 'Auto'}, commands: [], command_descriptions: {}}));",
	"console.log('OHJSON:' + JSON.stringify({type: 'todo_update', todo_markdown: '- [x] inspect repository\\n- [ ] write plan'}));",
	'process.stdin.resume();',
].join('\n');

test('the todo list stays in the sidebar outside plan mode', async () => {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', todoInAutoScript]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();

	const deadline = Date.now() + 3000;
	while (Date.now() < deadline) {
		const frame = stripAnsi(writes[writes.length - 1] ?? '');
		if (frame.includes('write plan')) {
			break;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
		await nextLoopTurn();
	}

	const frame = stripAnsi(writes[writes.length - 1] ?? '');
	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();

	assert.match(frame, /Todo/);
	assert.match(frame, /1\/2 done/);
	assert.match(frame, /▶ write plan/);
});

const permissionModalScript = [
	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model', permission_mode: 'Auto'}, commands: [], command_descriptions: {}}));",
	"console.log('OHJSON:' + JSON.stringify({type: 'todo_update', todo_markdown: '- [x] inspect repository\\n- [ ] write plan'}));",
	"setTimeout(() => console.log('OHJSON:' + JSON.stringify({type: 'modal_request', modal: {kind: 'permission', request_id: 'r1', tool_name: 'bash', reason: 'needs approval'}})), 300);",
	'process.stdin.resume();',
].join('\n');

test('a permission prompt is not crowded out by the todo panel', async () => {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', permissionModalScript]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();

	const deadline = Date.now() + 3000;
	while (Date.now() < deadline) {
		const frame = latestFrame(writes);
		if (frame.includes('Allow bash')) {
			break;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
		await nextLoopTurn();
	}

	const frame = latestFrame(writes);
	instance.unmount();
	await exitPromise;
	instance.cleanup();
	stdin.destroy();
	stdout.destroy();

	assert.match(frame, /Allow bash\?/);
	assert.match(frame, /\[y\] Allow/);
	// The sidebar and the inline checklist are hidden while a modal is open.
	assert.doesNotMatch(frame, /write plan/);
});
