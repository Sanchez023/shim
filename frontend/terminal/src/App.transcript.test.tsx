import assert from 'node:assert/strict';
import test from 'node:test';
import {PassThrough} from 'node:stream';
import React from 'react';
import {render} from 'ink';

import {App} from './App.js';

const nextLoopTurn = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
const stripAnsi = (value: string): string => value.replace(/\u001B\[[0-9;?]*[ -/]*[@-~]/g, '');

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

async function settle(): Promise<void> {
	for (let i = 0; i < 20; i += 1) {
		await nextLoopTurn();
	}
}

function lastFrame(writes: string[]): string {
	return stripAnsi(writes[writes.length - 1] ?? '');
}

async function waitForFrame(
	writes: string[],
	predicate: (frame: string) => boolean,
	timeoutMs = 3000,
): Promise<string> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		await settle();
		const frame = lastFrame(writes);
		if (predicate(frame)) {
			return frame;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
	}
	return lastFrame(writes);
}

const backendScript = [
	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}}));",
	'process.stdin.resume();',
].join(' ');

const historyBackendScript = [
	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}}));",
	"for (let i = 0; i < 50; i += 1) { console.log('OHJSON:' + JSON.stringify({type: 'transcript_item', item: {role: 'assistant', text: 'history-' + i}})); }",
	'process.stdin.resume();',
].join(' ');

test('long transcripts keep their committed prefix in scrollback', async () => {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', historyBackendScript]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();
	try {
		// Poll instead of sleeping a fixed 500ms: under a loaded suite the child
		// process can take longer to stream all 50 history items.
		const deadline = Date.now() + 8000;
		while (Date.now() < deadline) {
			const written = stripAnsi(writes.join(''));
			if (written.includes('history-0') && written.includes('history-49')) {
				break;
			}
			await new Promise((resolve) => setTimeout(resolve, 25));
			await settle();
		}
		const output = stripAnsi(writes.join(''));
		assert.match(output, /history-0/);
		assert.match(output, /history-49/);
		assert.match(writes.join(''), /\u001B\]0;[^\u0007]*test-model/);
	} finally {
		instance.unmount();
		await exitPromise;
		instance.cleanup();
		stdin.destroy();
		stdout.destroy();
	}
});

test('real App toggles the transcript panel repeatedly', async () => {
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
	try {
		await settle();

		stdin.write('\u000f');
		await settle();
		assert.match(lastFrame(writes), /↑\/↓ scroll/);

		stdin.write('\u000f');
		await settle();
		assert.doesNotMatch(lastFrame(writes), /↑\/↓ scroll/);

		stdin.write('\u000f');
		await settle();
		assert.match(lastFrame(writes), /↑\/↓ scroll/);
	} finally {
		instance.unmount();
		await exitPromise;
		instance.cleanup();
		stdin.destroy();
		stdout.destroy();
	}
});

test('esc closes the transcript panel', async () => {
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
	try {
		await settle();

		stdin.write('\u000f');
		await settle();
		assert.match(lastFrame(writes), /↑\/↓ scroll/);

		stdin.write('\u001B');
		await settle();
		assert.doesNotMatch(lastFrame(writes), /↑\/↓ scroll/);
	} finally {
		instance.unmount();
		await exitPromise;
		instance.cleanup();
		stdin.destroy();
		stdout.destroy();
	}
});

const oversizedOutputBackendScript = [	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}}));",
	"setTimeout(() => {",
	"  const text = Array.from({length: 200}, (_, index) => 'system-line-' + index).join('\\n');",
	"  console.log('OHJSON:' + JSON.stringify({type: 'transcript_item', item: {role: 'system', text}}));",
	"  console.log('OHJSON:' + JSON.stringify({type: 'line_complete'}));",
	'}, 100);',
	'process.stdin.resume();',
].join('\n');

test('oversized command output opens a closable, scrollable view', async () => {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', oversizedOutputBackendScript]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();
	try {
		const frame = await waitForFrame(writes, (value) => value.includes('Command output'));
		assert.match(frame, /Command output/);
		assert.match(frame, /esc \/ q \/ ctrl\+o close/);
		assert.match(frame, /system-line-199/);

		stdin.write('\u001B');
		const closed = await waitForFrame(writes, (value) => !value.includes('Command output'));
		assert.doesNotMatch(closed, /Command output/);
		assert.match(closed, /system-line-0/);
		assert.match(closed, /more lines · ctrl\+o full transcript/);
	} finally {
		instance.unmount();
		await exitPromise;
		instance.cleanup();
		stdin.destroy();
		stdout.destroy();
	}
});
