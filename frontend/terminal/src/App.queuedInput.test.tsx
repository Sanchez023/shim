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
		const frame = lastFrame(writes);
		if (predicate(frame)) {
			return frame;
		}
		await new Promise((resolve) => setTimeout(resolve, 25));
		await settle();
	}
	return lastFrame(writes);
}

const backendScript = (turnMs: number): string =>
	[
		"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model'}, commands: ['/model', '/help'], command_descriptions: {}}));",
		'process.stdin.resume();',
		"let buf = '';",
		"process.stdin.on('data', (chunk) => {",
		'  buf += chunk.toString();',
		'  let idx;',
		"  while ((idx = buf.indexOf('\\n')) >= 0) {",
		'    const line = buf.slice(0, idx); buf = buf.slice(idx + 1);',
		'    if (!line) continue;',
		'    let msg;',
		'    try { msg = JSON.parse(line); } catch { continue; }',
		"    if (msg.type === 'submit_line') {",
		"      console.log('OHJSON:' + JSON.stringify({type: 'transcript_item', item: {role: 'user', text: msg.line}}));",
		"      console.log('OHJSON:' + JSON.stringify({type: 'tool_started', tool_name: 'bash', item: {role: 'tool', text: 'running', tool_name: 'bash'}}));",
		'      setTimeout(() => {',
		"        console.log('OHJSON:' + JSON.stringify({type: 'assistant_complete', message: 'reply to ' + msg.line}));",
		"        console.log('OHJSON:' + JSON.stringify({type: 'line_complete'}));",
		`      }, ${turnMs});`,
		'    }',
		'  }',
		'});',
	].join('\n');

type Harness = {
	stdin: InkTestStdin;
	writes: string[];
	stop: () => Promise<void>;
};

async function startApp(script: string): Promise<Harness> {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', script]}} />,
		{
			stdin: stdin as unknown as NodeJS.ReadStream & {fd: 0},
			stdout: stdout as unknown as NodeJS.WriteStream,
			exitOnCtrlC: false,
			debug: true,
			patchConsole: false,
		},
	);
	const exitPromise = instance.waitUntilExit();
	await waitForFrame(writes, (frame) => frame.includes('test-model'));
	return {
		stdin,
		writes,
		stop: async () => {
			instance.unmount();
			await exitPromise;
			instance.cleanup();
			stdin.destroy();
			stdout.destroy();
		},
	};
}

async function typeText(stdin: InkTestStdin, value: string): Promise<void> {
	for (const char of value) {
		stdin.write(char);
	}
	await settle();
}

async function pressEnter(stdin: InkTestStdin): Promise<void> {
	stdin.write('\r');
	await settle();
}

test('typing while the agent runs echoes and shows the queued message', async () => {
	const harness = await startApp(backendScript(20000));
	try {
		await typeText(harness.stdin, 'first');
		await pressEnter(harness.stdin);
		await waitForFrame(harness.writes, (frame) => frame.includes('Running bash'));

		await typeText(harness.stdin, 'second');
		assert.match(lastFrame(harness.writes), /second/, 'typed text should appear in the prompt');

		await pressEnter(harness.stdin);
		const frame = await waitForFrame(harness.writes, (value) => value.includes('↳ second'));
		assert.match(frame, /↳ second/);
		assert.match(frame, /edit queued message/);
	} finally {
		await harness.stop();
	}
});

test('up arrow pulls the latest queued message back into the prompt', async () => {
	const harness = await startApp(backendScript(20000));
	try {
		await typeText(harness.stdin, 'first');
		await pressEnter(harness.stdin);
		await waitForFrame(harness.writes, (frame) => frame.includes('Running bash'));

		await typeText(harness.stdin, 'second');
		await pressEnter(harness.stdin);
		await waitForFrame(harness.writes, (frame) => frame.includes('↳ second'));

		harness.stdin.write('\u001B[A');
		await settle();
		const frame = await waitForFrame(harness.writes, (value) => !value.includes('↳ second'));
		assert.match(frame, /> second/, 'queued text should move back into the prompt');
		assert.doesNotMatch(frame, /↳ second/);
	} finally {
		await harness.stop();
	}
});

test('queued message is submitted automatically when the turn completes', async () => {
	const harness = await startApp(backendScript(600));
	try {
		await typeText(harness.stdin, 'first');
		await pressEnter(harness.stdin);
		await waitForFrame(harness.writes, (frame) => frame.includes('Running bash'));

		await typeText(harness.stdin, 'second');
		await pressEnter(harness.stdin);
		await waitForFrame(harness.writes, (frame) => frame.includes('↳ second'));

		const frame = await waitForFrame(
			harness.writes,
			(value) => value.includes('reply to second'),
			5000,
		);
		assert.match(frame, /reply to second/);
	} finally {
		await harness.stop();
	}
});

test('slash command picker stays available while the agent runs', async () => {
	const harness = await startApp(backendScript(20000));
	try {
		await typeText(harness.stdin, 'first');
		await pressEnter(harness.stdin);
		await waitForFrame(harness.writes, (frame) => frame.includes('Running bash'));

		await typeText(harness.stdin, '/');
		const frame = await waitForFrame(harness.writes, (value) => value.includes('/model'));
		assert.match(frame, /\/model/);
	} finally {
		await harness.stop();
	}
});
