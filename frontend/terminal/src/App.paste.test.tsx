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
		columns: 100,
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

// Echoes every submitted line back as a user transcript item so the test can
// see exactly what the backend received.
const echoBackendScript = [
	"console.log('OHJSON:' + JSON.stringify({type: 'ready', state: {model: 'test-model'}, commands: [], command_descriptions: {}}));",
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
	"      console.log('OHJSON:' + JSON.stringify({type: 'assistant_complete', message: 'received'}));",
	"      console.log('OHJSON:' + JSON.stringify({type: 'line_complete'}));",
	'    }',
	'  }',
	'});',
].join('\n');

async function startApp(): Promise<{
	stdin: InkTestStdin;
	writes: string[];
	stop: () => Promise<void>;
}> {
	const stdin = createTestStdin();
	const stdout = createTestStdout();
	const writes: string[] = [];
	stdout.on('data', (chunk) => {
		writes.push(chunk.toString());
	});

	const instance = render(
		<App config={{backend_command: [process.execPath, '-e', echoBackendScript]}} />,
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

test('a large paste is collapsed in the composer and sent in full', async () => {
	const app = await startApp();
	const pasted = ['def main():', '    print("hi")', '    return 0', '', 'main()', 'done'].join('\n');

	try {
		app.stdin.write(pasted);
		await settle();

		const composerFrame = await waitForFrame(app.writes, (frame) =>
			frame.includes('[Pasted text #1 +5 lines]'),
		);
		assert.match(composerFrame, /\[Pasted text #1 \+5 lines\]/);
		assert.doesNotMatch(composerFrame, /def main\(\):/);

		app.stdin.write('\r');
		const sentFrame = await waitForFrame(app.writes, (frame) => frame.includes('received'));
		// The backend got the real text, not the placeholder.
		assert.match(sentFrame, /def main\(\):/);
		assert.match(sentFrame, /return 0/);
	} finally {
		await app.stop();
	}
});
