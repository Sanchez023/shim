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
	timeoutMs = 6000,
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

/**
 * The first prompt starts a turn with one tool call; the tool completes after
 * 300ms but the turn only finishes after 2.5s, leaving a wide window in which a
 * queued prompt must be handed over at the tool boundary.
 */
const script = [
	"const emit = (event) => console.log('OHJSON:' + JSON.stringify(event));",
	"emit({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}});",
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
	"    if (msg.type !== 'submit_line') continue;",
	"    if (msg.line === 'first') {",
	"      emit({type: 'transcript_item', item: {role: 'user', text: 'first'}});",
	"      emit({type: 'tool_started', item: {role: 'tool', text: 'running', tool_name: 'bash'}});",
	"      setTimeout(() => emit({type: 'tool_completed', item: {role: 'tool', text: 'done', tool_name: 'bash'}}), 300);",
	"      setTimeout(() => {",
	"        emit({type: 'assistant_complete', message: 'reply to first'});",
	"        emit({type: 'line_complete'});",
	'      }, 2500);',
	'    } else {',
	"      emit({type: 'transcript_item', item: {role: 'system', text: 'GOT ' + msg.line}});",
	'    }',
	'  }',
	'});',
].join('\n');

type Harness = {
	stdin: InkTestStdin;
	writes: string[];
	stop: () => Promise<void>;
};

async function startApp(): Promise<Harness> {
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

test('a queued prompt is handed to the backend at the next tool boundary', async () => {
	const harness = await startApp();
	try {
		await typeText(harness.stdin, 'first');
		await pressEnter(harness.stdin);
		await waitForFrame(harness.writes, (frame) => frame.includes('bash'));

		await typeText(harness.stdin, 'second');
		await pressEnter(harness.stdin);
		const queued = await waitForFrame(harness.writes, (frame) => frame.includes('↳ second'));
		assert.match(queued, /↳ second/, 'the prompt should sit in the queue first');

		const flushed = await waitForFrame(harness.writes, (frame) => frame.includes('GOT second'));
		assert.match(flushed, /GOT second/, 'the tool boundary must hand the prompt over');
		assert.doesNotMatch(flushed, /↳ second/, 'the queue entry should be gone once flushed');
	} finally {
		await harness.stop();
	}
});
